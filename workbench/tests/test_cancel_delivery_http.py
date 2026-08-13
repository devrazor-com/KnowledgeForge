"""Operator cancellation-DELIVERY state, end to end over real HTTP (Step 3B-1 fix).

Regression coverage for the defect where a *failed* cancel-delivery attempt hid /
permanently disabled the Cancel control and the single `cancel_requested` boolean
could not represent what Module 1 actually knew about delivery.

The model under test:
    cancel_delivery = NULL | unknown | undelivered | rejected | acknowledged
  * intent (`cancel_requested`) is separate from delivery knowledge;
  * 'unknown' is persisted BEFORE the call (so an interruption reconciles correctly);
  * only ECONNREFUSED / DNS are 'undelivered'; timeout / 5xx stay 'unknown';
  * 4xx is 'rejected'; 2xx is 'acknowledged' (NOT the same as the run being cancelled);
  * 'acknowledged' is sticky — a later failed retry cannot downgrade it;
  * the Cancel control's presence tracks TERMINAL state only, never attempt history;
  * the post-timeout cleanup cancel must not touch any operator-cancel field.

Note on the mock: it holds run state in memory, so "kill then restore with the run
intact" is not literally possible. Where a test needs a Gateway that was down and
then serves the same run again, it re-establishes the run on the restarted mock
with the same run_id (a harness accommodation, clearly marked — not contract
behaviour). Recovery itself never re-calls start; the mock start counters prove it.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import _regutil

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- process / http helpers ---------------------------------------------------

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_ready(port, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _start(module, port, env):
    return subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
                            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p is None:
            continue
        try:
            p.terminate(); p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def _wb_env(mock_port, dbpath, timeout=180, guard=1, http_timeout=None):
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(dbpath)
    env["WORKBENCH_DEV_MOCK"] = "1"
    env["WORKBENCH_TIMEOUT_SECONDS"] = str(timeout)
    env["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = str(guard)
    if http_timeout is not None:
        env["WORKBENCH_GATEWAY_HTTP_TIMEOUT"] = str(http_timeout)
    return env


def _post_run(wb_port, forced=None, fault=None):
    fields = [("source_id", _regutil.ensure_larkspur(wb_port)), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem")]
    if forced:
        fields.append(("forced_outcome", forced))
    if fault:
        fields.append(("fault", fault))

    class _NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(_NR).open(
            urllib.request.Request(f"http://127.0.0.1:{wb_port}/runs",
                                   data=urllib.parse.urlencode(fields).encode(), method="POST"))
    except urllib.error.HTTPError as e:
        return e.headers["Location"].rsplit("/", 1)[-1]
    raise AssertionError("expected 303")


def _api(wb_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _cancel(wb_port, run_id):
    """POST the operator cancel; return the parsed JSON response."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{wb_port}/runs/{run_id}/cancel", data=b"", method="POST"), timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def _run_html(wb_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/runs/{run_id}", timeout=5) as r:
        return r.read().decode()


def _cancel_button_present(wb_port, run_id):
    return 'id="cancel-btn"' in _run_html(wb_port, run_id)


def _mock_info(mock_port):
    with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/", timeout=5) as r:
        return json.loads(r.read().decode())


def _wait_mock_events(mock_port, run_id, n, timeout=10.0):
    """Wait until the mock has emitted at least n events for run_id. Needed when a
    restarted (in-memory) mock re-establishes a run: its sequence numbering only
    lines up past the Workbench's cursor once its non-terminal prefix has been
    emitted, so a subsequent cancelled event gets a fresh, higher sequence."""
    end = time.time() + timeout
    while time.time() < end:
        with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/runs/{run_id}/events?since=0", timeout=3) as r:
            if len(json.loads(r.read().decode())["events"]) >= n:
                return True
        time.sleep(0.2)
    return False


def _mock_start_direct(mock_port, request, fault=None, cancel_fault=None, forced=None):
    q = {}
    if forced:
        q["forced_outcome"] = forced
    if fault:
        q["fault"] = fault
    if cancel_fault:
        q["cancel_fault"] = cancel_fault
    url = f"http://127.0.0.1:{mock_port}/runs" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    req = urllib.request.Request(url, data=json.dumps(request).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _poll(wb_port, run_id, until, timeout=45.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        v = _api(wb_port, run_id)
        if until(v):
            return v
        time.sleep(0.25)
    return v


def _seed_running(dbpath, mock_port, run_id, cancel_requested=0, cancel_delivery=None, timeout=180):
    """In-process seed of an accepted, running run with one non-terminal event."""
    os.environ["WORKBENCH_DB"] = str(dbpath)
    from workbench import config, db, orchestrator
    from workbench.tasks import load_tasks
    root = config.PACKAGES_DIR / "larkspur"
    task = next(t for t in load_tasks(root / "tasks") if t.id == "LARK-TASK-001")
    request = orchestrator.build_request(root, "larkspur", task, ["filesystem"], "larkspur-sandbox", run_id)
    request["execution_context"]["timeout_seconds"] = timeout
    db.init()
    db.create_run({"run_id": run_id, "package_name": request["package"]["name"],
                   "package_fingerprint": "sha256:p", "task_id": task.id, "task_fingerprint": task.fingerprint,
                   "capabilities": ["filesystem"], "target_environment": "larkspur-sandbox",
                   "request": request, "request_validation": {"passed": True, "errors": []},
                   "run_state": "running"})
    db.set_run_running(run_id, {"accepted": True})
    db.append_event(run_id, {"run_id": run_id, "sequence": 1, "timestamp": "2026-01-01T00:00:00Z",
                             "event_type": "started", "message": "seeded"}, {"passed": True, "errors": []})
    if cancel_requested:
        db.set_cancel_requested(run_id)
    if cancel_delivery:
        db.set_cancel_delivery(run_id, cancel_delivery)
    return request


# --- tests --------------------------------------------------------------------

def test_known_non_delivery_then_retry_reaches_cancelled(tmp_path):
    """The exact operator flow from the defect report: an active (recovering) run,
    Gateway down in a way that makes delivery DEFINITELY fail (connection refused),
    operator Cancel → 'undelivered', run stays non-terminal, Cancel stays available,
    the UI shows the request was not delivered, recovery does not auto-reissue; then
    the Gateway is restored, an explicit second Cancel is acknowledged, the Gateway
    returns a valid cancelled result, and the run reaches verdict rule #1 — the
    Cancel button disappears only because the run is now terminal."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=180, guard=1, http_timeout=3)
    mock1 = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")  # stays non-terminal
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        request = _api(wb_port, run_id)["request"]

        # Make it a RECOVERING run (tolerates a transient outage), then take the
        # Gateway down hard so the cancel delivery is provably refused.
        _stop(wb1)
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running", timeout=10)
        _stop(mock1)  # process gone → ECONNREFUSED on the fixed port

        r = _cancel(wb_port, run_id)
        assert r["attempt"] == "undelivered" and r["delivery"] == "undelivered" and r["ok"] is False
        v = _api(wb_port, run_id)
        assert v["run_state"] == "running"                       # NOT falsely terminated
        assert v["cancel_requested"] and v["cancel_delivery"] == "undelivered"
        assert v["cancel_note"]["delivery"] == "undelivered" and "did not arrive" in v["cancel_note"]["message"]
        assert _cancel_button_present(wb_port, run_id)           # control still available
        _stop(wb2)

        # Restore the Gateway. The mock is in-memory, so re-establish the same run on
        # the restarted process (harness accommodation). Recovery must NOT call start.
        mock2 = _start("tools.mock_gateway.app:app", mock_port, env)
        assert _wait_ready(mock_port)
        _mock_start_direct(mock_port, request, fault="never_terminal", forced="success")
        assert _mock_info(mock_port)["starts"].get(run_id) == 1  # the re-establish; recovery adds none

        wb3 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running", timeout=15)
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == 0   # recovery did not reissue cancel
        # Let the re-established run emit its non-terminal prefix so the cancelled
        # event gets a sequence past the Workbench's cursor (in-memory mock detail).
        assert _wait_mock_events(mock_port, run_id, 2)

        # Explicit second Cancel → acknowledged → real cancelled result → rule #1.
        r2 = _cancel(wb_port, run_id)
        assert r2["attempt"] == "acknowledged" and r2["delivery"] == "acknowledged" and r2["ok"] is True
        v2 = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=20)
        assert v2["run_state"] == "terminal" and v2["outcome"] == "cancelled" and v2["verdict"]["rule"] == 1
        assert v2["cancel_delivery"] == "acknowledged"
        assert _mock_info(mock_port)["starts"].get(run_id) == 1       # still exactly one start
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == 1   # exactly the one explicit retry
        assert not _cancel_button_present(wb_port, run_id)            # gone ONLY because terminal
        _stop(wb3, mock2)
    finally:
        _stop(wb1, mock1)


def test_unknown_delivery_on_timeout(tmp_path):
    """A cancel that hits a post-connect TIMEOUT (Gateway paused) is 'unknown', NOT
    'undelivered': Module 1 cannot prove the request failed to arrive — indeed a
    paused Gateway may buffer the request and act on it once resumed. So the run
    stays non-terminal, Cancel stays available, and the wording is the conservative
    'cannot determine' — never the stronger 'did not arrive'. (The killed-Gateway
    test covers the genuinely-undelivered case and the retry-to-cancelled flow.)"""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=180, guard=1, http_timeout=2)  # detect the hang quickly
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        _stop(wb1)
        wb2 = _start("workbench.app:app", wb_port, env)   # recovering run tolerates the outage
        assert _wait_ready(wb_port)
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running", timeout=10)

        mock.send_signal(signal.SIGSTOP)   # paused → calls hang → socket timeout (indeterminate)
        try:
            r = _cancel(wb_port, run_id)
            assert r["attempt"] == "unknown" and r["delivery"] == "unknown" and r["ok"] is False
            v = _api(wb_port, run_id)
            assert v["run_state"] == "running" and v["cancel_delivery"] == "unknown"
            assert "cannot determine" in v["cancel_note"]["message"]   # conservative, not 'undelivered'
            assert "did not arrive" not in v["cancel_note"]["message"]
            assert _cancel_button_present(wb_port, run_id)             # control still usable
        finally:
            mock.send_signal(signal.SIGCONT)
        _stop(wb2)
    finally:
        try:
            mock.send_signal(signal.SIGCONT)
        except Exception:
            pass
        _stop(mock)


def test_cancel_rejected_4xx(tmp_path):
    """Gateway receives the cancel and declines it (4xx) → 'rejected'. Module 1 does
    not report success; the run stays non-terminal and Cancel remains available."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=180, guard=1)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    try:
        assert _wait_ready(mock_port)
        run_id = "run-cancel-4xx"
        request = _seed_running(dbpath, mock_port, run_id)
        _mock_start_direct(mock_port, request, fault="never_terminal", cancel_fault="reject", forced="success")
        wb = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running", timeout=10)
        r = _cancel(wb_port, run_id)
        assert r["attempt"] == "rejected" and r["delivery"] == "rejected" and r["ok"] is False
        v = _api(wb_port, run_id)
        assert v["run_state"] == "running" and v["cancel_delivery"] == "rejected"
        assert "rejected it" in v["cancel_note"]["message"]
        assert _cancel_button_present(wb_port, run_id)
        _stop(wb)
    finally:
        _stop(mock)


def test_cancel_5xx_stays_unknown(tmp_path):
    """Gateway errors on cancel (5xx) → 'unknown' (it may have partially acted).
    Module 1 does not report success; the run stays non-terminal."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=180, guard=1)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    try:
        assert _wait_ready(mock_port)
        run_id = "run-cancel-5xx"
        request = _seed_running(dbpath, mock_port, run_id)
        _mock_start_direct(mock_port, request, fault="never_terminal", cancel_fault="http_500", forced="success")
        wb = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running", timeout=10)
        r = _cancel(wb_port, run_id)
        assert r["attempt"] == "unknown" and r["delivery"] == "unknown" and r["ok"] is False
        v = _api(wb_port, run_id)
        assert v["run_state"] == "running" and v["cancel_delivery"] == "unknown"
        assert _cancel_button_present(wb_port, run_id)
        _stop(wb)
    finally:
        _stop(mock)


def test_sticky_acknowledged_not_downgraded_by_failed_retry(tmp_path):
    """Once acknowledged, a later failed operator attempt (here connection refused,
    the Gateway being down) must NOT downgrade the durable state. The immediate
    response still describes the failed attempt."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=180, guard=1, http_timeout=2)
    # Seed a run that is running, already acknowledged (Gateway acked a prior cancel
    # but the cancelled event hasn't been observed yet). No mock is started, so the
    # cancel call is refused.
    _seed_running(dbpath, mock_port, "run-sticky", cancel_requested=1, cancel_delivery="acknowledged")
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        _poll(wb_port, "run-sticky", lambda v: v["run_state"] == "running", timeout=10)
        assert _api(wb_port, "run-sticky")["cancel_delivery"] == "acknowledged"
        r = _cancel(wb_port, "run-sticky")   # Gateway down → refused → attempt 'undelivered'
        assert r["attempt"] == "undelivered"                     # this attempt failed to deliver
        assert r["delivery"] == "acknowledged"                   # durable knowledge NOT downgraded
        assert r["attempt_message"] and "could not reach" in r["attempt_message"]
        assert _api(wb_port, "run-sticky")["cancel_delivery"] == "acknowledged"
        _stop(wb)
    finally:
        _stop(wb)


def test_post_timeout_cleanup_does_not_touch_operator_fields(tmp_path):
    """Module 1's own post-timeout cleanup cancel must never look like operator
    cancellation: after a timed_out run, cancel_requested stays 0 and cancel_delivery
    stays NULL — even though the mock observed the cleanup cancel call."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=2, guard=1)   # short deadline → Module 1 times out
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "error", timeout=20)
        assert v["error_kind"] == "timed_out"
        # Operator-cancel fields untouched by the cleanup cancel…
        assert not v["cancel_requested"] and v["cancel_delivery"] is None and v["cancel_note"] is None
        # …even though the cleanup cancel call reached the mock.
        time.sleep(1.0)
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) >= 1
        # Re-read: still untouched.
        v2 = _api(wb_port, run_id)
        assert not v2["cancel_requested"] and v2["cancel_delivery"] is None
    finally:
        _stop(wb, mock)

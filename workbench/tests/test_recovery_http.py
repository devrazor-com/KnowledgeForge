"""Restart recovery & reconciliation over real HTTP (Step 3B-1).

The mock (Gateway) stays running; only the Workbench is killed and restarted —
the realistic model. Covers every partial persisted state, the interruptibility
of recovery itself, unreachable-during-recovery, a Gateway that no longer
recognises the run, and both halves of the ambiguous pre-acceptance case.

Seeding: we initialise the schema and write rows in-process (workbench.db, pointed
at the test DB via WORKBENCH_DB), then start the Workbench subprocess against the
same DB so its startup reconciliation sees them.
"""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import _regutil

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]

# SIGSTOP/SIGCONT pause/resume a live process to simulate a Gateway that is
# unreachable-but-intact. They are POSIX-only (absent on Windows), so the ONE test
# that needs them skips cleanly there; every other recovery test is platform-neutral.
_HAS_JOB_CONTROL = hasattr(signal, "SIGSTOP") and hasattr(signal, "SIGCONT")
_needs_job_control = pytest.mark.skipif(
    not _HAS_JOB_CONTROL, reason="SIGSTOP/SIGCONT are POSIX-only (not available on Windows)")


# --- process / http helpers ---------------------------------------------------

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wb_env(mock_port, dbpath, timeout=60, guard=1, result_window=30):
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(dbpath)
    env["WORKBENCH_DEV_MOCK"] = "1"
    env["WORKBENCH_TIMEOUT_SECONDS"] = str(timeout)
    env["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = str(guard)
    env["WORKBENCH_RESULT_RETRIEVAL_WINDOW_SECONDS"] = str(result_window)
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
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{wb_port}/runs/{run_id}/cancel", data=b"", method="POST"), timeout=5)
    except urllib.error.HTTPError:
        pass


def _mock_info(mock_port):
    with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/", timeout=5) as r:
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


def _rewind(dbpath, run_id, **cols):
    """Rewind persisted run columns to set up a crash-recovery precondition.

    Retries briefly with bounded backoff on a transient SQLite open/IO error: on Windows
    the DB file (and its -wal/-shm) can stay momentarily locked just after the Workbench
    child exits. This is defense-in-depth ON TOP OF stop_server(), which already waits for
    the child's real exit before we get here — the retry must not be relied on to mask a
    process that is still alive."""
    sets = ", ".join(f"{k}=?" for k in cols)
    last = None
    for attempt in range(10):                       # bounded: ~11s worst case, then re-raise
        try:
            con = sqlite3.connect(dbpath, timeout=5)
            try:
                con.execute(f"UPDATE run SET {sets} WHERE run_id=?", (*cols.values(), run_id))
                con.commit()
            finally:
                con.close()
            return
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(0.2 * (attempt + 1))
    raise last


# --- tests --------------------------------------------------------------------

def test_recover_accepted_no_terminal(tmp_path):
    """Core demo: kill the Workbench mid-run; restart resumes the same Gateway run."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and len(v["events"]) >= 2, timeout=15)
        _stop(wb1)  # kill mid-run; the mock keeps running
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=30)
        assert v["run_state"] == "terminal" and v["outcome"] == "passed"
        assert _mock_info(mock_port)["starts"].get(run_id) == 1   # no duplicate start
        _stop(wb2)
    finally:
        _stop(mock)


def test_recover_terminal_event_no_result(tmp_path):
    """Terminal event persisted but result not retrieved → completed on restart."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "terminal", timeout=20)  # completes normally
        _stop(wb1)
        # Rewind to "terminal event persisted, result not retrieved".
        _rewind(dbpath, run_id, run_state="running", result_json=None, verdict_json=None,
                outcome=None, contract_status=None, result_validation_json=None,
                gateway_result_json=None, terminal_at=None)
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=30)
        assert v["run_state"] == "terminal" and v["outcome"] == "passed"
        assert _mock_info(mock_port)["starts"].get(run_id) == 1
        _stop(wb2)
    finally:
        _stop(mock)


def test_recover_deadline_expired_while_down(tmp_path):
    """Deadline expired while the Workbench was down → timed_out on restart, and a
    second recovery does not re-fire the cleanup cancel."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=2, guard=1)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        _stop(wb1)
        # Make the deadline already expired: accepted_at far in the past.
        _rewind(dbpath, run_id, run_state="running",
                accepted_at="2000-01-01T00:00:00+00:00")
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "error", timeout=20)
        assert v["error_kind"] == "timed_out" and v["result"] is None
        cancels_after_first = _mock_info(mock_port)["cancels"].get(run_id, 0)
        _stop(wb2)
        # Second recovery: run already terminal → skipped → no extra cleanup cancel.
        wb3 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        time.sleep(1.5)
        v2 = _api(wb_port, run_id)
        assert v2["error_kind"] == "timed_out"                     # unchanged
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == cancels_after_first  # no 2nd cleanup cancel
        _stop(wb3)
    finally:
        _stop(mock)


def test_recover_cancel_requested_unknown_delivery(tmp_path):
    """Pre-crash cancel whose DELIVERY outcome is genuinely unknown (e.g. the
    Workbench died mid-call, leaving the persisted 'unknown'). Recovery does NOT
    auto-reissue; the UI shows the conservative unknown wording; an explicit
    operator cancel then ends the run (rule #1)."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=60, guard=1)   # long deadline; won't fire
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        _stop(wb1)
        # Requested before the crash, delivery outcome unknown (as request_cancel
        # persists 'unknown' before its Gateway call).
        _rewind(dbpath, run_id, cancel_requested=1, cancel_delivery="unknown")
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: (v.get("cancel_note") or {}).get("delivery") == "unknown", timeout=15)
        assert v["run_state"] == "running"
        assert v["cancel_note"]["delivery"] == "unknown"
        assert "cannot determine" in v["cancel_note"]["message"]           # conservative wording
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == 0        # recovery did not auto-reissue
        # Operator explicitly cancels → run ends cancelled (rule #1).
        _cancel(wb_port, run_id)
        v2 = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=20)
        assert v2["run_state"] == "terminal" and v2["outcome"] == "cancelled" and v2["verdict"]["rule"] == 1
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == 1        # exactly the explicit one
        _stop(wb2)
    finally:
        _stop(mock)


def test_recover_cancel_delivery_known_failed(tmp_path):
    """Pre-crash cancel whose delivery was KNOWN to have failed ('undelivered').
    Recovery preserves that stronger knowledge — it tells the operator the previous
    request did not arrive (distinct from the unknown wording) — does NOT auto-
    reissue, and keeps the run cancellable."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=60, guard=1)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="never_terminal")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        _stop(wb1)
        _rewind(dbpath, run_id, cancel_requested=1, cancel_delivery="undelivered")
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: (v.get("cancel_note") or {}).get("delivery") == "undelivered", timeout=15)
        assert v["run_state"] == "running"
        assert v["cancel_note"]["delivery"] == "undelivered"
        assert "did not arrive" in v["cancel_note"]["message"]             # stronger-than-unknown wording
        assert _mock_info(mock_port)["cancels"].get(run_id, 0) == 0        # not auto-reissued
        _stop(wb2)
    finally:
        _stop(mock)


def test_start_unresolved_when_start_never_reached(tmp_path):
    """submitting, no ack, mock never saw a start → start_unresolved, no start issued."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath)
    os.environ["WORKBENCH_DB"] = str(dbpath)
    from workbench import db
    db.init()
    run_id = "run-seed-neveraccepted"
    db.create_run({"run_id": run_id, "package_name": "Larkspur", "package_fingerprint": "sha256:p",
                   "task_id": "LARK-TASK-001", "task_fingerprint": "sha256:t", "capabilities": ["filesystem"],
                   "target_environment": "larkspur-sandbox",
                   "request": {"run_id": run_id, "package": {"name": "Larkspur"}, "task": {"checks": []}},
                   "request_validation": {"passed": True, "errors": []}, "run_state": "submitting"})
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "error", timeout=15)
        assert v["error_kind"] == "start_unresolved" and v["result"] is None and v["outcome"] is None
        assert run_id not in _mock_info(mock_port)["starts"]   # start was never called
    finally:
        _stop(wb, mock)


def test_start_unresolved_dangerous_half_orphan_left_untouched(tmp_path):
    """The Gateway DOES have a run for the run_id (start was accepted) but Module 1
    crashed before persisting the ack. Recovery records start_unresolved, never
    re-calls start, and leaves the existing Gateway run untouched."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success")     # creates the run on the mock (starts=1)
        _poll(wb_port, run_id, lambda v: v["gateway_run_created"], timeout=15)
        assert _mock_info(mock_port)["starts"].get(run_id) == 1
        _stop(wb1)
        # Simulate: accepted by the Gateway, but Module 1 crashed before persisting the ack.
        _rewind(dbpath, run_id, run_state="submitting", accepted_at=None, gateway_ack_json=None)
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "error", timeout=15)
        assert v["error_kind"] == "start_unresolved"
        assert _mock_info(mock_port)["starts"].get(run_id) == 1   # NOT restarted — orphan untouched
        _stop(wb2)
    finally:
        _stop(mock)


def test_recover_gateway_no_longer_recognises_run(tmp_path):
    """Accepted run whose run_id the Gateway does not have → gateway_http_error."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=60, guard=1)
    os.environ["WORKBENCH_DB"] = str(dbpath)
    from workbench import db
    db.init()
    run_id = "run-seed-unknown-to-gateway"
    db.create_run({"run_id": run_id, "package_name": "Larkspur", "package_fingerprint": "sha256:p",
                   "task_id": "LARK-TASK-001", "task_fingerprint": "sha256:t", "capabilities": ["filesystem"],
                   "target_environment": "larkspur-sandbox",
                   "request": {"run_id": run_id, "package": {"name": "Larkspur"},
                               "task": {"checks": []}, "execution_context": {"timeout_seconds": 60}},
                   "request_validation": {"passed": True, "errors": []}, "run_state": "submitting"})
    db.set_run_running(run_id, {"accepted": True})   # accepted_at + gateway_ack present, but the mock has no such run
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "error", timeout=15)
        assert v["error_kind"] == "gateway_http_error"
    finally:
        _stop(wb, mock)


@_needs_job_control
def test_recover_gateway_unreachable_then_reachable(tmp_path):
    """Gateway temporarily unreachable during recovery (paused, state intact):
    the recovering status shows, and on resume the run completes normally."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath, timeout=60, guard=1)
    env["WORKBENCH_GATEWAY_HTTP_TIMEOUT"] = "2"          # a SIGSTOPped mock hangs; detect the outage quickly
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and len(v["events"]) >= 2, timeout=15)
        _stop(wb1)
        mock.send_signal(signal.SIGSTOP)                 # unreachable but state intact
        wb2 = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: (v.get("recovery_status") or {}).get("kind") == "recovering_unreachable", timeout=15)
        assert (v["recovery_status"] or {}).get("kind") == "recovering_unreachable"
        assert v["run_state"] == "running"               # not terminal — bounded by the run deadline
        mock.send_signal(signal.SIGCONT)                 # reachable again, run intact
        v2 = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=30)
        assert v2["run_state"] == "terminal" and v2["outcome"] == "passed"
        _stop(wb2)
    finally:
        try:
            mock.send_signal(signal.SIGCONT)
        except Exception:
            pass
        _stop(mock)


def test_recovery_is_interruptible_double_kill(tmp_path):
    """Killing the Workbench again during recovery and restarting is safe: one
    terminal disposition, no duplicate start, no duplicated events."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _wb_env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb1 = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success")
        _poll(wb_port, run_id, lambda v: v["run_state"] == "running" and len(v["events"]) >= 2, timeout=15)
        _stop(wb1)
        wb2 = _start("workbench.app:app", wb_port, env)   # recovery begins
        assert _wait_ready(wb_port)
        time.sleep(0.8)
        _stop(wb2)                                        # kill again during recovery
        wb3 = _start("workbench.app:app", wb_port, env)   # recover again
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=30)
        assert v["run_state"] == "terminal" and v["outcome"] == "passed"   # exactly one disposition
        assert _mock_info(mock_port)["starts"].get(run_id) == 1            # no duplicate start
        seqs = [e["event"]["sequence"] for e in v["events"]]
        assert seqs == sorted(set(seqs))                                   # no duplicated persisted events
        _stop(wb3)
    finally:
        _stop(mock)

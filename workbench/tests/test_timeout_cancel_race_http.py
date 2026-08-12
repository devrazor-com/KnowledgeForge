"""Deadline/cancel race over real HTTP (Step 3A, clarification 4).

A user cancellation is fired close to the run deadline, at slightly different
offsets across several iterations, so that on any given machine either
cancellation or timeout may legitimately win. The test does NOT depend on which
wins; it asserts the invariant every time:

  * exactly one terminal disposition wins;
  * cancel wins  → valid cancelled result → rule #1 → cancelled;
  * timeout wins → error_kind=timed_out → inconclusive (no result);
  * never two terminal meanings, and — after waiting beyond the fire-and-forget
    cleanup-cancel window — never a later transition from one to the other.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Deadline = accepted_at (sub-second precise) + 2 + 1 = ~3s. Offsets exercise BOTH
# orderings reliably: a clear-early cluster (0.5/1.5) always lets cancellation win,
# a clear-late cluster (4.0/4.5) always lets the deadline win, and two genuinely
# near-boundary offsets (2.4/2.8) straddle it — the poller's ~0.5s tick means a
# cancel landing very close to the deadline can legitimately lose to it. The
# invariant must hold for every iteration regardless of which wins.
OFFSETS = [0.5, 1.5, 2.4, 2.8, 4.0, 4.5]
CLEANUP_SETTLE = 2.5   # wait beyond the cleanup window before re-reading


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


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
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _start_run(port):
    fields = [("dir_name", "larkspur"), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem"),
              ("forced_outcome", "success")]

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(f"http://127.0.0.1:{port}/runs",
                                   data=urllib.parse.urlencode(fields).encode(), method="POST"))
    except urllib.error.HTTPError as e:
        return e.headers["Location"].rsplit("/", 1)[-1]
    raise AssertionError("expected 303")


def _post_cancel(port, run_id):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/runs/{run_id}/cancel", data=b"", method="POST"), timeout=5)
    except urllib.error.HTTPError:
        pass  # 404/other are fine; the run may already be terminal


def _get(port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _poll_terminal(port, run_id, timeout=15.0):
    end = time.time() + timeout
    v = _get(port, run_id)
    while time.time() < end:
        v = _get(port, run_id)
        if v["run_state"] in ("terminal", "error"):
            return v
        time.sleep(0.2)
    return v


def _disposition(v: dict) -> tuple:
    """The single terminal meaning of a run, for equality across a re-read."""
    return (v["run_state"], v["outcome"], v["error_kind"],
            (v["result"] or {}).get("status") if v["result"] else None, v["terminal_at"])


def test_deadline_cancel_race_has_one_permanent_disposition(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"
    env["WORKBENCH_TIMEOUT_SECONDS"] = "2"        # deadline ≈ accepted + 2 + 1 ≈ 2–3s
    env["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb_proc = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        winners = {"cancelled": 0, "timed_out": 0}

        for offset in OFFSETS:
            run_id = _start_run(wb_port)
            time.sleep(offset)
            _post_cancel(wb_port, run_id)

            v = _poll_terminal(wb_port, run_id)
            assert v["run_state"] in ("terminal", "error"), f"offset {offset}: not terminal"

            if v["run_state"] == "terminal":       # cancellation won
                assert v["outcome"] == "cancelled"
                assert v["verdict"]["rule"] == 1
                assert v["result"]["status"] == "cancelled"
                assert v["error_kind"] is None
                winners["cancelled"] += 1
            else:                                   # deadline won
                assert v["error_kind"] == "timed_out"
                assert v["outcome"] is None
                assert v["result"] is None
                winners["timed_out"] += 1

            # No later transition: after the fire-and-forget cleanup cancel has had
            # time to make the Gateway produce a cancelled result, the local
            # disposition must be byte-for-byte unchanged.
            before = _disposition(v)
            time.sleep(CLEANUP_SETTLE)
            after = _disposition(_get(wb_port, run_id))
            assert after == before, (
                f"offset {offset}: disposition changed after cleanup window: {before} -> {after}")

        # Both orderings must actually have been exercised (the early and late
        # clusters guarantee this); otherwise only one path was tested.
        assert winners["cancelled"] + winners["timed_out"] == len(OFFSETS)
        assert winners["cancelled"] >= 1, f"cancellation ordering never exercised: {winners}"
        assert winners["timed_out"] >= 1, f"timeout ordering never exercised: {winners}"
    finally:
        _stop(wb_proc, mock)

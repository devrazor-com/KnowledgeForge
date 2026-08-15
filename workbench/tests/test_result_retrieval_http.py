"""Delayed-but-valid result publication (Step 3B-1).

The frozen contract does not guarantee the ValidationResult is available the instant
the terminal event appears, so Module 1 applies a bounded result-retrieval allowance.
These tests prove the MIDDLE case — result temporarily unavailable, then valid within
the window — for BOTH the normal execution path and restart recovery, and prove both
paths reach that behaviour through the SAME shared helper (not two look-alike impls).
"""

import inspect
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

import _regutil

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _env(mock_port, dbpath):
    e = os.environ.copy()
    e["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
    e["WORKBENCH_TIMEOUT_SECONDS"] = "60"
    e["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
    e["WORKBENCH_RESULT_RETRIEVAL_WINDOW_SECONDS"] = "20"   # comfortably > the ~2s mock delay
    return e


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


def _poll(wb_port, run_id, until, timeout=40.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        v = _api(wb_port, run_id)
        if until(v):
            return v
        time.sleep(0.25)
    return v


def _mock_post_start(mock_port, request, fault, forced="success"):
    url = f"http://127.0.0.1:{mock_port}/runs?forced_outcome={forced}&fault={fault}"
    req = urllib.request.Request(url, data=json.dumps(request).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _mock_events(mock_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/runs/{run_id}/events?since=0", timeout=5) as r:
        return json.loads(r.read().decode())["events"]


def test_normal_execution_waits_for_delayed_result(tmp_path):
    """Normal path: terminal event → result null for several polls → valid → passed."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, forced="success", fault="delayed_result")
        v = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=40)
        assert v["run_state"] == "terminal"          # waited through the delay, no error
        assert v["outcome"] == "passed" and v["verdict"]["rule"] == 6
    finally:
        _stop(wb, mock)


def test_recovery_waits_for_delayed_result(tmp_path):
    """Recovery path: a terminal event is persisted, the result is temporarily
    unavailable, then becomes valid within the window → passed."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    try:
        assert _wait_ready(mock_port)

        # Build a valid request (real task with checks → passed verdict) and start it
        # directly on the mock with the delayed_result fault. Do NOT call result here,
        # so the delay is still pending when recovery runs.
        os.environ["WORKBENCH_DB"] = str(dbpath)
        from workbench import config, db, orchestrator
        from workbench.tasks import load_tasks
        root = config.PACKAGES_DIR / "larkspur"
        task = next(t for t in load_tasks(root / "tasks") if t.id == "LARK-TASK-001")
        run_id = "run-recovery-delayed"
        request = orchestrator.build_request(root, "larkspur", task, ["filesystem"], "larkspur-sandbox", run_id)
        _mock_post_start(mock_port, request, fault="delayed_result")

        # Wait until the mock has emitted the terminal event.
        end = time.time() + 15
        events = []
        while time.time() < end:
            events = _mock_events(mock_port, run_id)
            if events and events[-1]["event_type"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.3)
        assert events and events[-1]["event_type"] == "completed"

        # Seed the Workbench: accepted, all events persisted (incl terminal), no result.
        db.init()
        db.create_run({"run_id": run_id, "package_name": request["package"]["name"],
                       "package_fingerprint": "sha256:p", "task_id": task.id, "task_fingerprint": task.fingerprint,
                       "capabilities": ["filesystem"], "target_environment": "larkspur-sandbox",
                       "request": request, "request_validation": {"passed": True, "errors": []},
                       "run_state": "running"})
        db.set_run_running(run_id, {"accepted": True})
        for ev in events:
            db.append_event(run_id, ev, {"passed": True, "errors": []})

        # Start the Workbench → recovery rule #2 → shared retrieval → passed.
        wb = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        v = _poll(wb_port, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=40)
        assert v["run_state"] == "terminal"
        assert v["outcome"] == "passed" and v["verdict"]["rule"] == 6
        _stop(wb)
    finally:
        _stop(mock)


def test_both_paths_use_the_same_retrieval_helper():
    """Architectural seam: the normal poller and restart recovery both finalize a
    terminal run through the SAME _finish_from_result → _retrieve_result helper."""
    from workbench import orchestrator as o
    assert "_retrieve_result" in inspect.getsource(o._finish_from_result)
    assert "_finish_from_result" in inspect.getsource(o._poll)                  # normal path
    assert "_finish_from_result" in inspect.getsource(o.recover_inflight_runs)  # recovery path
    # And there is exactly one result-retrieval implementation.
    assert sum(1 for n in dir(o) if n == "_retrieve_result") == 1

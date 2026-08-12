"""Unit invariants for the Step 3A termination policy.

Deadline is authoritative (per-call timeout bounded by remaining budget; raises
once the deadline passes), and terminal state is write-once (a timeout can never
be overwritten by a late Gateway result, and vice versa).
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from workbench import config, gateway_client
from workbench import orchestrator as o


def test_deadline_from_accepted_plus_request_timeout_plus_guard():
    now = datetime.now(timezone.utc)
    run = {"accepted_at": now.isoformat(timespec="seconds"),
           "request_json": json.dumps({"execution_context": {"timeout_seconds": 10}})}
    dl = o._deadline(run)
    expected = now + timedelta(seconds=10 + config.timeout_guard_seconds())
    assert abs((dl - expected).total_seconds()) < 2
    # No deadline before Gateway acceptance.
    assert o._deadline({"accepted_at": None, "request_json": None}) is None


def test_gw_call_bounds_call_timeout_by_remaining_budget():
    captured = {}

    def fake(*args, timeout=None):
        captured["timeout"] = timeout
        return 200, {"ok": True}

    # ~5s to deadline → call timeout is min(GATEWAY_HTTP_TIMEOUT, remaining) ≈ 5.
    dl = datetime.now(timezone.utc) + timedelta(seconds=5)
    status, _ = asyncio.run(o._gw_call(fake, dl, "x"))
    assert status == 200
    assert 4.0 <= captured["timeout"] <= 5.5

    # No deadline (pre-acceptance) → bounded only by the per-call socket timeout.
    asyncio.run(o._gw_call(fake, None, "x"))
    assert abs(captured["timeout"] - config.gateway_http_timeout()) < 0.01


def test_gw_call_raises_once_deadline_passed():
    def fake(*args, timeout=None):
        return 200, {}
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(gateway_client.GatewayError):
        asyncio.run(o._gw_call(fake, past, "x"))


def _running_run(db, run_id):
    db.create_run({
        "run_id": run_id, "package_name": "Larkspur", "package_fingerprint": "sha256:p",
        "task_id": "T", "task_fingerprint": "sha256:t", "capabilities": [],
        "target_environment": "env", "request": {"execution_context": {"timeout_seconds": 5}},
        "request_validation": {"passed": True, "errors": []}, "run_state": "running"})


def test_terminal_is_write_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "wb.db"))
    from workbench import db
    db.init()

    # finalize first → a later error write is a no-op (result stands).
    _running_run(db, "r1")
    ok = db.finalize_run("r1", {"status": "completed", "summary": "s", "check_results": [],
                                "artifacts": [], "duration_seconds": 1.0},
                         {"passed": True, "errors": []}, {"outcome": "passed", "rule": 6})
    assert ok is True
    assert db.set_run_error("r1", "timed_out", "late", None) is False
    row = db.get_run("r1")
    assert row["run_state"] == "terminal" and row["outcome"] == "passed"

    # timeout first → a later finalize is a no-op (timeout wins once fired).
    _running_run(db, "r2")
    assert db.set_run_error("r2", "timed_out", "deadline", None) is True
    assert db.finalize_run("r2", {"status": "cancelled", "summary": "s", "check_results": [],
                                  "artifacts": [], "duration_seconds": 1.0},
                           {"passed": True, "errors": []}, {"outcome": "cancelled", "rule": 1}) is False
    row = db.get_run("r2")
    assert row["run_state"] == "error" and row["error_kind"] == "timed_out" and row["result_json"] is None

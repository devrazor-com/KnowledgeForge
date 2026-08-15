"""Hostile Gateway behaviours over real HTTP (Step 2B).

Each case drives Module 1 against the mock (or a dead port) as separate processes
and asserts the run reaches a clear terminal ERROR state with the right
Module-1-authored `error_kind`, that no ValidationResult was fabricated
(result is None), and that unreachable/rejected (no Gateway run) is distinguished
from a mid-stream failure (Gateway run exists) via `gateway_run_created`.
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

import _regutil
from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def _start_run(wb_port: int, forced_outcome=None, fault=None) -> str:
    fields = [("source_id", _regutil.ensure_larkspur(wb_port)), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem")]
    if forced_outcome:
        fields.append(("forced_outcome", forced_outcome))
    if fault:
        fields.append(("fault", fault))
    body = urllib.parse.urlencode(fields).encode()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(f"http://127.0.0.1:{wb_port}/runs", data=body, method="POST"))
    except urllib.error.HTTPError as e:
        assert e.code == 303
        return e.headers["Location"].rsplit("/", 1)[-1]
    raise AssertionError("expected 303")


def _get(wb_port: int, run_id: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _poll_terminal(wb_port: int, run_id: str, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    view = _get(wb_port, run_id)
    while time.time() < end:
        view = _get(wb_port, run_id)
        if view["run_state"] in ("terminal", "error"):
            return view
        time.sleep(0.4)
    return view


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path_factory.mktemp("db") / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        yield wb_port
    finally:
        _stop(wb, mock)


# fault, expected error_kind, whether a Gateway run should exist
HOSTILE_CASES = [
    ("reject", "start_rejected", False),
    ("http_500", "gateway_http_error", True),
    ("http_404", "gateway_http_error", True),
    ("malformed", "protocol_error", True),
    ("invalid_event", "protocol_error", True),
    ("invalid_result", "protocol_error", True),
    ("seq_gap", "protocol_error", True),
    ("seq_dup", "protocol_error", True),
    ("seq_ooo", "protocol_error", True),
]


@pytest.mark.parametrize("fault,kind,gw_created", HOSTILE_CASES)
def test_hostile_case_reaches_clear_error_state(servers, fault, kind, gw_created):
    run_id = _start_run(servers, forced_outcome="success", fault=fault)
    view = _poll_terminal(servers, run_id)
    assert view["run_state"] == "error", f"{fault}: expected error, got {view['run_state']}"
    assert view["error_kind"] == kind, f"{fault}: expected {kind}, got {view['error_kind']}"
    assert view["result"] is None, f"{fault}: a ValidationResult was fabricated"
    assert view["outcome"] is None, f"{fault}: an error run carries no verdict outcome"
    assert view["gateway_run_created"] is gw_created, f"{fault}: gateway_run_created mismatch"
    assert view["error"], f"{fault}: missing human-readable detail"


def test_gateway_unreachable(tmp_path):
    # Point Module 1 at a port with nothing listening.
    dead_port = _free_port()
    wb_port = _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{dead_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        run_id = _start_run(wb_port)
        view = _poll_terminal(wb_port, run_id)
        assert view["run_state"] == "error"
        assert view["error_kind"] == "gateway_unreachable"
        assert view["gateway_run_created"] is False  # no run ever created on the Gateway
        assert view["result"] is None
    finally:
        _stop(wb)


def test_oversized_result_flows_through(servers):
    run_id = _start_run(servers, forced_outcome="check_failure_large")
    view = _poll_terminal(servers, run_id)
    assert view["run_state"] == "terminal"
    assert view["outcome"] == "failed"
    assert view["verdict"]["rule"] == 4
    # the large content is preserved intact end to end
    diag = view["result"]["diagnosis"]
    assert len(diag["recommendations"]) == 10
    assert len(diag["evidence_references"]) == 10
    assert len(view["result"]["check_results"][1]["output"]) > 800

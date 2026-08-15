"""Termination behaviour over real HTTP (Step 3A): cancellation (+ edges) and the
authoritative timeout, with the mock as a separate process.
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

import pytest

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _make_servers(tmp_path, timeout_seconds: int, guard_seconds: int):
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"
    env["WORKBENCH_TIMEOUT_SECONDS"] = str(timeout_seconds)
    env["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = str(guard_seconds)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    assert _wait_ready(mock_port) and _wait_ready(wb_port)
    return (wb, mock), wb_port, mock_port


def _post(port: int, path: str, fields=None):
    data = urllib.parse.urlencode(fields).encode() if fields else b""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST"))
        return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 303:
            return 303, e.headers["Location"]
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def _start_run(port, forced=None, fault=None):
    fields = [("source_id", _regutil.ensure_larkspur(port)), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem")]
    if forced:
        fields.append(("forced_outcome", forced))
    if fault:
        fields.append(("fault", fault))
    status, loc = _post(port, "/runs", fields)
    assert status == 303
    return loc.rsplit("/", 1)[-1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _poll(port, run_id, until, timeout=30.0):
    end = time.time() + timeout
    v = _get(port, f"/api/runs/{run_id}")
    while time.time() < end:
        v = _get(port, f"/api/runs/{run_id}")
        if until(v):
            return v
        time.sleep(0.3)
    return v


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="module")
def servers_long(tmp_path_factory):
    procs, wb, mock = _make_servers(tmp_path_factory.mktemp("long"), timeout_seconds=30, guard_seconds=1)
    try:
        yield wb
    finally:
        _stop(*procs)


@pytest.fixture(scope="module")
def servers_short(tmp_path_factory):
    procs, wb, mock = _make_servers(tmp_path_factory.mktemp("short"), timeout_seconds=2, guard_seconds=1)
    try:
        yield wb, mock
    finally:
        _stop(*procs)


# --- cancellation -------------------------------------------------------------

def test_cancel_midflight_gives_rule1_cancelled(servers_long):
    wb = servers_long
    run_id = _start_run(wb, forced="success")
    # wait until the run is genuinely in flight
    _poll(wb, run_id, lambda v: v["run_state"] == "running" and len(v["events"]) >= 1, timeout=15)
    status, resp = _post(wb, f"/runs/{run_id}/cancel")
    assert status == 200 and resp.get("ok") is True
    v = _poll(wb, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=15)
    assert v["run_state"] == "terminal"
    assert v["outcome"] == "cancelled"
    assert v["verdict"]["rule"] == 1
    assert v["result"]["status"] == "cancelled"


def test_cancel_already_terminal(servers_long):
    wb = servers_long
    run_id = _start_run(wb, fault="reject")           # terminal (error) almost immediately
    _poll(wb, run_id, lambda v: v["run_state"] in ("terminal", "error"), timeout=10)
    status, resp = _post(wb, f"/runs/{run_id}/cancel")
    assert status == 200 and resp.get("already_terminal") is True


def test_cancel_unknown_run(servers_long):
    status, _ = _post(servers_long, "/runs/does-not-exist/cancel")
    assert status == 404


def test_cancel_gateway_unreachable(tmp_path):
    """Cancel when the Gateway is unreachable. Originally (Step 3A) this checked only
    the backend response and that the run was not falsely terminated — it did NOT
    verify the operator could still act afterwards, nor how delivery knowledge was
    represented. That was the gap behind the button-hiding defect; this now also
    asserts the run stays cancellable and the delivery state is recorded."""
    procs, wb, mock_port = _make_servers(tmp_path, timeout_seconds=30, guard_seconds=1)
    wb_proc, mock_proc = procs
    try:
        run_id = _start_run(wb, forced="success")
        _poll(wb, run_id, lambda v: v["run_state"] == "running", timeout=15)
        _stop(mock_proc)                              # Gateway killed → connection refused
        status, resp = _post(wb, f"/runs/{run_id}/cancel")
        # A killed Gateway is provable non-delivery: 'undelivered', not a success.
        assert status == 200 and resp["delivery"] == "undelivered" and resp["ok"] is False
        assert "could not reach" in resp["message"].lower()
        # Gap now closed: run stays non-terminal, delivery knowledge recorded, and
        # the Cancel control remains available for a retry.
        v = _get(wb, f"/api/runs/{run_id}")
        assert v["run_state"] == "running"                     # not falsely terminated
        assert v["cancel_requested"] and v["cancel_delivery"] == "undelivered"
        assert v["cancel_note"]["delivery"] == "undelivered"
        with urllib.request.urlopen(f"http://127.0.0.1:{wb}/runs/{run_id}", timeout=5) as r:
            assert 'id="cancel-btn"' in r.read().decode()      # control still usable
    finally:
        _stop(wb_proc)


# --- timeout ------------------------------------------------------------------

def test_module1_timeout_is_authoritative(servers_short):
    wb, mock = servers_short
    run_id = _start_run(wb, forced="success", fault="never_terminal")
    # deadline ≈ accepted + 2 + 1 → times out within a few seconds
    v = _poll(wb, run_id, lambda v: v["run_state"] == "error", timeout=20)
    assert v["run_state"] == "error"
    assert v["error_kind"] == "timed_out"
    assert v["result"] is None                        # never fabricated
    assert v["outcome"] is None

    # Cleanup cancel reached the Gateway (mock now shows a cancelled result)...
    end = time.time() + 8
    mock_result = None
    while time.time() < end:
        mock_result = _get(mock, f"/runs/{run_id}/result").get("result")
        if mock_result:
            break
        time.sleep(0.3)
    assert mock_result and mock_result["status"] == "cancelled"

    # ...yet Module 1's timed_out disposition is unchanged (write-once held).
    again = _get(wb, f"/api/runs/{run_id}")
    assert again["run_state"] == "error" and again["error_kind"] == "timed_out"

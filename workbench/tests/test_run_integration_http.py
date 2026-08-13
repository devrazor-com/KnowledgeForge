"""Real-HTTP integration test — the network boundary that will exist between
Module 1 and Sadia's Module 3.

The mock Gateway and the Workbench each run as a SEPARATE process on a real local
port. Module 1 is pointed at the mock via MOD3_BASE_URL. A complete run is driven
over HTTP, and the final persisted state, per-message validation, and verdict are
verified. Not an in-process ASGI client — this exercises the actual socket.
"""

import json
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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _start(module: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_complete_run_over_real_http(tmp_path):
    import os
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"  # enables forced_outcome for a deterministic run

    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port), "mock gateway did not start"
        assert _wait_ready(wb_port), "workbench did not start"

        # Start a run over HTTP; read run_id from the 303 redirect (don't follow it).
        body = urllib.parse.urlencode([
            ("source_id", _regutil.ensure_larkspur(wb_port)), ("task", "LARK-TASK-001"),
            ("environment", "larkspur-sandbox"),
            ("capabilities", "filesystem"), ("capabilities", "shell"),
            ("forced_outcome", "success"),
        ]).encode()

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        run_id = None
        try:
            opener.open(urllib.request.Request(f"http://127.0.0.1:{wb_port}/runs", data=body, method="POST"))
        except urllib.error.HTTPError as e:
            assert e.code == 303, f"expected 303, got {e.code}"
            run_id = e.headers["Location"].rsplit("/", 1)[-1]
        assert run_id, "no run_id returned"

        # Mid-run: a fresh fetch returns already-persisted events while the run is
        # still going — this is the replay-from-persistence a browser refresh uses.
        time.sleep(1.3)
        mid = _get_json(wb_port, f"/api/runs/{run_id}")
        assert mid["run_state"] != "terminal", "run finished too fast to prove mid-run replay"
        assert len(mid["events"]) < 8, "expected a partial event set mid-run"

        # Poll to terminal.
        view = mid
        end = time.time() + 30
        while time.time() < end:
            view = _get_json(wb_port, f"/api/runs/{run_id}")
            if view["run_state"] == "terminal":
                break
            time.sleep(0.4)

        assert view["run_state"] == "terminal", "run did not reach terminal state"
        assert view["contract_status"] == "completed"
        assert view["outcome"] == "passed"
        assert view["verdict"]["rule"] == 6
        assert len(view["events"]) == 8
        assert all(e["m1_validation"]["passed"] for e in view["events"]), "an inbound event failed Module 1 validation"
        assert view["request_validation"]["passed"], "outbound request failed Module 1 validation"
        assert view["result_validation"]["passed"], "inbound result failed Module 1 validation"
        assert view["capabilities"] == ["filesystem", "shell"]
        assert view["target_environment"] == "larkspur-sandbox"

        # forced_outcome must never appear in the Module 2 request.
        assert "forced_outcome" not in view["request"]
        assert "forced_outcome" not in json.dumps(view["request"])

        # Replay again after completion: a fresh fetch still returns all events.
        again = _get_json(wb_port, f"/api/runs/{run_id}")
        assert len(again["events"]) == 8
    finally:
        for p in (wb, mock):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

"""Two timeout provenances, proven end to end over real HTTP and kept visibly
distinct in persistence and in the rendered UI (Step 3A, clarification 6):

  * Gateway-reported timeout — a valid ValidationResult (status failed, diagnosis
    category 'timeout') → verdict rule #2 → inconclusive. The real result is
    present; this is NEVER recorded as error_kind=timed_out.
  * Module 1 deadline timeout — no valid ValidationResult → run_state=error,
    error_kind=timed_out → effective inconclusive.
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

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _start_run(port, forced=None, fault=None):
    fields = [("source_id", _regutil.ensure_larkspur(port)), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem")]
    if forced:
        fields.append(("forced_outcome", forced))
    if fault:
        fields.append(("fault", fault))

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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _get_html(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.read().decode()


def _poll(port, run_id, until, timeout=20.0):
    end = time.time() + timeout
    v = _get(port, f"/api/runs/{run_id}")
    while time.time() < end:
        v = _get(port, f"/api/runs/{run_id}")
        if until(v):
            return v
        time.sleep(0.3)
    return v


def test_two_timeout_provenances_are_distinct(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"
    env["WORKBENCH_TIMEOUT_SECONDS"] = "4"      # deadline ≈ accepted + 4 + 1
    env["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb_proc = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)

        # (a) Gateway-reported timeout: completes well before the deadline.
        gid = _start_run(wb_port, forced="gateway_timeout")
        gv = _poll(wb_port, gid, lambda v: v["run_state"] in ("terminal", "error"), timeout=15)
        assert gv["run_state"] == "terminal", gv
        assert gv["outcome"] == "inconclusive"
        assert gv["verdict"]["rule"] == 2
        assert gv["result"] is not None and gv["result"]["status"] == "failed"
        assert gv["result"]["diagnosis"]["category"] == "timeout"
        assert gv["error_kind"] is None            # NOT a Module-1 timeout
        ghtml = _get_html(wb_port, f"/runs/{gid}")
        assert "Rule #2 applied." in ghtml         # verdict rendered
        assert "Category:" in ghtml and "timeout" in ghtml  # diagnosis rendered
        assert 'id="run-state">terminal' in ghtml

        # (b) Module 1 deadline timeout: no valid result.
        tid = _start_run(wb_port, forced="success", fault="never_terminal")
        tv = _poll(wb_port, tid, lambda v: v["run_state"] == "error", timeout=20)
        assert tv["error_kind"] == "timed_out"
        assert tv["result"] is None
        assert tv["outcome"] is None
        thtml = _get_html(wb_port, f"/runs/{tid}")
        assert 'id="run-state">error' in thtml
        assert "Rule #2 applied." not in thtml     # no verdict/result rendered
        assert "Available once the run is terminal." in thtml  # result stage empty

        # The two pages are visibly different: one carries a full ValidationResult
        # + rule #2, the other carries no result and is in the error state.
        assert ("Rule #2 applied." in ghtml) and ("Rule #2 applied." not in thtml)
    finally:
        _stop(wb_proc, mock)

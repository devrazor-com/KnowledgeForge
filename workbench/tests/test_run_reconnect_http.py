"""Mid-run SSE disconnect/reconnect regression test — over real HTTP.

The behaviour most likely to regress silently when Step 3 rewrites this machinery
for Last-Event-ID and poller restart. It exercises the Step 2A guarantee only:

  * the browser disconnects while the run is still in flight;
  * the server-side poller keeps going independently;
  * on reconnect, persisted events are replayed and live delivery continues;
  * the EFFECTIVE (client-deduplicated) event sequence has no loss and no
    duplicates, and the run reaches the correct result and verdict.

It deliberately does NOT require Step 3's server-side resume semantics: the SSE
server is allowed to re-send already-seen events after reconnect. What must hold
is the deduplicated, rendered result — exactly what app.js produces with its
`seen` set.
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


def _start(module: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _start_run(wb_port: int) -> str:
    body = urllib.parse.urlencode([
        ("dir_name", "larkspur"), ("task", "LARK-TASK-001"),
        ("environment", "larkspur-sandbox"),
        ("capabilities", "filesystem"), ("capabilities", "shell"),
        ("forced_outcome", "success"),
    ]).encode()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(f"http://127.0.0.1:{wb_port}/runs", data=body, method="POST"))
    except urllib.error.HTTPError as e:
        assert e.code == 303
        return e.headers["Location"].rsplit("/", 1)[-1]
    raise AssertionError("expected a 303 redirect with the run_id")


def _read_sse(wb_port: int, run_id: str, stop_after_events: int | None = None, overall_timeout: float = 25.0):
    """Read the SSE stream. Returns (event_sequences, saw_done, verdict). If
    stop_after_events is set, disconnect immediately after that many events."""
    resp = urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/runs/{run_id}/stream", timeout=overall_timeout)
    seqs, saw_done, verdict, cur = [], False, None, None
    start = time.time()
    try:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                if cur == "event":
                    seqs.append(data["event"]["sequence"])
                    if stop_after_events and len(seqs) >= stop_after_events:
                        return seqs, saw_done, verdict          # disconnect mid-run
                elif cur == "result":
                    verdict = data.get("verdict")
                elif cur == "done":
                    return seqs, True, verdict
            if time.time() - start > overall_timeout:
                break
    finally:
        resp.close()
    return seqs, saw_done, verdict


def test_mid_run_reconnect_replays_without_loss_or_duplicates(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = os.environ.copy()
    env["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    env["WORKBENCH_DEV_MOCK"] = "1"

    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port), "mock gateway did not start"
        assert _wait_ready(wb_port), "workbench did not start"

        run_id = _start_run(wb_port)

        # Connection #1: take three events, then disconnect while the run is active.
        c1_seqs, c1_done, _ = _read_sse(wb_port, run_id, stop_after_events=3)
        assert c1_done is False, "client saw the run finish before we disconnected"
        assert len(c1_seqs) == 3

        # Reconnect and read to completion.
        c2_seqs, c2_done, verdict = _read_sse(wb_port, run_id)
        assert c2_done is True, "reconnected stream never reached the terminal 'done'"

        # Effective view = what app.js renders after its dedup `seen` set.
        seen, rendered = set(), []
        for s in c1_seqs + c2_seqs:
            if s not in seen:
                seen.add(s)
                rendered.append(s)

        final = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}").read())
        expected = list(range(1, len(final["events"]) + 1))

        # 1. run was still in flight at disconnect
        assert len(c1_seqs) < len(expected)
        # 2. persisted events replayed on reconnect (server re-sent already-seen ones)
        assert set(c1_seqs) & set(c2_seqs), "no events were replayed on reconnect"
        # 3 & 4. effective sequence: no loss (contiguous 1..N) and no duplicates
        assert rendered == expected, f"effective sequence {rendered} != {expected}"
        assert len(rendered) == len(set(rendered)), "duplicate event in the effective sequence"
        # 5. reached terminal result
        assert final["run_state"] == "terminal"
        # 6. correct result and verdict
        assert final["outcome"] == "passed"
        assert (verdict or {}).get("rule") == 6
    finally:
        for p in (wb, mock):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

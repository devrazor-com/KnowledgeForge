"""Formal SSE resume via Last-Event-ID (Step 3B-2), asserted against the RAW server
stream over real HTTP — before any browser/client deduplication. This proves the
Workbench endpoint itself honours the resume cursor, not that JavaScript happened
to hide duplicates.

Guarantee under test (narrow): a valid `Last-Event-ID: N` yields only ExecutionEvents
with sequence > N; the endpoint does not deliberately re-send persisted events at or
below N. A fresh connection (no header) still receives the full persisted history.
Not an exactly-once distributed-delivery claim.
"""

import http.client
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


def _env(mock_port, dbpath):
    e = os.environ.copy()
    e["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
    e["WORKBENCH_TIMEOUT_SECONDS"] = "60"
    e["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
    return e


def _post_run(wb_port, forced="success"):
    fields = [("source_id", _regutil.ensure_larkspur(wb_port)), ("task", "LARK-TASK-001"),
              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem"), ("forced_outcome", forced)]

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
        time.sleep(0.2)
    return v


# --- raw SSE reading (no client library, no dedup) ---------------------------

def _stream_conn(wb_port, run_id, last_event_id=None, read_timeout=2.0):
    """Open the SSE endpoint as a raw HTTP connection. `last_event_id=None` sends NO
    header (fresh connection); an int sends `Last-Event-ID: <n>` (native reconnect)."""
    conn = http.client.HTTPConnection("127.0.0.1", wb_port, timeout=10)
    conn.putrequest("GET", f"/runs/{run_id}/stream")
    conn.putheader("Accept", "text/event-stream")
    if last_event_id is not None:
        conn.putheader("Last-Event-ID", str(last_event_id))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.headers.get("content-type", "").startswith("text/event-stream")
    conn.sock.settimeout(read_timeout)
    return conn, resp


def _read_frames(resp, stop, max_seconds=25):
    """Parse raw SSE frames (id/event/data) until stop(frames) is True, the server
    closes, or the overall budget expires. No deduplication whatsoever."""
    frames, cur = [], {}
    end = time.time() + max_seconds
    while time.time() < end:
        try:
            raw = resp.fp.readline()
        except (socket.timeout, TimeoutError):
            if stop(frames):
                break
            continue
        if raw == b"":                       # server closed the stream
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":                       # blank line terminates a frame
            if cur:
                frames.append(cur); cur = {}
                if stop(frames):
                    break
            continue
        if line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"] = line[5:].strip()
    return frames


def _event_frames(frames):
    return [f for f in frames if f.get("event") == "event" and "data" in f]


def _event_seqs(frames):
    return [json.loads(f["data"])["event"]["sequence"] for f in _event_frames(frames)]


def _has_done(frames):
    return any(f.get("event") == "done" for f in frames)


# --- tests -------------------------------------------------------------------

def test_raw_sse_resume_only_sends_events_after_cursor(tmp_path):
    """1..N over one connection → reconnect with Last-Event-ID:N → the RAW second
    stream contains only sequences > N, contiguous, with no server-side overlap, and
    the run reaches the correct final verdict."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, "success")

        # Connection 1 (no Last-Event-ID): replays from the very start.
        c1, r1 = _stream_conn(wb_port, run_id, None)
        f1 = _read_frames(r1, lambda fr: len(_event_seqs(fr)) >= 2, max_seconds=15)
        c1.close()
        seqs1 = _event_seqs(f1)
        assert seqs1[:2] == [1, 2]                                  # fresh conn began at the start
        # The id: line equals the ExecutionEvent sequence (the resume cursor source).
        assert [int(f["id"]) for f in _event_frames(f1)][:2] == [1, 2]
        N = seqs1[-1]

        # Connection 2 (Last-Event-ID: N): must resume strictly after N.
        c2, r2 = _stream_conn(wb_port, run_id, N)
        f2 = _read_frames(r2, _has_done, max_seconds=30)
        c2.close()
        seqs2 = _event_seqs(f2)
        assert seqs2, "resume delivered no events"
        assert min(seqs2) > N                                       # nothing at/below the cursor re-sent
        assert seqs2 == list(range(N + 1, N + 1 + len(seqs2)))      # contiguous tail
        assert set(seqs1) & set(seqs2) == set()                     # no server-side replay overlap
        combined = sorted(set(seqs1) | set(seqs2))
        assert combined == list(range(1, combined[-1] + 1))         # combined raw stream is contiguous 1..last

        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "terminal", 15)
        assert v["outcome"] == "passed" and v["verdict"]["rule"] == 6
    finally:
        _stop(wb, mock)


def test_raw_sse_fresh_connection_replays_full_history(tmp_path):
    """A connection with NO Last-Event-ID header still receives the complete persisted
    ExecutionEvent history from sequence 1 (full replay from cursor 0)."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, "success")
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "terminal", 20)
        last = len(v["events"])
        assert last >= 3

        c, r = _stream_conn(wb_port, run_id, None)                  # fresh page → no header
        f = _read_frames(r, _has_done, max_seconds=15)
        c.close()
        seqs = _event_seqs(f)
        assert seqs == list(range(1, last + 1))                     # full history, from the start
        assert _has_done(f)
    finally:
        _stop(wb, mock)


def test_raw_sse_cursor_above_max_sends_no_events(tmp_path):
    """A cursor above the current max waits rather than fabricating anything: on a
    terminal run, resuming past the last sequence yields no ExecutionEvents but still
    delivers the terminal result/done."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        run_id = _post_run(wb_port, "success")
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "terminal", 20)
        last = len(v["events"])

        c, r = _stream_conn(wb_port, run_id, last + 50)             # cursor beyond the end
        f = _read_frames(r, _has_done, max_seconds=15)
        c.close()
        assert _event_seqs(f) == []                                # nothing at/below (or fabricated)
        assert any(fr.get("event") == "result" for fr in f) and _has_done(f)
    finally:
        _stop(wb, mock)

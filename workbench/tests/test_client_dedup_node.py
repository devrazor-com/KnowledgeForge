"""Client defensive dedup (Step 3B-2) — INDEPENDENT of server resume correctness.

With the Last-Event-ID resume cursor the server no longer re-sends acknowledged
ExecutionEvents, so the client `seen` guard is no longer exercised during normal
reconnect. It stays as defence-in-depth: if a duplicate sequence nevertheless
reaches the JavaScript rendering path, each sequence must still render exactly once.

This exercises the REAL dedup seam (workbench/static/dedup.js) in actual JavaScript
via a Node subprocess — no browser, no Python C-extension JS engine, no production
dependency. If Node is not installed the test SKIPS (it must never fail the
clean-clone verification over an optional test-only runtime).

It deliberately does NOT touch the SSE server: server resume correctness is proven
separately in test_sse_resume_http.py, and neither test may rely on the other.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DEDUP_JS = Path(__file__).resolve().parents[1] / "static" / "dedup.js"


def test_client_dedup_renders_each_sequence_once():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; client dedup seam exercised only when Node is present")

    # Feed a raw stream containing duplicates (2, 4 and 1 repeated) straight through
    # the real shouldRenderEvent seam; only first-seen sequences should 'render'.
    harness = (
        "const {shouldRenderEvent} = require(%s);"
        "const seen = new Set();"
        "const incoming = [1, 2, 3, 2, 4, 4, 5, 1, 3];"
        "const rendered = [];"
        "for (const seq of incoming) { if (shouldRenderEvent(seen, seq)) rendered.push(seq); }"
        "process.stdout.write(JSON.stringify(rendered));"
    ) % json.dumps(str(DEDUP_JS))

    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    # Each sequence rendered exactly once, in first-seen order; every duplicate suppressed.
    assert json.loads(out.stdout) == [1, 2, 3, 4, 5]

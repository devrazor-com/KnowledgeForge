"""Help page + vocabulary synchronisation (Step 3C-3).

The Help page must mention exactly the outcome values, error kinds, and cancel-delivery
states the implementation defines — so a future vocabulary change cannot leave Help
stale. Also cross-checks the canonical vocab against the live code that actually uses it.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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


def test_help_mentions_every_vocabulary_token():
    from workbench import vocab
    port = _free_port()
    env = os.environ.copy(); env["WORKBENCH_DB"] = "/tmp/kf_help_test.db"
    wb = subprocess.Popen([sys.executable, "-m", "uvicorn", "workbench.app:app", "--port", str(port),
                           "--log-level", "warning"], cwd=str(REPO_ROOT), env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_ready(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/help", timeout=5) as r:
            html = r.read().decode()
        for token in list(vocab.OUTCOMES) + list(vocab.ERROR_KINDS) + list(vocab.CANCEL_DELIVERY_STATES):
            assert token in html, f"Help page does not mention '{token}'"
        # Non-obvious semantics that must be spelled out.
        assert "acknowledged" in html and "does not mean the run is cancelled" in html
        assert "Two provenances" in html
        assert "references only" in html
        # A Help link is present in the nav.
        assert 'href="/help"' in urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    finally:
        wb.terminate(); wb.wait(timeout=5)


def test_vocab_matches_live_code():
    """The canonical vocab must not drift from the code that actually uses it."""
    from workbench import orchestrator, vocab, verdict
    # Cancel-delivery states == the orchestrator's operator-facing message keys.
    assert set(vocab.CANCEL_DELIVERY_STATES) == set(orchestrator.CANCEL_DELIVERY_MESSAGES)
    # Every verdict outcome the engine can emit is a documented outcome value.
    emitted = set()
    for status in ("cancelled", "failed", "completed"):
        for checks in ([], [{"id": "C", "description": "d", "command": "c"}]):
            req = {"task": {"checks": checks}}
            res = {"status": status, "check_results": []}
            emitted.add(verdict.derive_verdict(req, res)["outcome"])
    assert emitted <= set(vocab.OUTCOMES)

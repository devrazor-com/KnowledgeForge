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

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_help_mentions_every_vocabulary_token(tmp_path):
    from workbench import vocab
    port = _free_port()
    env = os.environ.copy(); env["WORKBENCH_DB"] = str(tmp_path / "kf_help_test.db")
    wb = _start("workbench.app:app", port, env)
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
        _stop(wb)


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

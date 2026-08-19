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


# The operator-guide section anchors that must exist. Structural (an id), not prose:
# a removed or renamed section fails here, prose wording stays owned by human review.
_REQUIRED_ANCHORS = (
    "what-for", "quick-start", "workflow", "packages", "profiles", "environments",
    "tasks", "starting", "monitoring", "results", "current-stale", "fingerprints",
    "cancellation", "recovery", "history", "configuration", "troubleshooting",
    "authoring", "operator-best-practices",
)

# Operator concepts identified in the UI inventory that Help must represent. These are
# machine-defined tokens / configuration surface, not sentences — semantic, not brittle.
_REQUIRED_CONCEPTS = (
    "WORKBENCH_ENVIRONMENTS_FILE", "MOD3_BASE_URL", "fail-closed", "no longer configured",
    "validation context", "override", "package_id", "package_fingerprint",
    "not_validated", "revalidation_required",          # per-task statuses w/o a UI affordance elsewhere
    "missing", "cycle", "outside_root",                # discovery problem kinds
    "Change root", "historical readability",
)


def test_help_mentions_every_vocabulary_token(tmp_path):
    from workbench import vocab
    port = _free_port()
    env = os.environ.copy(); env["WORKBENCH_DB"] = str(tmp_path / "kf_help_test.db")
    wb = _start("workbench.app:app", port, env)
    try:
        assert _wait_ready(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/help", timeout=5) as r:
            html = r.read().decode()
        # Every machine-defined operator outcome / error kind / cancel-delivery state.
        for token in list(vocab.OUTCOMES) + list(vocab.ERROR_KINDS) + list(vocab.CANCEL_DELIVERY_STATES):
            assert token in html, f"Help page does not mention '{token}'"
        # Non-obvious semantics that must be spelled out.
        assert "acknowledged" in html and "does not mean the run is cancelled" in html
        assert "Two provenances" in html
        assert "references only" in html
        # Required operator-guide sections exist (structural anchors).
        for anchor in _REQUIRED_ANCHORS:
            assert f'id="{anchor}"' in html, f"Help page is missing section anchor '{anchor}'"
        # Required operator concepts are represented.
        for concept in _REQUIRED_CONCEPTS:
            assert concept in html, f"Help page does not represent concept '{concept}'"
        # A Help link is present in the nav.
        assert 'href="/help"' in urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    finally:
        _stop(wb)


def test_help_excludes_frozen_future_gateway_topics(tmp_path):
    """Cheap negative guards: Help must not present frozen future Gateway behaviour as
    current Module 1 behaviour. These are deliberately simple string checks (not coupled
    to prose) — `/alive` has no current Module 1 behaviour, and a 409 duplicate-run_id
    interpretation is future reconciliation work, neither of which exists today."""
    port = _free_port()
    env = os.environ.copy(); env["WORKBENCH_DB"] = str(tmp_path / "kf_help_neg.db")
    wb = _start("workbench.app:app", port, env)
    try:
        assert _wait_ready(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/help", timeout=5) as r:
            html = r.read().decode()
        assert "/alive" not in html, "Help must not mention /alive (no current Module 1 behaviour)"
        assert "409" not in html, "Help must not present 409 duplicate-run_id (frozen future behaviour)"
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

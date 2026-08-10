"""Exercise Module 1 against the canonical messages in contract/examples/ — the
shared source of truth — as well as against the mock elsewhere. Confirms our
validators accept the canonical messages and our verdict engine produces the
documented outcomes.
"""

import json

from workbench import contract
from workbench.config import REPO_ROOT
from workbench.verdict import derive_verdict

EX = REPO_ROOT / "contract" / "examples"


def _load(name):
    return json.loads((EX / name).read_text(encoding="utf-8"))


def test_canonical_request_validates():
    assert contract.validate_request(_load("request-larkspur-task-001.json"))["passed"]


def test_canonical_events_validate():
    for ev in _load("events-pass.json"):
        assert contract.validate_event(ev)["passed"]


def test_canonical_results_validate():
    for name in ("result-pass.json", "result-knowledge-gap.json", "result-technical-failure.json"):
        assert contract.validate_result(_load(name))["passed"], name


def test_canonical_verdicts():
    request = _load("request-larkspur-task-001.json")  # declares CHK-MIGRATE + CHK-TESTS
    assert derive_verdict(request, _load("result-pass.json"))["outcome"] == "passed"
    # checks declared but none ran -> needs_review (rule 5)
    v = derive_verdict(request, _load("result-knowledge-gap.json"))
    assert (v["outcome"], v["rule"]) == ("needs_review", 5)
    assert derive_verdict(request, _load("result-technical-failure.json"))["outcome"] == "inconclusive"

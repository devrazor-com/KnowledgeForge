"""All six verdict rules at the engine level (VER-1…VER-5, ground rule 6).

Checks-declared is read from the request's task; "did not run" means no entry or
exit_code null; Rule 4 is evaluated before Rule 5.
"""

from workbench.verdict import derive_verdict

REQ_CHECKS = {"task": {"checks": [{"id": "CHK-MIGRATE", "description": "m", "command": "c"},
                                  {"id": "CHK-TESTS", "description": "t", "command": "c"}]}}
REQ_NO_CHECKS = {"task": {"checks": []}}


def _v(req, result):
    return derive_verdict(req, result)


def test_rule1_cancelled():
    r = _v(REQ_CHECKS, {"status": "cancelled", "check_results": []})
    assert (r["outcome"], r["rule"]) == ("cancelled", 1)


def test_rule2_failed_is_inconclusive():
    r = _v(REQ_CHECKS, {"status": "failed", "check_results": []})
    assert (r["outcome"], r["rule"]) == ("inconclusive", 2)


def test_rule3_no_checks_declared():
    r = _v(REQ_NO_CHECKS, {"status": "completed", "check_results": []})
    assert (r["outcome"], r["rule"]) == ("needs_review", 3)


def test_rule4_a_check_ran_and_failed():
    r = _v(REQ_CHECKS, {"status": "completed", "check_results": [
        {"check_id": "CHK-MIGRATE", "passed": True, "exit_code": 0},
        {"check_id": "CHK-TESTS", "passed": False, "exit_code": 1}]})
    assert (r["outcome"], r["rule"]) == ("failed", 4)


def test_rule4_beats_rule5_when_a_sibling_did_not_run():
    # One check ran-and-failed, the other never ran → still failed (rule 4 first).
    r = _v(REQ_CHECKS, {"status": "completed", "check_results": [
        {"check_id": "CHK-TESTS", "passed": False, "exit_code": 1}]})
    assert (r["outcome"], r["rule"]) == ("failed", 4)


def test_rule5_declared_but_no_results():
    r = _v(REQ_CHECKS, {"status": "completed", "check_results": []})
    assert (r["outcome"], r["rule"]) == ("needs_review", 5)


def test_rule5_exit_code_null_counts_as_did_not_run():
    r = _v(REQ_CHECKS, {"status": "completed", "check_results": [
        {"check_id": "CHK-MIGRATE", "passed": True, "exit_code": 0},
        {"check_id": "CHK-TESTS", "passed": False, "exit_code": None}]})
    assert (r["outcome"], r["rule"]) == ("needs_review", 5)


def test_rule6_all_passed():
    r = _v(REQ_CHECKS, {"status": "completed", "check_results": [
        {"check_id": "CHK-MIGRATE", "passed": True, "exit_code": 0},
        {"check_id": "CHK-TESTS", "passed": True, "exit_code": 0}]})
    assert (r["outcome"], r["rule"]) == ("passed", 6)

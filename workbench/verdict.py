"""Verdict derivation — Module 1's logic alone (VER-1…VER-5).

The six ordered rules, evaluated first-match-wins. "Checks declared" is read from
the task in the ValidationRequest that was sent, never inferred from whether
check_results is empty. "Did not run" means no entry in check_results for a
declared check, or an entry whose exit_code is null.

Ported verbatim from the proven POC engine. The reasoning list drives the UI:
each entry has a `sym` (ok / bad / info / rule) and human text.
"""

from __future__ import annotations


def derive_verdict(request: dict, result: dict) -> dict:
    status = result.get("status")
    declared = list((request.get("task") or {}).get("checks") or [])
    by_id = {c.get("check_id"): c for c in (result.get("check_results") or [])}

    def did_not_run(check_id: str) -> bool:
        c = by_id.get(check_id)
        return c is None or c.get("exit_code") is None

    def ran_and_failed(check_id: str) -> bool:
        c = by_id.get(check_id)
        return c is not None and c.get("exit_code") is not None and c.get("passed") is False

    # Rule 1
    if status == "cancelled":
        return {"outcome": "cancelled", "rule": 1, "reasoning": [
            {"sym": "info", "text": "Run status is 'cancelled' — someone stopped the run."},
            {"sym": "rule", "text": "Rule #1 applied."},
        ]}
    # Rule 2
    if status == "failed":
        return {"outcome": "inconclusive", "rule": 2, "reasoning": [
            {"sym": "info", "text": "Run status is 'failed' — a technical failure; the run did not complete."},
            {"sym": "info", "text": "The package was never really tested, so this is neither for nor against it."},
            {"sym": "rule", "text": "Rule #2 applied."},
        ]}
    # From here the run completed.
    # Rule 3
    if not declared:
        return {"outcome": "needs_review", "rule": 3, "reasoning": [
            {"sym": "info", "text": "No validation checks were declared in the submitted task."},
            {"sym": "rule", "text": "Rule #3 applied."},
        ]}

    ran = [c["id"] for c in declared if not did_not_run(c["id"])]
    failed = [c["id"] for c in declared if ran_and_failed(c["id"])]
    not_run = [c["id"] for c in declared if did_not_run(c["id"])]

    # Rule 4 (before Rule 5): a check that ran and failed is real evidence.
    if failed:
        reasoning = [
            {"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."},
            {"sym": "ok", "text": f"{len(ran)} check(s) executed."},
        ]
        for cid in failed:
            reasoning.append({"sym": "bad", "text": f"{cid} ran and failed (exit {by_id[cid].get('exit_code')})."})
        reasoning.append({"sym": "rule", "text": "Rule #4 applied."})
        return {"outcome": "failed", "rule": 4, "reasoning": reasoning}

    # Rule 5: some declared check did not run.
    if not_run:
        reasoning = [{"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."}]
        if ran:
            reasoning.append({"sym": "ok", "text": f"{len(ran)} declared check(s) ran and passed."})
        for cid in not_run:
            reasoning.append({"sym": "info", "text": f"{cid} did not run (no result, or exit_code is null)."})
        reasoning.append({"sym": "rule", "text": "Rule #5 applied."})
        return {"outcome": "needs_review", "rule": 5, "reasoning": reasoning}

    # Rule 6: all declared checks ran and passed.
    return {"outcome": "passed", "rule": 6, "reasoning": [
        {"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."},
        {"sym": "ok", "text": f"All {len(declared)} declared check(s) ran and passed."},
        {"sym": "rule", "text": "Rule #6 applied."},
    ]}

"""Current interpretation layered over immutable evidence (Step 3C-3): the
validation-context identity, staleness, review-aware effective outcome, per-task
status, approval eligibility, and approval currency.

The validation context is package fingerprint + task fingerprint + canonical
permitted capability set + target environment (the extended model — see
REQUIREMENTS_CLARIFICATIONS.md: HST-2/APR-3/APR-4 literally name only package/task
fingerprints; this stricter interpretation adds the profile's environment and
capabilities). Capability ordering is canonicalised and never itself causes
staleness. Nothing here mutates evidence; it only derives current status.
"""

from __future__ import annotations

import hashlib
import json

from workbench import db


def validation_context_id(package_fp: str | None, task_fp: str | None,
                          capabilities, environment: str | None) -> str:
    """Deterministic identity for a validation context. Capabilities are sorted and
    de-duplicated, so {filesystem, network} == {network, filesystem}."""
    caps = "␟".join(sorted(set(capabilities or [])))
    raw = "\n".join(["vctx-1", package_fp or "", task_fp or "", caps, environment or ""])
    return "vctx:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def run_context_id(run: dict) -> str:
    """The validation context a run actually executed against, from its immutable fields."""
    caps = json.loads(run["capabilities_json"]) if run.get("capabilities_json") else run.get("capabilities") or []
    return validation_context_id(run.get("package_fingerprint"), run.get("task_fingerprint"),
                                 caps, run.get("target_environment"))


def effective_outcome(run: dict, review: dict | None) -> tuple[str, str]:
    """(outcome, source) where source is 'machine' | 'review' | 'error'. A needs_review
    machine verdict resolved by a human takes the human resolution (VER-6/VER-7); the
    mechanical verdict is preserved separately and never overwritten."""
    if run.get("run_state") == "error":
        return ("inconclusive", "error")
    verdict = json.loads(run["verdict_json"]) if run.get("verdict_json") else None
    outcome = verdict["outcome"] if verdict else run.get("outcome")
    if outcome == "needs_review" and review:
        return (review["resolution"], "review")
    return (outcome or "unknown", "machine")


def _current_context_for_task(current_pkg_fp: str | None, task_fp: str | None,
                              profile: dict | None) -> str | None:
    if not current_pkg_fp or not profile:
        return None
    return validation_context_id(current_pkg_fp, task_fp, profile["capabilities"], profile["target_environment"])


def run_current_status(run: dict, current_pkg_fp: str | None, profile: dict | None,
                       review: dict | None) -> dict:
    """Derived current status of a single run for the evidence view."""
    outcome, source = effective_outcome(run, review)
    current_ctx = _current_context_for_task(current_pkg_fp, run.get("task_fingerprint"), profile)
    run_ctx = run_context_id(run)
    is_current = current_ctx is not None and run_ctx == current_ctx
    return {"effective_outcome": outcome, "outcome_source": source,
            "review": review, "run_context_id": run_ctx, "current_context_id": current_ctx,
            "context_comparable": current_ctx is not None, "is_current": is_current,
            "is_stale": current_ctx is not None and not is_current}


def package_status(package_id: str | None, current_pkg_fp: str | None,
                   tasks: list, profile: dict | None) -> dict:
    """Per-task current status, approval eligibility (APR-1, extended), and whether the
    latest recorded approval still applies (APR-4, derived). `tasks` are the current
    assembled tasks (with .id, .fingerprint, .active). Historical evidence is untouched."""
    runs = db.runs_for_package(package_id) if package_id else []
    reviews = db.reviews_for_runs([r["run_id"] for r in runs])
    active_tasks = [t for t in tasks if getattr(t, "active", True)]

    task_rows, qualifying_runs, unmet = [], [], 0
    for t in tasks:
        task_runs = [r for r in runs if r["task_id"] == t.id]            # newest-first
        current_ctx = _current_context_for_task(current_pkg_fp, t.fingerprint, profile)
        row = {"task": t, "status": "not_validated", "qualifies": False,
               "effective_outcome": None, "outcome_source": None, "run_id": None}

        # Approval considers ONLY runs whose validation context matches the CURRENT
        # context (current package fp + task fp + capability set + environment). Runs
        # under an override or a superseded context are NOT candidates — they neither
        # qualify nor disqualify current evidence (they stay fully visible in history).
        candidates = [r for r in task_runs
                      if current_ctx is not None and run_context_id(r) == current_ctx]

        # Within the current-context candidates, newest-first: inconclusive/cancelled are
        # skipped (established nothing / abandoned); an UNRESOLVED needs_review stops the
        # scan and blocks approval (current evidence awaiting human judgment); a resolved
        # needs_review uses the human passed/failed; the first passed/failed decides.
        decided = False
        for r in candidates:
            oc, src = effective_outcome(r, reviews.get(r["run_id"]))
            if oc in ("inconclusive", "cancelled"):
                continue
            row.update(run_id=r["run_id"], effective_outcome=oc, outcome_source=src)
            if oc == "needs_review":                    # unresolved → review pending, blocks
                row["status"] = "needs_review"; row["needs_review"] = True
            elif oc == "passed":
                row["status"] = "passing_current"; row["qualifies"] = True
                qualifying_runs.append(r["run_id"])
            else:                                        # failed (incl. resolved-failed)
                row["status"] = "failed"
            decided = True
            break

        if not decided:
            if candidates:                              # current attempts, all non-decisive
                latest_c = candidates[0]
                oc, src = effective_outcome(latest_c, reviews.get(latest_c["run_id"]))
                row.update(run_id=latest_c["run_id"], effective_outcome=oc, outcome_source=src)
                row["status"] = "cancelled" if oc == "cancelled" else "inconclusive"
            elif task_runs:                             # runs exist, but none match current context
                row["status"] = "revalidation_required"
            # else: no runs at all → not_validated (the default)
        task_rows.append(row)
        if t.active and not row["qualifies"]:
            unmet += 1

    # Eligibility (APR-1, extended): a profile exists, there is >=1 active task, and
    # every active task has a latest passed, context-current (non-stale) run.
    if profile is None:
        eligible, reason = False, "This package has no validation profile configured yet."
    elif not active_tasks:
        eligible, reason = False, "This package has no active tasks."
    elif unmet:
        eligible, reason = False, f"{unmet} of {len(active_tasks)} active task(s) have no current passing run."
    else:
        eligible, reason = True, "Every active task has a current passing run."

    # Approval currency (APR-4, derived — never mutates the stored approval).
    appr = db.latest_approval(package_id)
    appr_current = False
    if appr and profile and current_pkg_fp:
        appr_current = (appr["package_fingerprint"] == current_pkg_fp
                        and set(appr["task_fingerprints"]) == {t.fingerprint for t in active_tasks}
                        and appr["target_environment"] == profile["target_environment"]
                        and appr["capabilities"] == sorted(set(profile["capabilities"])))

    return {"task_rows": task_rows, "active_task_count": len(active_tasks),
            "eligible": eligible, "eligibility_reason": reason,
            "qualifying_runs": qualifying_runs, "approval": appr, "approval_current": appr_current,
            "active_task_fingerprints": sorted(t.fingerprint for t in active_tasks)}

"""Canonical Module 1 vocabulary — the single source the Help page renders and the
Help-sync test checks, so a vocabulary change cannot silently leave Help stale.
"""

# Verdict / effective outcomes (VER-2).
OUTCOMES = {
    "passed": "All declared checks ran and passed (rule 6).",
    "failed": "A declared check ran and failed (rule 4).",
    "needs_review": "No decisive machine evidence — no checks were declared (rule 3) or a "
                    "declared check did not run (rule 5). A named human records passed/failed.",
    "inconclusive": "The package was not really tested, so it counts neither for nor against it. "
                    "Two provenances: Module 3 reported a technical failure (valid result, rule 2), "
                    "OR Module 1 could not obtain/validate a usable result (an error run).",
    "cancelled": "Someone stopped the run before it produced a validating result (rule 1).",
}

# Module-1-authored error kinds (a run that reached a terminal error state, no ValidationResult).
ERROR_KINDS = {
    "gateway_unreachable": "Module 3 could not be reached over the network.",
    "start_rejected": "Module 3 rejected the ValidationRequest at start; no run was created.",
    "gateway_http_error": "Module 3 returned an HTTP error (e.g. 5xx) that persisted.",
    "protocol_error": "Module 3 sent something that broke the contract — malformed JSON, a "
                      "schema-invalid event/result, or a sequence anomaly.",
    "request_invalid": "Module 1's own outbound ValidationRequest failed schema validation; not sent.",
    "timed_out": "Module 1's authoritative run deadline expired before completion.",
    "start_unresolved": "Interrupted around start; Module 1 cannot tell whether Module 3 created a "
                        "run, so it will not retry (avoids duplicate execution).",
}

# Operator cancellation-DELIVERY states (distinct from the run being cancelled).
CANCEL_DELIVERY_STATES = {
    "unknown": "Requested, but Module 1 cannot determine whether Module 3 received or acted on it.",
    "undelivered": "Positive evidence the request did not reach Module 3 (connection refused / DNS).",
    "rejected": "Module 3 received the cancellation request and declined it (HTTP 4xx).",
    "acknowledged": "Module 3 acknowledged the cancellation request (HTTP 2xx). This does NOT mean "
                    "the run is cancelled — only the contract cancelled event/result and verdict "
                    "rule #1 mean that.",
}

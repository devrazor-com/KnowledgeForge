# Requirement clarifications

Interpretations agreed during implementation that affect how a numbered
requirement is read. Recorded here so the traceability table doesn't drift across
sessions. Keep entries short.

Two kinds of entry live here: **settled clarifications** (approved, treat as
decided) and **open items** (explicitly unresolved, to be decided in a later
step). Open items are NOT settled clarifications; they are parked here so they are
not forgotten.

## EXE-8 — "Every run produces a result, including runs that fail technically or are cancelled."

**Clarification (Step 2B):** Module 1 does **not** satisfy EXE-8 by fabricating a
`ValidationResult`. A Module 2 `ValidationResult` exists only when the Gateway
returns a valid one.

For transport or protocol failures — where no valid `ValidationResult` is ever
received — Module 1's guarantee is instead that **every local validation attempt
reaches a terminal Workbench error state**: the `run` row ends with
`run_state = error`, a Module-1-authored `error_kind`, and an explanation, with
`result_json` left NULL. The run is Module 1's local attempt record; it may exist
even when Module 3 never created a run (`gateway_ack` is the authoritative
indication that a Gateway run exists).

So EXE-8's "every run produces a result" is read as: *every run reaches a durable
terminal outcome the operator can see* — a real `ValidationResult` when one is
received, or an authored error state when one is not. The two are visibly
distinct in the UI and in history.

## Verdict outcomes for error runs

An error run carries an **effective** approval outcome of `inconclusive` (it
counts neither for nor against the package, like a Gateway-reported technical
failure). This reuses one of the five contract-defined verdict outcomes rather
than inventing a sixth. The distinction is preserved and shown:

- valid Gateway `ValidationResult` with `status = failed` → verdict rule #2 → `inconclusive`;
- no valid `ValidationResult` → `run_state = error` + `error_kind = …` → **effective** `inconclusive`.

---

## Timeout policy — one authoritative deadline (Step 3A, settled)

The run deadline is **`accepted_at` (Gateway acceptance) + `execution_context.
timeout_seconds` from the SENT request + a small guard margin** — one number,
traceable to the request. `execution_context.timeout_seconds` is the Gateway's
execution budget after acceptance (GW-11 enforces; EXE-7 makes Module 1 the
backstop). Individual Gateway calls and 5xx retries are bounded by the remaining
time to the deadline, so nothing can silently overrun it. The per-call socket
timeout (`GATEWAY_HTTP_TIMEOUT`) bounds one network call only; the old
120-second poller cap is removed. On breach the run is terminal `timed_out`
(effective `inconclusive`), **write-once**, with a fire-and-forget cleanup cancel
on its own `GATEWAY_CANCEL_CLEANUP_TIMEOUT` budget. This resolves the earlier
open item about competing backstops.

**Provenance is distinguished**: Module 1's deadline breach → `run_state = error`,
`error_kind = timed_out`, no `ValidationResult`. A Gateway-reported timeout → a
valid `ValidationResult` with `status = failed` → verdict rule #2. Same effective
`inconclusive`, different provenance, shown distinctly.

---

# Open items — UNRESOLVED, for a later step

These are **not** settled. Do not treat them as decided.

## Early stall detection depends on an unstated Gateway property (dropped from Step 3A)

The frozen contract guarantees **no progress/heartbeat cadence** — nothing in
`operations.md` or `execution-event.schema.json` obliges the Gateway to emit
`progress` (or anything) within any interval. A legitimate compile/test can be
silent for minutes. So Module 1 **cannot** reliably detect an early stall from
silence without depending on an unstated property of Module 3, and doing so could
manufacture false `inconclusive` evidence. Early stall detection was therefore
**dropped from Step 3A** (only the authoritative timeout above remains). Revisit
**only** if a heartbeat/progress cadence is added in a future additive contract
version — a conversation with Sadia, not something Module 1 infers on its own.

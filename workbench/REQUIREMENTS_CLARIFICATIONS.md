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

## Restart recovery — reconciliation & the conservative rules (Step 3B-1, settled)

On startup Module 1 reconciles non-terminal local attempts. It **never re-calls
`start`**: accepted runs (with `gateway_ack`) are only observed/completed by the
existing `run_id`; a run whose acceptance is unknown becomes terminal
`error_kind = start_unresolved` (effective `inconclusive`, no result). A terminal
event with no result is completed within the shared, bounded result-retrieval
allowance (below). A pre-crash but unconfirmed cancellation is **not** auto-
reissued (repeated `cancel` is contractually undefined — see open items); recovery
resumes observing and surfaces the ambiguity so the operator decides. Recovery
status is ephemeral (in-memory), never persisted.

## Result-retrieval allowance is separate from the execution deadline (Step 3B-1, settled)

`operations.md` says `result` "returns nothing until the run reaches a terminal
state" — a lower bound only; it does **not** guarantee the `ValidationResult` is
available the instant a terminal event is emitted. So once a terminal event is
observed, Module 1 applies a bounded, authoritative **result-retrieval allowance**
(`config.result_retrieval_window_seconds`, each call bounded by
`min(GATEWAY_HTTP_TIMEOUT, remaining window)`, no retry past the window). This is a
publication allowance, not extra execution time, and is used identically by the
normal poller and by restart recovery.

## Operator cancellation-delivery is a distinct, persisted knowledge state (Step 3B-1 fix, settled)

Operator cancellation intent (`cancel_requested`) is separate from what Module 1
knows about **delivery** of that request. Delivery knowledge is persisted in a new
`cancel_delivery` column with an explicit vocabulary:

- `NULL` (with `cancel_requested=0`) — no operator cancellation requested;
- `unknown` — requested, but Module 1 cannot determine the delivery outcome;
- `undelivered` — positive evidence the request did not reach Module 3
  (ECONNREFUSED / DNS only — a reset/timeout/5xx is **not** proof of non-delivery);
- `rejected` — Module 3 returned 4xx: it received and declined the request;
- `acknowledged` — Module 3 returned 2xx and acknowledged the request. This is
  **not** the same as the run being cancelled — only the contract `cancelled`
  event/result and verdict rule #1 mean that.

`unknown` is persisted **before** the Gateway call, so an interruption mid-call
reconciles to exactly what Module 1 knows. `acknowledged` is **sticky**: a later
failed/rejected/unknown attempt cannot downgrade it (an earlier request was already
acknowledged); the immediate response still describes the latest attempt. The Cancel
control's presence tracks **terminal run state only**, never attempt history — a
failed delivery is not a cancellation and must never hide or disable it. The
post-timeout cleanup cancel (Step 3A) is Module-1-initiated and must **not** touch
any of these operator-cancel fields. Recovery reads `cancel_delivery` and never
auto-reissues; the operator decides whether to Cancel again. This supersedes the
earlier ephemeral `cancel_ambiguous` recovery status, which conflated the
known-non-delivery and genuinely-unknown cases.

---

# Open items — UNRESOLVED, for a later step

These are **not** settled. Do not treat them as decided.

## Repeated `cancel` has no defined semantics in the frozen contract (Step 3B-1)

`operations.md` defines `cancel` as *"Ends the run with a cancelled event and a
cancelled result"* but says nothing about a **second** `cancel` for the same
`run_id`; there is no idempotence language anywhere in `contract/`. So Module 1
does **not** auto-reissue a pre-crash unconfirmed cancellation during recovery.
The dev mock happens to treat repeated cancel safely, but that is **mock-only**,
not contract evidence. Question for the Gateway owner / a future additive
contract version: define repeated-`cancel` semantics (ideally idempotent).

## Ambiguous `start` cannot be reconciled under the frozen contract (Step 3B-1)

If Module 1 crashes after calling `start` but before persisting `gateway_ack`, it
cannot determine whether Module 3 created a run: the contract has no `start`
idempotency on `run_id`, no defined unknown-`run_id` response on `events`/`result`
(so probing is unreliable), and no lookup operation. Module 1 therefore records
`start_unresolved` and never retries. An **orphaned Gateway run may still be
executing**; a fresh attempt could run alongside it. Recommended future additive
capability: **idempotent `start` on the client-generated `run_id`** and/or
**lookup-by-`run_id`**, so recovery can reconcile deterministically.

## Orphaned-run risk under the slot-pool model (Step 3B-1) — question for the Gateway owner

Does each run receive an **exclusively reserved workspace that is reset on
acquire**, so an orphaned run only wastes a validation slot until reclamation and
**cannot contaminate** a fresh run's evidence? Module 1 cannot establish this from
its side. The answer is the difference between temporary **inefficiency** and a
**threat to the reliability of validation evidence** — flag for Sadia.

## Fresh-run vs recovery-path transient-unreachability asymmetry (Step 3B-1)

During a normal fresh run, a mid-run unreachable Gateway becomes an immediate
terminal integration error; during restart recovery, a temporarily unreachable
Gateway is tolerated and retried within the authoritative remaining run deadline.
The same network failure currently produces different outcomes depending on
whether the Workbench restarted. Not changed in 3B-1. Revisit after continuity to
decide whether the fresh-run path should adopt equivalent bounded-retry semantics.

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

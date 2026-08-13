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

## Minimal explicit package format; manifest is structural config, not knowledge (Step 3C-1, settled)

A package is an operator-registered root folder with a `package.yaml` manifest
declaring only `entry_point` and `tasks:` (optional `name`/`version`). The manifest
is Module 1 structural configuration: it is **excluded** from
`KnowledgePackage.files`, **never crosses Module 2**, and **never enters any
fingerprint**. This replaces the earlier heuristic loader (index-name guessing,
hardcoded `tasks/`). No Business/Technical/Skills taxonomy exists in Module 1 V1 —
folder organisation is an authoring convention Module 1 does not interpret. The
package (knowledge) fingerprint is derived only from the assembled
`(package-relative path, content)` sequence, so it is independent of the absolute
registered root and identical across machines/OSes. Package fingerprint, task
fingerprint, and (3C-3) validation-context fingerprint remain distinct concepts;
structural loader config is never conflated with knowledge evidence. Full spec:
`workbench/PACKAGE_FORMAT.md`. Adding a manifest to an existing package does not
change its fingerprint if the same knowledge files at the same relative paths are
assembled (verified for Larkspur).

## Operator-supplied local package roots are a trusted-localhost decision (Step 3C-1, settled — NFR-6)

Registering an operator-supplied absolute path (`package_source`) lets the Workbench
read any directory its local process can reach. For V1 this is an **accepted design
decision**: the Workbench is a localhost, single-trusted-operator tool in a trusted
environment, consistent with the existing NFR-6 assumption. Roots are still validated
(must exist and be a directory) and unreadable/invalid roots fail clearly and remain
visible as *unhealthy*. This is not to be re-litigated during implementation unless a
feature would require materially broader access than reading the operator-selected
package root.

## Durable package identity (`package_id`) is separate from content and registration (Step 3C-2, settled)

History and (later) approval are keyed by a durable **`package_id`** declared in the
manifest — NOT by `package_name` (mutable display metadata) nor by the `package_source`
registration (mutable location) nor by `package_fingerprint` (content, which changes
with edits). The axes are kept distinct:
`package_id` (which logical package) → `run` → `package_fingerprint` → immutable
content-addressed snapshot. `package_id` is required with no fallback, route-safe
syntax, one active source per id, and cannot change silently under existing evidence
(a manifest id change refuses runs until deliberate re-registration). Snapshots stay
content-addressed by fingerprint and are never bound to a single `package_id` (two
identities with identical content share one snapshot). Runs persist `package_id`; the
disposable dev DB is recreated rather than migrated for legacy pre-identity rows. Full
format: `workbench/PACKAGE_FORMAT.md`.

**Nullable `package_source.package_id` vs non-null run identity.** `package_id` is
mandatory for a *valid/loadable* package; it is **not** mandatory for the *registration
row*, because a structurally-invalid (Unhealthy) source may have no readable manifest to
obtain identity from. Such a source is registered with a NULL `package_id` so it stays
visible with a clear reason — but a NULL-identity source is never treated as a valid
package: it cannot start a run (`start_run` refuses when the registered identity is
absent), and its package-history is empty (the history query needs a non-NULL id). By
contrast, **every newly created validation run has a non-NULL `package_id`**: a run is
only started from a registered source whose live manifest identity was validated and
confirmed to match the registered identity. `run.package_id` is nullable in the schema
only because we use additive schema changes against the disposable local SQLite DB
rather than migrations — NOT to permit identity-less runs, and there is no fallback
identity. (Enforced in `orchestrator.start_run`; proven by tests.)

## Recovery status is intentionally ephemeral — not historical evidence (Step 3C-2, settled)

Checked against the architecture requirements: no numbered **HST/EVD/UI** requirement
mandates durable proof that a Workbench restart/recovery occurred. HST-1 requires
retention of *fingerprints, evidence, and outcome*; EVD concerns raw evidence and
diagnosis; UI concerns visibility and evidence-linking. Recovery status was
deliberately made ephemeral in Step 3B-1 (it describes what the live poller is doing).
Therefore history/evidence surface **only persisted provenance** — `cancel_requested`/
`cancel_delivery` and `timed_out` — and never infer or display a "recovered" badge.
This is CLOSED, not an open item. (`timed_out` provenance is durable via the run's
terminal error state; a Gateway-reported timeout is a valid result via verdict rule #2.)

## EVD-1 — Module 1 preserves what the contract carries; artifact CONTENT is not transmitted (settled)

EVD-1: "Raw evidence — logs, transcript, diffs, check output — is preserved for every
run and reachable from the UI." The frozen `ValidationResult` carries **artifact names**
(e.g. `diff.patch`, `migrate.log`, `test.log`, `transcript.log`) and **check output**
text (`check_results[].output`), plus the full ExecutionEvent log — but **not** the byte
contents of those artifact files. Module 1 preserves and exposes **everything the frozen
contract actually carries** (events, check outputs, the whole ValidationResult), and
shows artifact names honestly as **references whose content was not transmitted** —
it never implies it holds diff/log/transcript bytes that never crossed Module 2.

This is the artifact-content gap ("flag A") raised during the original Module 1 planning
and accepted then; recorded here so the 3C-3 requirement audit cannot claim Module 1
persists artifact contents. Under this interpretation EVD-1 is **satisfied** in 3C-2:
every piece of evidence Module 1 receives is preserved and reachable. Making artifact
contents reachable would require an **additive Module 2 capability** (artifact content in
the result, or a fetch-artifact operation) — a Sadia/Module 3 contract discussion, not a
hidden Module 1 gap. See the open Gateway-owner item below. `contract/` is NOT changed.

## Current-vs-execution snapshot is factual in 3C-2; formal staleness is 3C-3 (settled)

3C-2 exposes only the fact "this run used fingerprint X; the currently registered
package assembles to Y; X == Y or not." The formal **HST-2** staleness definition,
**HST-3** stale-marking, **HST-4** stale-excluded-from-approval, and **UI-4/5/6**
(task current status / approval control / stale-visually-distinct) are 3C-3, together
with the validation profile.

## Per-package canonical environment/capabilities belong to the 3C-3 validation profile (recorded for 3C-3)

In 3C-1/3C-2 the run-start Target-environment and capability choices are global/ad-hoc.
The **3C-3 per-package validation profile** should define the package's canonical
`target_environment` and permitted capability set; opening a package should default to
that profile's environment rather than presenting one undifferentiated global choice;
ad-hoc overrides (if kept for experimentation) must be visibly distinct and must not
silently redefine the profile; and only a run whose package/task/capabilities/
environment context matches the current profile can later qualify as approval evidence.

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

## Artifact CONTENT is not carried by the frozen contract (Step 3C-2) — question for the Gateway owner

The `ValidationResult` carries artifact **names** (`diff.patch`, `test.log`,
`transcript.log`, …) and `check_results[].output` text, but **not** the artifact file
**contents**. Module 1 therefore preserves and exposes everything the contract carries
and shows artifact names as references only (see the settled EVD-1 interpretation above).
If artifact contents must be reachable from Module 1, that needs an **additive Module 2
capability** — e.g. artifact content embedded in the result, or a **fetch-artifact**
operation keyed by `run_id` + artifact name — decided with Sadia. This is a
contract-adjacent item like idempotent `start`, repeated-`cancel` semantics, result-
availability timing, and slot/workspace isolation; it is NOT a hidden Module 1 gap.

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

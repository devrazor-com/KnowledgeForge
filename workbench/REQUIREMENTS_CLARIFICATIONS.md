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

## Cross-OS fingerprint stability is empirically confirmed on Windows (post-v1.0, settled)

Fingerprint machine-independence — the property that staleness and approval semantics
rest on (a run's evidence is keyed to knowledge content, not to the machine it ran on)
— is now confirmed on a **real Windows machine**, not merely simulated by LF/CRLF unit
tests. In a complete Windows run (Larkspur registered from a Windows path, profile
configured, one full validation to a PASSED rule-#6 verdict), the fingerprints matched
the established macOS values **byte-for-byte**:

- Larkspur **package** fingerprint:
  `sha256:ab62f181e48dcb0d1cff0a3cdeb606ed33308dd6ef08aa5b5312fcdb62ea6ac9`
- `LARK-TASK-001` **task** fingerprint:
  `sha256:16ad18f5a74dc880463bcb38a9b6cedcdb81a6b23e3c8b7d56bd4e1243e844c9`

This validates in practice what `fingerprints.normalize_content` (CRLF/CR → LF) and the
POSIX package-relative paths were designed to guarantee. No fingerprint code changed as
part of confirming this; the value here is the empirical cross-machine evidence.

## Supported Python baseline: 3.12 (post-v1.0, settled)

**Python 3.12 is the supported integration/development baseline** (`>=3.12,<3.13`;
declared in `.python-version`, README, VERIFY.md). Both Mac and Windows integration are
verified on 3.12. Evidence (one full suite run per interpreter; the failure mode is
intermittent, so this is strong empirical evidence, not a controlled proof):

- Mac 3.12: 166 passed.
- Windows 3.12.6: 158 passed / 4 failed / 4 skipped.
- Windows 3.14.4: 143 passed / 15 failed / 4 skipped / 1 error.
- The relevant dependency versions were **identical** across the two Windows venvs
  (uvicorn 0.52.3, fastapi 0.141.1, starlette 1.6.0, anyio 4.14.2, h11 0.16.0,
  pydantic 2.13.4, pytest 9.1.1, click 8.4.2, jsonschema 4.26.0), so the interpreter —
  not a dependency version — is the material environment difference.

We record the **support decision**, not a causal claim about a specific interpreter
defect: Python 3.14 is not currently supported for Windows integration because our
validation suite exhibits materially more Windows asyncio accept failures under it. The
range is pinned to the single 3.12 minor line so Mac and Windows run the **same asyncio
implementation** — the earlier Mac-3.11 / Windows-3.14 split is precisely the confound
that produced divergent results. Dependencies are pinned (`requirements.txt` direct pins;
`requirements.lock` full closure with a `sys_platform == "win32"` marker for the
Windows-only `colorama`) so the environment does not drift underneath the baseline.

## Windows event loop: serve on the Selector loop, not the Proactor loop (post-v1.0, settled — source + Windows A/B)

On Windows the default asyncio **ProactorEventLoop** has a real accept-loop failure. In
CPython 3.12 `windows_events.IocpProactor.accept.finish_accept` calls `ov.getresult()`
with no handler, so an accept-time **`WinError 64` (ERROR_NETNAME_DELETED)** — produced
when an incoming connection is aborted while `AcceptEx` is completing — escapes;
`proactor_events.BaseProactorEventLoop._start_serving` catches it in `except OSError`,
logs `Accept failed on a socket`, **closes the listening socket, and does not re-arm
accept** (the re-arm is only on the success `else` branch). The process stays alive but
can no longer accept connections. Verified from the Windows 3.12.6 stdlib and reproduced
in a standalone A/B: ordinary `uvicorn.run()` (Proactor) and a manual Proactor server
both lost the listener in **fewer than 500** abortive connections, while a Selector
server survived **10,000** across two runs with no accept error. The **SelectorEventLoop**
registers its listener once via `_add_reader` and keeps it across per-accept errors (only
the resource-exhaustion path removes and re-schedules it), so it is not exposed to this.

**Mitigation (launcher/config boundary; no request-behaviour change):** start Module 1
with `python -m workbench.run_workbench`, which on Windows selects the Selector loop
through uvicorn's own custom loop-factory (`workbench.winloop:selector_loop_factory`).
uvicorn's `Server.run()` still owns the `asyncio.Runner` and shutdown lifecycle — the
manually-driven `loop.run_until_complete(server.serve())` pattern regressed clean Ctrl-C
and is deliberately not used. The launcher makes the safe loop automatic on Windows (no
`--loop` flag to forget) and is a no-op elsewhere. App startup logs the live loop
(`[workbench] serving on event loop: …`) so a Windows launch is positively confirmed to
serve on `_WindowsSelectorEventLoop`. Module 1 uses no asyncio subprocesses,
`add_reader` on non-sockets, or other Proactor-only features, so it is Selector-compatible.

## Transport classification is structural and evidence-bounded; timeout ≠ non-delivery (post-v1.0, settled — Windows evidence)

`gateway_client._classify` categorises a transport failure from the **exception
structure** (`socket.timeout`/`TimeoutError` → `timeout`; `socket.gaierror` → `dns`;
`ConnectionRefusedError`/`errno.ECONNREFUSED` → `refused`; else `other`), never from
message text. Only `refused`/`dns` are in `NON_DELIVERY_REASONS` — the reasons that
**positively prove** the request never reached Module 3. This is correct and must not
change; do not widen `NON_DELIVERY_REASONS`.

Windows made the distinction concrete and repeatable:

- Connecting to a *released* high loopback port (obtained and closed exactly as the test
  harness does) yielded `URLError` wrapping `TimeoutError('timed out')`, `errno=None`,
  `winerror=None` → `_classify` = `timeout`. A timeout does **not** prove non-delivery,
  so a cancellation attempt correctly resolves to `cancel_delivery = unknown`, and a
  fresh-start attempt would remain the conservative unreachable/indeterminate outcome.
- Killing the actual Gateway (mock on port 8003) yielded a **structured** refusal
  (`WinError 10061` → `ConnectionRefusedError`) → `_classify` = `refused` → Module 1
  reported the Gateway unreachable and marked stages not reached, no result fabricated.

So: this environment *can* produce a true refusal; a *supposedly* unused port does
**not** necessarily produce one; the classifier must classify the evidence it actually
receives. The failing `test_sticky_acknowledged_not_downgraded_by_failed_retry` encodes
the environment-dependent assumption that "no listener ⇒ immediate refusal"; that is a
**test** defect (to be corrected in the harness work), not a classifier defect.

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

## Extended validation-context staleness — stronger than the literal HST-2/APR-3/APR-4 (Step 3C-3, settled)

**Literal requirements:** HST-2 "A run is stale if its package or task fingerprint no
longer matches the current one." APR-3 "Approval records who, when, the package
fingerprint, and the set of task fingerprints." APR-4 "Changing the package or any
active task invalidates approval."

**Implemented (stronger) interpretation, approved by the operator:** the current
validation context is **package fingerprint + task fingerprint + canonical permitted
capability set + target environment** (the profile). A run is stale when its
execution-time context no longer equals the current context — so a change to the
package validation profile's **target environment** or **permitted capabilities** also
makes a run stale and invalidates approval, in addition to package/task fingerprint
changes. Capability ordering is canonicalised (sorted) and never itself causes
staleness. The approval record therefore persists **more** than APR-3 literally
requires (it also stores the profile's environment and capability set), never less.
This is a deliberate superset of HST-2/APR-3/APR-4, recorded so the final audit
distinguishes the literal requirement from the stronger implemented model. Package
fingerprint stays purely knowledge-derived; the validation-context identity is a
separate concept.

## needs_review resolved-to-passed counts as passing evidence for APR-1 (Step 3C-3, settled)

VER-5 makes a task with no declared checks always yield `needs_review`; VER-6 lets a
named human resolve a `needs_review` run to `passed`/`failed` with the time recorded;
VER-7 keeps that resolution alongside the machine verdict, never replacing it. So a
`needs_review` run resolved to `passed` counts as passing evidence for APR-1's "latest
passed, non-stale run" (the only path by which a no-check task can be approved). Review
applies ONLY to `needs_review` runs — it can never make a `failed`/`inconclusive`/
`cancelled` run approvable. A later validation-context change makes the resolved run
stale, so it stops counting toward current eligibility (HST-4) while the resolution
remains a historical fact. (Verified against the full VER/APR text in the final audit.)

## APR-1 candidate-selection rule — current context first (Step 3C-3, settled)

APR-1 "every active task has a latest passed, non-stale run" is ambiguous. Settled rule,
in two steps:

**1. Restrict to the current-context candidate set.** A run is an approval candidate for
a task only if its validation context (package fp + task fp + canonical capability set +
target environment) equals the package's **current** context. Runs under a different
context — an environment/capability **override**, or a superseded package/task/profile —
are **outside** the candidate set: they neither qualify nor disqualify current evidence.
Crucially, a passing override run performed AFTER a valid profile-matching pass does
**not** revoke that pass (it isn't a candidate at all). This differs from ordinary
staleness (a package/task/profile change moves the current context itself, so old-context
evidence legitimately stops qualifying — those runs then read as *re-validation
required*).

**2. Decide within the candidates, newest-first.** `inconclusive` (VER-3: the package was
never really tested — neither for nor against) and `cancelled` (abandoned attempt) are
**skipped**; an **unresolved** `needs_review` **stops the scan and blocks** approval
(current evidence explicitly awaiting human judgment — not a fallback to an older pass);
a **resolved** `needs_review` uses the human `passed`/`failed` (VER-6/VER-7); the first
`passed`/`failed` decides. The task qualifies iff that decisive current run is `passed`.

Concrete results (current context): `passed→failed` = no; `passed→inconclusive` = still
qualifies from the prior pass; `passed→cancelled` = still qualifies; `passed→unresolved
needs_review` = blocked (review pending); `passed→resolved-passed` = qualifies;
`passed→resolved-failed` = no; and a passing env/capability override after a current pass
= still qualifies (override not a candidate). Checked against VER-3, VER-6, VER-7 and
APR-1 — no contradiction. Not the loose "any older passing run" reading. Per-task status
distinguishes **not yet validated** (no runs) from **re-validation required** (runs exist
but none under the current context) — display semantics derived from stored evidence, no
new persisted state. Candidate filtering affects approval **eligibility only**; history
and the evidence view remain complete and mark stale/override runs (HST-3/UI-6).

## Profile-form environment list is intentional; the old run-form global leak was not (Step 3C-3, settled)

The **validation-profile configuration form** deliberately offers **all** globally
configured target environments: that is where the operator chooses the package's
**canonical** validation environment, so the full list is correct there. This is
distinct from the 3C-1 **run-form** environment leak, which was wrong: execution must
start from the package's configured profile, never silently inherit a global environment
from another package. Under "no profile, no run", the run form now defaults strictly from
the profile; selecting a different value is an explicit **run-only override** that does
not update the profile and does not qualify as current approval evidence. Recorded so a
future cleanup does not "fix" the profile form by removing legitimate choices, nor
reintroduce global defaults into execution.

## No validation profile, no validation run (Step 3C-3, settled — supersedes the 3C-3-proposal pre-profile model)

Every validation run executes against an **explicitly configured** package validation
context. A newly registered package is "profile not configured"; it may be inspected
but the Run action is blocked until the operator configures the profile (target
environment + permitted capabilities). Module 1 never manufactures a context from
global defaults, and there is no fallback execution context — which also removes the
environment-leakage seen in 3C-1. Pre-profile runs are not supported, so no run's
approval eligibility is undetermined-at-creation. The disposable dev DB is recreated;
a historical run recorded before this rule appears honestly in history as evidence
whose context differs from the current one (immutable), its current qualification
derived, never hidden or reinterpreted.

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

**Live cancel is also single-shot on a 5xx (post-v1.0 fix, settled independently of
the Gateway owner).** The shared Gateway-call wrapper `_gw_call` retries a transient
5xx up to `RETRY_ATTEMPTS` — correct for `start`/`events`/`result`, whose reads are
safe to repeat. It was previously applied to `cancel` too, so a 5xx on cancel could
transmit up to three physical cancel requests — an *automatic* repeated cancel, exactly
what the paragraph above says Module 1 must not do. The operator cancel path now passes
`retry_5xx=False`, so **a 5xx yields exactly one physical cancel request**. The rationale:
*a 5xx does not establish whether the first cancel was acted upon, so Module 1 records
cancellation delivery as `unknown` and exposes that uncertainty rather than making an
undefined repeated cancel call on the operator's behalf; the operator, seeing that
evidence, decides whether another cancellation attempt is appropriate.* The existing UX
is unchanged: `unknown` stays visually/textually distinct from `acknowledged`, the
uncertainty is shown, and manual Cancel-again remains available while the run is
non-terminal. `start`/`events`/`result`/cleanup retry behaviour is untouched. A
mechanism test (`test_cancel_5xx_sends_exactly_one_physical_request`) asserts the mock
received the cancel exactly once, not merely that the state is `unknown`.

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

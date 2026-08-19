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
with `python -m workbench.run_workbench` — the one documented launch on every platform —
which selects the Selector loop through uvicorn's own custom loop-factory
(`workbench.eventloop:selector_loop_factory`). uvicorn's `Server.run()` still owns the
`asyncio.Runner` and shutdown lifecycle — the manually-driven
`loop.run_until_complete(server.serve())` pattern regressed clean Ctrl-C and is
deliberately not used. **This is one branchless cross-platform choice, not a Windows
workaround hidden behind a flag:** on macOS/Linux the selector loop is what uvicorn
already uses (uvloop is absent), and on Windows it is this mitigation; selecting it
explicitly everywhere gives a single launch path and lets the loop-selection be verified
on any OS. The HTTP test harness (`_regutil.start_server`) selects the same factory the
same way, also without a platform branch. App startup logs the live loop as
`<module>.<class>` (e.g. `asyncio.unix_events._UnixSelectorEventLoop` on macOS,
`asyncio.windows_events._WindowsSelectorEventLoop` on Windows), so any launch is positively
confirmed to serve on a selector loop and a reader can resolve the exact runtime class.
Module 1 uses no asyncio subprocesses,
`add_reader` on non-sockets, or other Proactor-only features, so it is Selector-compatible.

## Windows acceptance of candidate dc56c26 — PASS (post-v1.0, recorded)

The Selector mitigation, the Python 3.12 lock baseline, and active-run restart/recovery
were accepted on a real Windows work machine against the mock Gateway, from a fresh
lock-installed environment (candidate commit `dc56c26`, archive verified by SHA-256
before extraction; provenance `commit=dc56c26 origin_main=8319bda v1.0-module1=a5c0bd7`).

- **Phase A — PASS.** Fresh Python 3.12.6 venv installed from `workbench/requirements.lock`:
  28 lock entries, 28 installed, zero missing / zero extra, `colorama==0.4.6` present via
  the `sys_platform == "win32"` marker.
- **Phase C — PASS.** Real Workbench launched with `python -m workbench.run_workbench`;
  startup logged `[workbench] serving on event loop: _WindowsSelectorEventLoop`. Larkspur
  registered from a Windows path; package fingerprint `sha256:ab62f181…` and LARK-TASK-001
  `sha256:16ad18f5…` matched the recorded values in full. A forced-success run reached
  PASSED (rule #6) with all 8 events individually validated and the ValidationResult
  validated; a cancelled run rendered `cancel_delivery=acknowledged` on both the run
  screen and history and reached CANCELLED (rule #1). History/evidence correct; Ctrl-C
  shut down cleanly.
- **Phase D — PASS (deterministic active-run restart/recovery).** With
  `MOCK_EVENT_DELAY_SECONDS=15`, a forced-success run was interrupted after event 2 (only
  sequences 1–2 persisted), the Workbench stopped and restarted (Selector reconfirmed),
  and the run allowed to complete. Evidence: mock `starts[run_id] == 1` (reattached, not
  replaced — recovery reused the client-supplied `run_id` and never re-called start); the
  persisted event list was exactly `[(1,accepted),(2,started),(3,progress),(4,tool_call),
  (5,tool_call),(6,check),(7,check),(8,completed)]` — eight contiguous rows, no duplicates,
  no gaps; final verdict PASSED (rule #6). **Why this is strong evidence, not merely a
  green UI:** recovery resumes from the persisted `last_sequence`, checks each resumed
  event against `expected = max_seq + 1`, and would have terminated the run as
  `protocol_error` *before* persisting any mis-sequenced event (the `run_event` PRIMARY
  KEY on `(run_id, sequence)` is a second guard). A mis-sequenced recovery therefore could
  not have produced a PASSED run — so the PASSED run with eight contiguous rows proves the
  durable evidence was reconstructed correctly, not just that the UI reached PASSED. This
  closes the 2026-08-14 inconclusive recovery test (which finished before the Workbench
  could be stopped); the `MOCK_EVENT_DELAY_SECONDS` knob made it deterministic.

The Selector loop was confirmed on four independent Workbench process starts and never
fell back to Proactor. Two operational notes: (1) Ctrl-C on the real Workbench may briefly
print `Waiting for connections to close` while a browser SSE stream is open, then times
out and exits cleanly on its own — no second Ctrl-C or tab-closing needed; (2) the real
Workbench under `run_workbench` does NOT inherit the Ctrl-C shutdown hang seen in the three
throwaway manual-loop A/B diagnostic servers (those drove `loop.run_until_complete`
directly; `run_workbench` keeps uvicorn's `Server.run()` lifecycle).

This acceptance attaches to the tree actually executed, `dc56c26`. It certifies the
Selector mitigation's product-level behaviour and the lock baseline; it does not exercise
Sadia's Gateway (Module 1 has no non-mutating Gateway op — its first real call is
necessarily `POST /runs`), and the full Windows pytest suite gate for the harness commit
remains separate and open.

## Windows harness acceptance of candidate 86243b8 — PASS (post-v1.0, recorded)

The full Windows pytest suite gate for the test harness is now closed on a real Windows
machine (Python 3.12.6), from a fresh lock-installed environment on candidate `86243b8`:

- **Full suite: 172 passed, 4 skipped, 0 failed** (176 collected).
- **Mechanism proof PASSED:** `test_windows_harness_child_serves_on_selector_loop` — a
  Workbench child launched through the same `_regutil.start_server` the whole harness uses
  positively reported serving on `_WindowsSelectorEventLoop` (adoption by the real launch
  path, not mere factory construction). So the harness child servers are verified to run
  under the accepted Selector-loop mitigation, and the three historical WinError-64
  readiness failures are resolved by launching them on Selector.
- **Deterministic classifier test PASSED on Windows:**
  `test_sticky_acknowledged_not_downgraded_by_failed_retry` now fails the later cancel via
  the mock's `cancel_fault="http_500"` (a controlled 5xx → `unknown`), so the earlier
  OS-dependent closed-port premise (refuse vs. timeout) is gone without weakening the
  assertion (it still asserts `attempt=='unknown'`, sticky `acknowledged` preserved,
  exactly one physical cancel).
- **The four skips are legitimate platform constraints, not portability defects:**
  two SIGSTOP/SIGCONT tests (`test_cancel_delivery_http.py`, `test_recovery_http.py`) are
  POSIX-only job-control; two symlink tests (`test_portability.py`,
  `test_root_identity_http.py`) skip because **symlink creation was not permitted in this
  Windows configuration** — a Windows permission/policy constraint, not a Module 1 defect.

## Cross-platform acceptance of candidate af8bb8f — PASS on macOS and Windows (post-v1.0, recorded)

The unified, branchless Selector event-loop architecture (one codebase, one launch command
`python -m workbench.run_workbench`, one explicit Selector loop; `winloop`→`eventloop`) was
accepted on both macOS and Windows for candidate `af8bb8f`. This is portability acceptance
only — it does not close any product/Gateway item. Evidence is recorded in two classes:
**artifact-backed** (contained in a hashed file captured during the Windows run) and
**transcribed console** (directly observed and written into the Windows acceptance report,
not into a hashed file). The distinction is preserved below.

**macOS acceptance (candidate af8bb8f).** Full suite 175 collected, 175 passed, 0 skipped,
0 failed; live serving loop `asyncio.unix_events._UnixSelectorEventLoop`; the platform-
neutral structural mechanism test passed; `python -m workbench.run_workbench` started and
shut down cleanly. (Observed on the Mac dev machine.)

**Windows acceptance (same candidate af8bb8f, Python 3.12.6, fresh venv from
`requirements.lock`).**
- *Gate A — locked env.* Interpreter Python 3.12.6 (**artifact-backed**, `win_pytest.txt`
  session header); installed package list (**artifact-backed**, `win_freeze.txt`). The
  normalized comparison `lock 28 / installed 28 / missing [] / extra []` and
  `colorama==0.4.6` present via the `sys_platform=="win32"` marker are **transcribed
  console** observations, not hashed-file content.
- *Gate B — full suite.* 175 collected, **171 passed, 4 skipped, 0 failed**, runtime
  362.72s (**artifact-backed**, `win_pytest.txt`). The structural Selector mechanism test
  `test_harness_selector_loop.py::test_harness_child_serves_on_selector_loop` PASSED. The
  four skips (file:line + reason are in the captured `-ra` output; the **test-function
  names below were verified from source**, as they were not in the `-ra` summary):
  `test_cancel_delivery_http.py:249` `test_unknown_delivery_on_timeout` and
  `test_recovery_http.py:357` `test_recover_gateway_unreachable_then_reachable` — both
  `@_needs_job_control`, SIGSTOP/SIGCONT POSIX-only; `test_portability.py:105`
  `test_same_physical_dir_sees_through_symlinks` and `test_root_identity_http.py:122`
  `test_add_via_symlink_opens_existing_registration` — symlink creation not permitted in
  this Windows configuration (a permission/policy constraint, not a defect).
- *Gate C — real Workbench.* Launched with `python -m workbench.run_workbench` (the same
  command documented for Mac); startup reported live loop
  `asyncio.windows_events._WindowsSelectorEventLoop`; `/help` returned 200; one Ctrl-C shut
  down cleanly. (Live-loop line, `/help` result and shutdown are **transcribed console**
  observations.)
- *Gate D1 — supported Selector launcher under abortive-connect stress.* **Artifact-backed**
  (`d1_stress.txt`): 10,000 successful abortive connects over 20 batches × 500, with every
  recorded error class at zero (10048=0, 10055=0, 10061=0, timeout=0, other=0) and every
  post-batch `/help` PASS. **Transcribed console**: the same PID stayed LISTENING; exactly
  21 `/help 200` server log lines; **no** `Accept failed on a socket`, **no** WinError 64,
  **no** traceback; Ctrl-C then shut down cleanly.
- *Gate D2 — deliberate Proactor negative control (NOT a supported launch path).* Started
  with bare `python -m uvicorn workbench.app:app --port 8010` as a diagnostic control only.
  **Artifact-backed** (`d2_stress.txt`): PRE-BURST `/help` passed; the listener stopped
  accepting after **124** successful abortive connects; 25 consecutive further connects
  failed; all three confirmation `/help` GETs returned `WSAECONNREFUSED 10061`.
  **Transcribed console**: startup reported `asyncio.windows_events.ProactorEventLoop`;
  `Task exception was never retrieved`; `OSError [WinError 64]`; the failure occurred in
  `finish_accept` at `windows_events.py:555` on `ov.getresult()` and surfaced as
  `Accept failed on a socket` at `proactor_events.py:846`; the process remained alive;
  port 8010 had no LISTENING entry; `curl` returned `000` at `time_total 2.047987s`.

**Unplanned confirmation (transcribed console):** the `app.py` startup diagnostic also
printed under bare `python -m uvicorn workbench.app:app` with no `run_workbench` involved —
so the startup line is emitted by the application and reports the loop it is *actually*
serving on, not the launcher's intended configuration. Every Selector line in Gates C/D1 is
therefore live-loop evidence.

**Deviations from the written Windows procedure** (recorded so the record does not imply
literal compliance): (1) Gate B used a plain redirect `> win_pytest.txt 2>&1` instead of the
PowerShell `Tee-Object` pipeline — capture method only, no effect on execution/result;
(2) `-ra` was added so skip reasons appear in the summary — reporting only; (3) the
between-arm `TIME_WAIT ≈ 16` target was **not met and not used** — persistent corporate/
background traffic held TIME_WAIT at ~90–122 all session, so a **substituted** criterion was
used (D1 PID gone; no `:8010` LISTENING; D1/D2 started from similar counts, 90 and 105; the
count stayed within the session band rather than climbing; the 10,000 SO_LINGER/RST closes
bypass TIME_WAIT and moved the total by ~11). This was a substitution, **not** a satisfied
original criterion.

**Evidence hashes (SHA-256, as reported in the Windows acceptance report; not recomputed
here):** `ACCEPTANCE_BUILD.txt`
`b409cf7bcc87a00c9ce30d83d60654cbe5aadc1050b57e2c7d86ee75a48066d0`; `win_freeze.txt`
`9fd0d6352d4beda769c943178dc7eaaa2551fd7f50cd5eb131fadd3ead3a33a0`; `win_pytest.txt`
`16d5a3b7b6522971bf10577ba4283dff31757f2d4acf45f28039e57b4a074cda`; `d1_stress.txt`
`804d5fb3fe1e83bfe5f401093cf29fa2bbb51ed33e2d6b6bfd0242b2326c877f`; `d2_stress.txt`
`281fb1e3d696e19711ad29441f4768079b6fcee7f665efd75e4b9d3b700dfb27`.

**Conclusion (narrow):** `af8bb8f` is accepted on macOS and Windows. Module 1 now uses one
codebase, one documented launch command, and one explicit Selector event-loop architecture
on both platforms. Under the controlled Windows A/B, the supported Selector launcher
survived 10,000 abortive connects **with no accept error at all** while the deliberately
Proactor-based negative control lost its listener after 124 successful abortive connects.
(Selector did not "recover" from an accept error — Gate D1 observed none.)

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

## Cross-platform acceptance of candidate 5fe415c — PASS on macOS and Windows (configurable Module 3 target environments)

Candidate `5fe415c763f9646a44c0a9cc41aacee2b8938dae` (parent
`b9338949ee90ebe9195fdaaf9c40ee80f237c44c`) — **deployment-configurable Module 3 target
environments** (fail-closed; Module 3 owns the accepted logical names, Module 1 presents the
configured list and sends the selected value verbatim) — was accepted on both macOS and
Windows. Tag `v1.0-module1` = `a5c0bd7a3132847c6bd49184ef0bf2ba0154271c`, unmoved. This
record preserves two evidence classes: **artifact-backed** (contained in a hashed file
captured during the Windows run) and **transcribed HTTP/console** (directly observed and
written into the Windows acceptance report, not into a hashed file). Gate C observations
below are **transcribed HTTP/console** observations, **not** hashed artifacts, and must not
be elevated to artifact-backed evidence.

**macOS acceptance (candidate 5fe415c).** Full suite **190 collected, 190 passed, 0 skipped,
0 failed**. (Observed on the Mac dev machine.)

**Windows acceptance (same candidate 5fe415c, Python 3.12.6, fresh tree
`C:\KF_ConfigEnv2\KnowledgeForge`, empty database at start).**
- *Gate 0 — provenance (artifact-backed).* Archive `kf_configurable_environments_5fe415c.zip`
  decoded to 270,391 bytes and matched SHA-256
  `bf59d7c0dc1b67559e099fec704db7dcb36e82c5be575484c45cab0d26efe5af`; `ACCEPTANCE_BUILD.txt`
  recorded `commit=5fe415c…`, `origin_main=b9338949…`, `v1.0-module1=a5c0bd7…`,
  `created=2026-08-19T14:21:29Z`. Both templates confirmed to carry the placeholder guarded
  by `{% if not env_current %}`.
- *Gate A — locked env.* Python 3.12.6; `lock 28 / installed 28 / missing [] / extra []`;
  `colorama==0.4.6` via the `sys_platform=="win32"` marker. Fourth independent tree
  reproducing the lock exactly. Interpreter/freeze **artifact-backed** (`win_pytest.txt`
  header, `win_freeze.txt`); the normalized comparison is **transcribed console**.
- *Gate B — full suite (artifact-backed, `win_pytest.txt`).* `pytest workbench\tests -v -ra`
  → **190 collected, 186 passed, 4 skipped, 0 failed**, elapsed **373.77s**. All 13
  `test_environments_*` tests ran and passed (none skipped), including the four new
  render-state tests; both `unaffected_by_environment_removal` regressions passed. The four
  skips (file:line + reason from the captured output):
  - `test_cancel_delivery_http.py:249` — SIGSTOP/SIGCONT POSIX-only;
  - `test_recovery_http.py:357` — SIGSTOP/SIGCONT POSIX-only;
  - `test_portability.py:105` — symlink creation not permitted;
  - `test_root_identity_http.py:122` — symlink creation not permitted.
- *Gate C1 — Windows-native file behaviour (transcribed HTTP/console).* Config file at
  `C:\KF Acceptance Config\environments.txt` (an absolute path containing a space): 58 bytes,
  `EF BB BF` BOM, 3 CR, one full-line comment, `webplus` padded three spaces each side.
  Rendered through the UI as `ifastbase, webplus, idr` — file order preserved, BOM stripped,
  padding removed, comment dropped, internal characters untouched. A live edit was reflected
  without restarting the process.
- *Gate C4 — history vs future eligibility (transcribed HTTP/console); both halves PASS.*
  Rebuilt from scratch: registered `larkspur-475f97`, profile `webplus`, run
  `run-20260819-195605-06f202`; then `webplus` removed from the file with the process still
  running. **Historical readability:** the run retained `target_environment: webplus`, badged
  current, `gateway_unreachable → inconclusive` unchanged; the stored profile still displayed
  `webplus` with its `Configured … · by VB` stamp even with configuration entirely absent.
  **Future selection:** placeholder led both forms; a crafted empty POST was rejected; no
  second run was created. This is the exact `843bca7` failure, now absent.
- *All four environment render states observed live (transcribed HTTP/console).*
  - State 1 — stored env still configured: `<option value="webplus" selected>` on both forms,
    no placeholder. **This is the negative control** — a stored environment that remains
    configured stays selected without forcing unnecessary reselection; the fix does not
    over-apply.
  - State 2 — stored env no longer configured: the placeholder leads both forms; `webplus`
    is absent from both dropdowns; warnings name it. Correctly forces explicit selection.
  - State 3 — no profile yet: the placeholder leads; no configured option selected. Correctly
    forces explicit selection before the first profile environment is chosen. (This is the
    case that silently offered `ifastbase` under `843bca7`.)
  - State 4 — config error: the message replaces the dropdown; no `<option>` elements at all.
  Obsolete environment names remain visible as historical/context information but are **not**
  offered as selectable choices.
- *Rejection paths (transcribed HTTP/console).* Empty `environment=` POST to
  `/packages/{id}/profile` → **422**; empty `environment=` POST to `/runs` → **422** (FastAPI
  form validation, before route logic — no state written; profile untouched, history stayed
  at exactly one run). A present-but-unconfigured `environment=webplus` POST to `/runs` →
  **400** (the `not in allowed` check in `start_run`), no run written. Both codes are
  load-bearing: 422 stops the empty field, 400 stops a plausible-but-unconfigured name.
- *Gate C3 — fail-closed states (transcribed HTTP/console).* Four states, four distinct
  actionable messages, all fail-closed, no dropdown rendered, no synthetic fallback, no
  filesystem paths or OS errors in the browser: **unset** (names `WORKBENCH_ENVIRONMENTS_FILE`
  and the file format), **missing file**, **no names**, **duplicate** —
  `duplicate entry 'idr' (line 5; first seen at line 3)`, physical file lines, against a file
  whose comment/blank prefix would have made a filtered index read 2 and 0.
- *Gate C5 — safety boundary vs network failure (transcribed HTTP/console).*
  `MOD3_BASE_URL=http://127.0.0.1:8099` (deliberately not 8003). Unconfigured (`webplus`):
  **400**, no run row, history unchanged. Configured (`ifastbase`): **303**, run
  `run-20260819-200956-651f8f` created, reached the network boundary, failed with
  "Module 3 could not be reached at http://127.0.0.1:8099". Same route, same profile, seconds
  apart — the difference is the gate, not the network.
- *Gate C6 — local vs disposable configuration (transcribed HTTP/console).* Convention
  documented as `workbench/local/environments.txt` in README, `environments.example.txt`, and
  `VERIFY.md`; `.gitignore` anchors `/local/` and `/data/` separately with comments;
  `workbench\local\` does not ship in the archive (the operator creates it, as VERIFY
  instructs).
- *Gate C2 — relative path (transcribed HTTP/console); PASS, narrowed.* A relative
  `WORKBENCH_ENVIRONMENTS_FILE` works, resolving against the **process working directory**.
  The planned alternate-cwd negative test was **invalid** rather than failed:
  `python -m workbench.run_workbench` raises `ModuleNotFoundError` from any other directory,
  so the supported launcher inherently requires the repo root as its working directory and the
  cwd-dependency hazard cannot arise through it. Absolute paths remain the documented
  convention as **future-proofing**, not as a correction of an observed failure.

**Evidence provenance (SHA-256, copied verbatim from the Windows acceptance report; not
recomputed here).** Three files were captured and hashed:
- `ACCEPTANCE_BUILD.txt` `847d526916033132f53b425f20e0f7117cbec983ed714cdfb538fae48c5fb173`;
- `win_freeze.txt` `9fd0d6352d4beda769c943178dc7eaaa2551fd7f50cd5eb131fadd3ead3a33a0`
  (identical to the `af8bb8f` acceptance — expected corroboration: `requirements.lock` is
  untouched by this change set, so the same 28 pins on the same interpreter produce a
  byte-identical file);
- `win_pytest.txt` `41d6e8b2e6dd23678cea072de7fb3f1a310f47218502f71220f18d20f2f6f31b`.

All Gate C observations above were **transcribed HTTP/console** output, **not** hashed
artifacts, and are labelled as such.

**Deviations from the written Windows procedure** (recorded proportionately so the record does
not imply literal compliance where Document A says otherwise): (1) Gate B used a plain redirect
with `-ra`, as in prior acceptances (capture/reporting only). (2) Gate C2's planned negative
test was abandoned as **invalid** (see above), not forced; the narrowed finding replaces it.
(3) A claimed cosmetic spacing issue in the run page's POST URL was **withdrawn** — it was read
from `findstr /i` with space-separated patterns, which `findstr` treats as OR, so the matched
lines may not have matched the assumed pattern; no reliable evidence, cosmetic, no gate
affected. (4) Clean shutdown of the final server was not captured (the window closed before
Ctrl-C); not an acceptance criterion for this change set, and several clean shutdowns were
observed earlier in the session.

**Conclusion.** `5fe415c` is accepted on macOS and Windows. Configurable Module 3 target
environments is complete on both platforms. The Gate-C4 operator-safety defect that failed
`843bca7` (both environment `<select required>` widgets manufacturing an unchosen first-option
value when no valid current environment existed) is fixed by a disabled-selected placeholder in
both `_profile_form.html` and the run form in `package_detail.html`, and is now covered by
rendered-HTML regression tests — the class of defect a backend-only assertion could not see.

## MILESTONE — Module 1 product foundation complete (post-5fe415c)

**Module 1 product foundation is complete.** The cross-platform runtime architecture (one
codebase, one launch command, one explicit Selector event loop) and deployment-configurable
Module 3 environment selection are accepted on macOS and Windows. Subsequent Module 1 work
divides into **operator experience** and **contract-required Module 3 integration behaviour**,
not further foundation development.

## Operator-experience findings — observed current UI limitations (not defects fixed here)

Recorded during the Help / Operator Guide inventory. These are **observed current UI
limitations**, deliberately **out of scope** for the Help change set (which documents current
behaviour, it does not change product behaviour). They are **not product defects being fixed**,
and recording them here is **not a promise of future implementation** — they are candidates to
weigh in a later operator-experience change set.

1. **`revalidation_required` has no explanatory affordance in the UI.** The package detail
   tasks table renders the badge *“re-validation required”* (`status.py` → `package_status`
   status `revalidation_required`) but nothing on the page explains that it means *“runs exist,
   but none under the current validation context,”* as distinct from `not_validated` (*never
   run*). The distinction is now documented in operator Help (§17), but the UI itself gives no
   inline hint.
2. **History surfaces cancellation-delivery information only tersely.** `history.html` shows a
   requested cancellation as a small muted sub-line (`cancel: <delivery>`) under the outcome
   cell, without the fuller operator wording the Run screen carries
   (`orchestrator.CANCEL_DELIVERY_MESSAGES`). A reader scanning history sees the raw delivery
   token but not what it establishes (e.g. that `acknowledged` ≠ cancelled).

Both are legitimate to leave as-is for now; neither blocks any current workflow. If revisited,
that work must also update Help in the same change set (see `VERIFY.md`, item 8).

## Help entries requiring review when Module 3 start/reconciliation behaviour changes

**Inherited checklist for the upcoming Sadia start/reconciliation contract change set.** Operator
Help (`workbench/templates/help.html`) documents today's behaviour for the topics below. When the
Module 3 start/reconciliation contract behaviour changes, each of these Help topics must be
re-examined **in that same change set** and updated if its current wording becomes stale. This
list exists so that future change inherits an explicit set of Help material to reconsider rather
than relying on someone to remember which paragraphs went stale.

- **§17 troubleshooting — `start_unresolved`** (and the matching Run-screen guidance in
  `static/app.js`): today Module 1 records the ambiguity and will not retry. Revisit when
  **read-only reconciliation of ambiguous starts through `events`/`result`** is implemented.
- **§17 troubleshooting — `start_rejected`**: today a Gateway start rejection is recorded verbatim
  with "no run was created." Revisit when **409 duplicate-`run_id` interpretation** is added.
- **§17 troubleshooting — `gateway_http_error`** and any wording about the **current start-5xx
  retry behaviour** (§17 notes "a transient 5xx is retried a small fixed number of times within
  the run deadline"). Revisit if the **start-5xx retry policy** changes.
- **§13 cancellation — cancel-delivery `acknowledged` semantics**: revisit alongside any
  reconciliation change that lets Module 1 learn more about a cancelled run's true state.
- **§14 recovery and reattachment**: the reattachment cases described are today's behaviour;
  revisit if reconciliation changes what recovery can determine or do.

**Explicitly still frozen — do NOT pre-document in Help:** 409 duplicate-`run_id` interpretation;
read-only reconciliation of ambiguous starts through `events`/`result`; any future start-5xx
retry policy; **`/alive`** (no current Module 1 behaviour — it must not appear in Help at all).
`test_help_http.py::test_help_excludes_frozen_future_gateway_topics` guards `/alive` and `409`
against accidental appearance.

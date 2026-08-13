# Module 1 — final requirement-coverage audit (end of 3C-3)

Every numbered Module 1 requirement from
`KnowledgeForge-Architecture-and-Requirements-Draft-1.0.docx`, traced to the step that
closed it, the implementation, and the test evidence. Status is one of **Satisfied**,
**Satisfied (interpretation)** — with a pointer to `REQUIREMENTS_CLARIFICATIONS.md` — or
**Deferred**. No requirement is Deferred.

Clarification pointers use `RC:` for `REQUIREMENTS_CLARIFICATIONS.md`.

## PKG — package assembly

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| PKG-1 | Operator selects the entry; the rest is discovered | 1, refined 3C-1 | `packages.assemble` (entry now declared in `package.yaml`) | test_discovery, test_packages_registry | Satisfied (interpretation — entry declared in the manifest; RC: minimal package format, `PACKAGE_FORMAT.md`) |
| PKG-2 | Name/version/metadata from front matter | 1 | `packages.assemble` / `parse_front_matter` | test_discovery::test_reading_metadata_from_front_matter | Satisfied |
| PKG-3 | Dependencies from front matter + relative links | 1 | `packages._declared_and_linked` | test_discovery, test_packages_registry::test_claims_uses_relative_links… | Satisfied |
| PKG-4 | Discovery never resolves outside the root | 1 | `packages._safe_rel` + visit guard | test_discovery::test_path_outside_root_is_refused | Satisfied |
| PKG-5 | Missing files / cycles reported, not skipped | 1 | `packages.assemble` Problems | test_discovery::test_missing_link…, test_circular_reference… | Satisfied |
| PKG-6 | Deterministic ordered file list | 1 | entry-first then sorted | test_discovery::test_order_is_main_first_then_sorted | Satisfied |
| PKG-7 | Fingerprint changes on content/order change | 1 | `fingerprints.package_fingerprint` | test_fingerprints | Satisfied |
| PKG-8 | Snapshot stored immutably; evidence refers to it | 3C-2/3C-3 | `db.save_snapshot` at run start | test_history_http (snapshot-at-start), test_approval_http (immutable after change) | Satisfied |

## TSK — tasks

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| TSK-1 | Tasks are JSON, one per file, versioned with the package | 1 | `tasks.load_tasks` | test_discovery / examples | Satisfied |
| TSK-2 | Task carries id/title/description/business area/difficulty/… | 1 | `models.Task` | examples, test_verdict | Satisfied |
| TSK-3 | Zero or more checks (command + exit code) | 1 | `Task.checks`, `verdict` | test_verdict | Satisfied |
| TSK-4 | Task fingerprint changes on description/checks change | 1 | `fingerprints.task_fingerprint` | test_fingerprints | Satisfied |
| TSK-5 | Active/inactive; only active count toward approval | 1 + 3C-3 | `task_state`, `status.package_status` | test_status_unit::test_inactive_task_excluded, test_approval_http | Satisfied |

## EXE — execution

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| EXE-1 | One run = one task × one snapshot | 2A | `orchestrator.start_run`/`build_request` | test_run_integration_http | Satisfied |
| EXE-2 | Workbench sends the snapshot; Gateway does not discover | 2A | `request.package.files` | test_run_integration_http | Satisfied |
| EXE-3 | Run targets a named logical environment | 2A + 3C-3 | `execution_context.target_environment` (from profile) | test_approval_http | Satisfied |
| EXE-4 | Only listed capabilities are enabled | 2A | `permitted_capabilities` in request | test_run_integration_http | Satisfied (Module 1 declares; Module 3 enforces) |
| EXE-5 | Progress events stream to the UI while in flight | 2A | SSE `stream` + poller | test_run_reconnect_http, test_run_integration_http | Satisfied |
| EXE-6 | Operator can cancel a running validation | 2A/3B-1 | `orchestrator.request_cancel` | test_termination_http, test_cancel_delivery_http | Satisfied |
| EXE-7 | A run has a timeout; stopped and recorded | 3A | authoritative deadline → `timed_out` | test_termination_http, test_timeout_provenance_http | Satisfied (interpretation — Module-1 timeout is an error/effective-inconclusive, distinct from a Gateway-reported technical failure; RC: timeout policy/provenance) |
| EXE-8 | Every run produces a result, incl. fail/cancel | 2B | durable terminal outcome; never fabricated | test_hostile_gateway_http | Satisfied (interpretation — durable terminal state, real result or authored error; RC: EXE-8) |

## VER — verdict

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| VER-1 | Workbench derives the outcome; Gateway never supplies one | 2A | `verdict.derive_verdict` | test_verdict | Satisfied |
| VER-2 | Outcomes: passed/failed/needs_review/inconclusive/cancelled | 2A | `verdict`, `vocab.OUTCOMES` | test_verdict, test_help_http | Satisfied |
| VER-3 | Technical failure → inconclusive | 2A | rule 2 | test_verdict | Satisfied |
| VER-4 | Declared checks pass only if every check passed | 2A | rules 4/6 | test_verdict | Satisfied |
| VER-5 | No declared checks → needs_review | 2A | rule 3 | test_verdict | Satisfied |
| VER-6 | needs_review resolved by a named human (passed/failed, time) | 3C-3 | `db.set_review_resolution`, `/runs/{id}/review` | test_approval_http::test_needs_review_lifecycle | Satisfied |
| VER-7 | Human resolution stored alongside machine evidence, distinct | 3C-3 | `review_resolution`, run screen | test_approval_http (machine verdict unchanged; effective differs) | Satisfied |

## EVD — evidence

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| EVD-1 | Raw evidence preserved for every run and reachable from the UI | 2B/3C-2 | events + result + run/history screens | test_history_http, test_diagnosis_rendering | Satisfied (interpretation — artifact **names** are references; the contract carries no artifact **content**; RC: EVD-1 + Gateway-owner item) |
| EVD-2 | Diagnosis presented separately, marked as interpretation | 2B | `_result.html` | test_diagnosis_rendering | Satisfied |
| EVD-3 | Diagnosis never determines the outcome by itself | 2A | `verdict` ignores diagnosis | test_verdict | Satisfied |
| EVD-4 | Knowledge diagnosis references specific package files/sections | 2B | Module 3-authored diagnosis, displayed verbatim | test_diagnosis_rendering | Satisfied (Module 3 content responsibility; Module 1 preserves/displays) |
| EVD-5 | Recommendations phrased as package changes, not code | 2B | Module 3-authored diagnosis, displayed verbatim | test_diagnosis_rendering | Satisfied (Module 3 content responsibility) |

## HST — history & staleness

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| HST-1 | Every run retained with fingerprints, evidence, outcome | 3C-2 | `run` table + package-centric history | test_history_http | Satisfied |
| HST-2 | Stale if package or task fingerprint no longer matches current | 3C-3 | `status.validation_context_id` | test_status_unit, test_approval_http | Satisfied (interpretation — **extended** to package+task+capabilities+environment; RC: extended validation-context staleness) |
| HST-3 | Stale runs remain visible, clearly marked, never auto-deleted | 3C-3 | history/detail/run badges; runs never deleted | test_approval_http (historical unchanged) | Satisfied |
| HST-4 | Stale runs do not count toward approval | 3C-3 | `status.package_status` eligibility | test_status_unit, test_approval_http | Satisfied |

## APR — approval

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| APR-1 | Eligible when every active task has a latest passed, non-stale run | 3C-3 | `status.package_status` | test_status_unit (candidate selection, sequences), test_approval_http (override/needs_review) | Satisfied (interpretation — **current-context candidate set first**, then skip inconclusive/cancelled, unresolved needs_review blocks, decisive passed/failed; overrides don't disqualify; RC: APR-1 candidate-selection, extended staleness) |
| APR-2 | Approval is explicit; never automatic | 3C-3 | `app.approve_package` (POST + server re-check) | test_approval_http (ineligible→400) | Satisfied |
| APR-3 | Approval records who/when/package fp/task fps | 3C-3 | `db.add_approval` | test_approval_http (survives restart) | Satisfied (also records profile context — superset; RC) |
| APR-4 | Changing the package or any active task invalidates approval | 3C-3 | derived currency, never destructive | test_approval_http matrix | Satisfied (interpretation — also profile env/caps; RC) |

## UI

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| UI-1 | First usable version is visible, not a REST backend | 1 | Jinja server-rendered screens | manual + all HTTP tests | Satisfied |
| UI-2 | Live progress readable without opening the transcript | 2A | run screen event stream | test_run_reconnect_http | Satisfied |
| UI-3 | Every conclusion one click from its evidence | 2B/3C-2 | run screen + history → run links | test_history_http | Satisfied |
| UI-4 | Package view shows every task and its current status at a glance | 3C-3 | package_detail per-task status | test_approval_http (detail HTML) | Satisfied |
| UI-5 | Approval control disabled, reason stated, when ineligible | 3C-3 | package_detail approve button + reason | test_approval_http | Satisfied |
| UI-6 | Stale results visually distinct from current | 3C-3 | badges (passing·stale, “no longer current”) | test_approval_http | Satisfied |

## NFR

| ID | Requirement | Step | Implementation | Evidence | Status |
|----|-------------|------|----------------|----------|--------|
| NFR-1 | Runs with no enterprise access, against the mock | 1–3 | dev mock, `MOD3_BASE_URL` | all HTTP tests | Satisfied |
| NFR-2 | Runs take minutes; UI stays responsive, no long-held request | 2A | SSE + background poller | test_run_reconnect_http | Satisfied |
| NFR-3 | Reconnect resumes the stream without losing earlier events | 2A/3B-2 | replay + Last-Event-ID resume | test_sse_resume_http, test_run_reconnect_http | Satisfied |
| NFR-4 | No secrets/credentials/real packages/customer data/internal hostnames | all | synthetic examples; scans each commit | pre-commit scans, NFR-5 examples | Satisfied |
| NFR-5 | Example packages/tasks are synthetic | 1/3C-1 | `examples/larkspur`, `examples/claims` | present | Satisfied |
| NFR-6 | V1 trusted environment, few operators; no auth/RBAC | 3C-1/3C-3 | trusted-localhost; operator-entered names | RC: trusted-local-path (NFR-6) | Satisfied (interpretation — recorded) |
| NFR-7 | Concurrent runs permitted, not optimised; correctness first | 2A/3A | per-run poller, write-once terminal | test_timeout_cancel_race_http | Satisfied |

## Summary

All 54 numbered Module 1 requirements (PKG 1–8, TSK 1–5, EXE 1–8, VER 1–7, EVD 1–5,
HST 1–4, APR 1–4, UI 1–6, NFR 1–7) have an accountable **Satisfied** disposition.
**Nothing is deferred.** Interpretations that read a requirement more strictly or in a
particular way (PKG-1, EXE-7, EXE-8, EVD-1, HST-2, APR-1/3/4, NFR-6) are each recorded in
`REQUIREMENTS_CLARIFICATIONS.md`, so the audit distinguishes intentional interpretation
from accidental implementation.

## Open Module 2 / Gateway-owner integration questions (separate from Module 1 completion)

These are **not** Module 1 gaps; they are contract-adjacent questions for Sadia / a future
additive Module 2 version (all recorded in `REQUIREMENTS_CLARIFICATIONS.md`):

1. Idempotent `start` on the client `run_id` and/or lookup-by-`run_id` (ambiguous-start recovery).
2. Repeated-`cancel` semantics (currently undefined).
3. Result-availability timing after a terminal event (currently a lower bound only).
4. Slot/workspace isolation for orphaned runs (evidence-integrity question).
5. Artifact **contents** are not carried by the contract (only names) — an additive capability if reachable artifact content is ever required.
6. (Observed, non-blocking) fresh-run vs recovery-path transient-unreachability asymmetry.

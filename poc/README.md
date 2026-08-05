# KnowledgeForge POC — Module 2 contract demonstration

A small, **disposable** project that proves the Module 2 contract boundary end
to end:

> A `ValidationRequest` crosses from Module 1 to Module 3; `ExecutionEvent`s and
> a `ValidationResult` come back; **both sides independently validate every
> message against the schemas in [`../contract/`](../contract/)**; and Module 1
> alone derives the verdict.

It exists so the Execution Gateway (Module 3) can be built against a working
reference, and so we can see exactly what crosses the boundary. It is **not** the
production Module 1.

## What it proves

- **The boundary is real.** `KFPOCMod1.py` (Module 1) and `KFPOCMod3.py`
  (Module 3) share no Python code and never import each other. They communicate
  only through Module 2 JSON, over HTTP on localhost. Module 1 points at Module 3
  via the `MOD3_BASE_URL` environment variable — the one setting Sadia changes to
  aim it at the real Gateway.
- **Both sides enforce the contract.** Module 1 validates the request before
  sending and every event and result it receives; Module 3 validates the request
  on receipt and every event and result before sending. Each side loads the four
  schemas straight from `../contract/` (the `$id`s are bare filenames and refs
  are relative, so no URL mapping is needed). The UI shows both sides' validation.
- **Module 3 reports facts; Module 1 draws the conclusion.** The result carries
  no outcome. Module 1 derives the verdict locally with the ordered rules 1–6.

## The synthetic domain

`fixtures/package/larkspur/` is an invented subscription-billing knowledge
package with a **deliberate gap**: `data/account-model.md` defines
`entitlement_flags` on the subscription record, and `data/billing-ledger.md`
prices invoice lines from a ratecard — but nothing documents how an entitlement
flag becomes a billed line. A run against the "add a billable entitlement" task
can plausibly stop with a `missing_technical_mapping` diagnosis, which is exactly
the kind of fixable documentation defect KnowledgeForge exists to surface.

## Layout

```
poc/
├── KFPOCMod1.py         Module 1 — Validation Workbench (assembly, fingerprints,
│                        request validation, verdict, the UI, the Module 3 client)
├── KFPOCMod3.py         Module 3 — MOCK Execution Gateway  ← the file Sadia replaces
├── validate_fixtures.py standalone: validate every fixture against the schemas
├── requirements.txt     fastapi, uvicorn, jsonschema (plus stdlib)
└── fixtures/
    ├── package/larkspur/ the Markdown knowledge package (index + 3 docs)
    ├── tasks/            task-with-checks.json, task-no-checks.json
    └── results/          four canned outcomes + matching event sequences
                          (success, check-failure, knowledge-gap, technical-failure)
```

## How to run it

Requires **Python 3.10+**. The POC is two small local web servers — Module 3
(the mock Gateway) and Module 1 (the Workbench UI) — that talk to each other over
localhost. You run each in its own terminal and leave both running. All commands
are run from the `poc/` directory.

**Step 1 — one-time setup** (create a virtualenv and install the three
dependencies). From the repository root:

```bash
cd poc
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Optional check — every fixture should report `VALID`:

```bash
./.venv/bin/python validate_fixtures.py
```

**Step 2 — start Module 3** (the mock Gateway, port 8003). In a first terminal:

```bash
cd poc
./.venv/bin/uvicorn KFPOCMod3:app --port 8003
```

**Step 3 — start Module 1** (the Workbench UI, port 8001). In a second terminal:

```bash
cd poc
./.venv/bin/uvicorn KFPOCMod1:app --port 8001
```

Leave both terminals running. Stop either server with `Ctrl+C`.

**Step 4 — open the UI:** http://127.0.0.1:8001

Pick a package and task, optionally force an outcome, then click **Assemble &
Validate** and **Send to Module 3 ▶**. Events stream in, the result and verdict
follow, and every message shows a validation badge for each side of the boundary.
**Cancel run** genuinely interrupts an in-flight run.

`MOD3_BASE_URL` (default `http://127.0.0.1:8003`) tells Module 1 where Module 3
lives — the only setting that changes when Module 3 is real. To point Module 1 at
a different Gateway, set it before Step 3, e.g.
`MOD3_BASE_URL=http://host:port ./.venv/bin/uvicorn KFPOCMod1:app --port 8001`.

### The forced-outcome control

The UI has a **POC-only** outcome selector (Random, Success, Check failure,
Knowledge gap, Technical failure). It is **not** a contract field — the request
schema is `additionalProperties:false`, so it is passed out of band as a
`?forced_outcome=` query parameter on `POST /runs`. Forcing an outcome never
skips validation; the request is still assembled and schema-checked the same way.

### The verdict rules (Module 1 only)

Evaluated in order; first match wins. "Checks declared" is read from the
**submitted task**, not from whether `check_results` is empty.

| # | Condition | Verdict |
|---|---|---|
| 1 | run status `cancelled` | `cancelled` |
| 2 | run status `failed` (technical failure) | `inconclusive` |
| 3 | no checks declared in the task | `needs_review` |
| 4 | a declared check ran and failed | `failed` |
| 5 | a declared check did not run (no entry, or `exit_code` null) | `needs_review` |
| 6 | all declared checks ran and passed | `passed` |

The six can be walked in the UI:

- **1** Success, then click **Cancel run** mid-flight.
- **2** Technical failure.
- **3** Knowledge gap + `task-no-checks.json`.
- **4** Check failure + `task-with-checks.json`.
- **5** Knowledge gap + `task-with-checks.json`.
- **6** Success + `task-with-checks.json`.

## Note to Sadia

**`KFPOCMod3.py` is the file you replace.** It is a mock: it does no real work,
picks a canned outcome, and replays it. What it demonstrates — and what the real
Gateway must also do — is the contract behaviour:

- the four operations (`start`, `events`, `result`, `cancel`) with `start`
  returning a `run_id` immediately and the result available only once the run is
  terminal;
- asynchronous execution — events over time, not one blocking call;
- **validate every request on receipt, and every event and result before
  sending**, failing loudly if a message doesn't conform;
- exactly one terminal event per run (`completed` / `failed` / `cancelled`), with
  `cancel` a real interruption.

Everything inside Module 3 — environment resolution, Claude Code, MCP, connectors,
checks, diagnosis — is yours and stays behind this boundary. `KFPOCMod1.py` and
everything under `../contract/` are the fixed points you build against;
`../contract/` is frozen.

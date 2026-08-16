# KnowledgeForge

KnowledgeForge answers one question about a business-domain **knowledge package**
— a set of Markdown documents covering one domain's rules, data models, and
implementation guidance:

> Does this package contain enough information for a competent engineer, or an
> AI agent, to do real engineering work in that domain?

It answers it empirically: it takes a representative engineering task, runs it
with Claude Code against **only** the knowledge package plus a set of explicitly
permitted capabilities, and keeps the evidence. If the task succeeds, the
package gains confidence. If it doesn't, the run explains the likely reason —
often a specific, fixable gap in the documentation. The subject under test is
the *package*, not the model.

## Architecture — three modules

KnowledgeForge is one application split into three modules so they can be built
independently. Modules 1 and 3 depend only on Module 2; neither depends on the
other.

| Module | Name | Responsibility |
|---|---|---|
| 1 | **Validation Workbench** | Web UI, package assembly, task management, run history, the validation verdict, approval. |
| 2 | **Validation Contract** | The shared JSON messages and operations the other two use to talk. Not code. |
| 3 | **Execution Gateway** | Prepares the environment, runs Claude Code, routes connectors, runs checks, reports progress and evidence. |

The Workbench sends a `ValidationRequest`; the Gateway streams `ExecutionEvent`s
while the work happens, then returns one `ValidationResult`. The Workbench alone
turns that result into a verdict — the Gateway reports observable facts and
never decides the outcome.

## Repository layout

- **`contract/`** — Module 2: the shared JSON contract. Four JSON Schemas, a
  one-page description of the four operations, and canonical example messages.
  This is the single source of truth both other modules validate against.
- **`poc/`** — a small, **disposable** end-to-end demonstration that a request
  crosses the Module 2 boundary and events plus a result come back, with both
  sides validating against the schemas. It uses a synthetic domain and a mock
  Gateway. See [`poc/README.md`](poc/README.md).

- **`workbench/`** — Module 1: the production Validation Workbench (server-rendered
  FastAPI + Jinja + SSE, SQLite). See [`workbench/VERIFY.md`](workbench/VERIFY.md)
  and the in-app **Help** page.

## Status

The **Validation Workbench (Module 1)** is feature-complete against its 54 numbered
requirements (see `workbench/REQUIREMENTS_AUDIT.md`). `contract/` (Module 2) is the
frozen shared contract; `poc/` is a disposable end-to-end demonstration. The
production Execution Gateway (Module 3) is built separately; Module 1 reaches it over
HTTP at `MOD3_BASE_URL`, and a dev mock (`tools/mock_gateway/`) stands in locally.

## Running the Validation Workbench

The Workbench is pure Python — no Node, no build step. **Supported interpreter:
Python 3.12** (`>=3.12,<3.13`; recorded in `.python-version`). Mac and Windows
integration are both verified on Python 3.12; Python 3.14 is not currently supported
for Windows integration (our validation suite exhibits materially more Windows asyncio
accept failures under it — see `workbench/REQUIREMENTS_CLARIFICATIONS.md`). Dependencies
are in `workbench/requirements.txt` (direct deps, pinned exactly); for a reproducible
environment install the full pinned closure instead:
`pip install -r workbench/requirements.lock`.

**`MOD3_BASE_URL` is the only configuration required to point Module 1 at a Module 3
Gateway** (default `http://127.0.0.1:8003`, the dev mock). Pointing at the real Gateway
on another host is one environment variable — e.g. `https://gateway.internal:8443`.
(DNS, firewall/proxy egress, VPN and — for an internal-CA HTTPS Gateway — trusting that
CA in the OS trust store are network-environment concerns, not application settings.)

### macOS / Linux

```bash
python3.12 -m venv workbench/.venv
./workbench/.venv/bin/pip install -r workbench/requirements.txt   # or requirements.lock for the exact pinned closure
export MOD3_BASE_URL=http://127.0.0.1:8003        # or the real Gateway URL
./workbench/.venv/bin/uvicorn workbench.app:app --port 8010
# tests:
./workbench/.venv/bin/python -m pytest workbench/tests -q
```

### Windows

The Workbench runs natively on Windows. The differences are the venv layout (`Scripts\`
instead of `bin/`), how environment variables are set, and — importantly — **how you
start it**. On Windows, start Module 1 with the launcher **`python -m
workbench.run_workbench`**, not a bare `uvicorn` command. The launcher selects a
Selector event loop; the default Proactor loop can be left alive-but-unable-to-accept by
an aborted incoming connection (see `workbench/REQUIREMENTS_CLARIFICATIONS.md`, "Windows
event loop"). Startup prints `[workbench] serving on event loop: _WindowsSelectorEventLoop`
— confirm that line. (`run_workbench` works on every OS; off Windows it uses the default
loop.)

**PowerShell**
```powershell
py -3.12 -m venv workbench\.venv
workbench\.venv\Scripts\python -m pip install -r workbench\requirements.txt   # or requirements.lock
$env:MOD3_BASE_URL = "http://127.0.0.1:8003"      # or the real Gateway URL
workbench\.venv\Scripts\python -m workbench.run_workbench          # protected Windows launch (Selector loop)
# tests:
workbench\.venv\Scripts\python -m pytest workbench\tests -q
```

**Command Prompt (cmd.exe)**
```bat
py -3.12 -m venv workbench\.venv
workbench\.venv\Scripts\python -m pip install -r workbench\requirements.txt
set MOD3_BASE_URL=http://127.0.0.1:8003
workbench\.venv\Scripts\python -m workbench.run_workbench
```
`$env:MOD3_BASE_URL` (PowerShell) and `set MOD3_BASE_URL=` (cmd) each set the variable
for that shell only. To activate the venv instead of calling its `python` directly:
`workbench\.venv\Scripts\Activate.ps1` (PowerShell) or `workbench\.venv\Scripts\activate.bat`
(cmd); the macOS/Linux equivalent is `source workbench/.venv/bin/activate`.

**A few tests skip on Windows, by design.** Two tests pause a live mock process with
POSIX `SIGSTOP`/`SIGCONT` to simulate a Gateway that is unreachable-but-intact; those
signals do not exist on Windows, so the two tests report `skipped` (not failed) with a
clear reason. Everything they protect that *can* be exercised without job control —
recovery bookkeeping, cancellation-delivery states, the transport classifier — is
covered by platform-neutral tests that still run. A green run on Windows shows a small
skip count; that is expected.

### Moving a package folder between machines (Change root)

A package's **durable identity** (`package_id`, its validation history and approvals)
is independent of *where* the folder currently lives. When you copy a package folder to
a different machine — e.g. from a Mac to a Windows work machine — the old registered
path no longer exists there, so that registration shows **Unhealthy — "Registered root
no longer exists."** This is expected, not corruption.

The repair is **Change root** on the package detail page: point the registration at the
folder's new absolute location (e.g. `C:\KnowledgeForge\packages\claims`). The new root
must be a valid package whose manifest declares the **same** `package_id`; Module 1
verifies that and refuses a mismatch. Only the machine-local location moves —
`package_id`, the validation profile, run history, immutable snapshots and approval
history are all preserved, and because the knowledge bytes are unchanged, nothing
becomes stale from the move. (Line endings don't matter: fingerprints normalise
CRLF/LF, so the same content hashes identically on Windows and macOS.)

If you'd rather not carry a database across machines at all, start clean instead: bring
only the code and the package folders, create a fresh venv, and register each package
root and configure its profile fresh. See `workbench/VERIFY.md` for the transfer
checklist and the trade-off between the two approaches.

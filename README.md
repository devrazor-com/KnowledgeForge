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

## Status

**This repository currently contains a proof-of-concept, not the production
application.** The POC exists to prove the Module 2 contract and to give the
Execution Gateway a working reference to build the real Gateway against. The
production Validation Workbench and Execution Gateway are built separately.

# KnowledgeForge POC — Module 2 contract demonstration

A small, **disposable** project that proves the Module 2 contract boundary: a
`ValidationRequest` crosses from Module 1 to a mock Module 3, `ExecutionEvent`s
and a `ValidationResult` come back, and **both sides independently validate
every message against the schemas in [`../contract/`](../contract/)**.

It uses a synthetic domain (`Larkspur`, a subscription-billing package) and a
mocked Execution Gateway. No Claude API, no MCP, no network beyond localhost.

> **Status:** work in progress. Step 1 (fixtures + schema validator) is in place.
> The two services (`KFPOCMod1.py`, `KFPOCMod3.py`) and full run instructions
> are added in the following steps; this README is completed then.

## What exists today

- `fixtures/` — a synthetic knowledge package, two tasks, and three canned
  outcomes with matching event sequences (Module 3's response pool).
- `validate_fixtures.py` — validates every fixture against the frozen schemas.

```bash
cd poc
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python validate_fixtures.py
```

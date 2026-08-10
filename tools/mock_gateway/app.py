"""Mock Execution Gateway — happy path only (Step 2A).

Implements the contract operations Module 1 needs for one clean run:
    POST /runs                  (start)  — validate request, accept, return run_id at once
    GET  /runs/{run_id}/events  (events) — events after ?since=N, in order
    GET  /runs/{run_id}/result  (result) — nothing until the run is terminal

It genuinely validates the incoming request on receipt and every event and result
against the frozen contract schemas before sending, failing loudly otherwise. Its
own validation status is reported OUT OF BAND (a `module3_validation` field on the
transport envelope, never inside a contract message) — a development convenience
the Workbench labels "Mock-only".

Cancellation, timeouts, and hostile behaviours are NOT here: cancellation/edges are
Step 3, hostile behaviours are Step 2B.

Run from the repository root:
    ./workbench/.venv/bin/uvicorn tools.mock_gateway.app:app --port 8003
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE.parents[1] / "contract"     # repo_root/contract
FIXTURES = HERE / "fixtures"

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
EVENT_DELAY_SECONDS = 0.6

OUTCOME_POOL = {
    "success": ("events-success.json", "result-success.json"),
    "check_failure": ("events-check-failure.json", "result-check-failure.json"),
    "knowledge_gap": ("events-knowledge-gap.json", "result-knowledge-gap.json"),
    "technical_failure": ("events-technical-failure.json", "result-technical-failure.json"),
}

_SCHEMA_FILES = [
    "validation-request.schema.json", "execution-event.schema.json",
    "validation-result.schema.json", "failure-diagnosis.schema.json",
]


def _registry() -> Registry:
    res = []
    for name in _SCHEMA_FILES:
        c = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
        res.append((c["$id"], Resource.from_contents(c)))
    return Registry().with_resources(res)


_REG = _registry()
_REQUEST = Draft202012Validator({"$ref": "validation-request.schema.json"}, registry=_REG)
_EVENT = Draft202012Validator({"$ref": "execution-event.schema.json"}, registry=_REG)
_RESULT = Draft202012Validator({"$ref": "validation-result.schema.json"}, registry=_REG)


def _errors(v: Draft202012Validator, inst: Any) -> list[str]:
    out = []
    for e in sorted(v.iter_errors(inst), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "(root)"
        out.append(f"{loc}: {e.message}")
    return out


def _validate_or_die(v: Draft202012Validator, inst: Any, kind: str) -> None:
    errs = _errors(v, inst)
    if errs:
        print(f"\n!!! MOCK GATEWAY refusing to send an invalid {kind} !!!", file=sys.stderr)
        for e in errs:
            print(f"    - {e}", file=sys.stderr)
        raise RuntimeError(f"mock produced an invalid {kind}: {errs}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_pool() -> dict:
    pool = {}
    for name, (ev, res) in OUTCOME_POOL.items():
        pool[name] = {
            "events": json.loads((FIXTURES / ev).read_text(encoding="utf-8")),
            "result": json.loads((FIXTURES / res).read_text(encoding="utf-8")),
        }
    return pool


_POOL = _load_pool()


class Run:
    def __init__(self, run_id: str, outcome: str):
        self.run_id = run_id
        self.planned = _POOL[outcome]["events"]
        self.result_template = _POOL[outcome]["result"]
        self.emitted: list[dict] = []
        self.result: dict | None = None
        self.terminal = False
        self.lock = asyncio.Lock()


RUNS: dict[str, Run] = {}


async def _emit(run: Run) -> None:
    for tmpl in run.planned:
        await asyncio.sleep(EVENT_DELAY_SECONDS)
        async with run.lock:
            ev = {"run_id": run.run_id, "sequence": len(run.emitted) + 1, "timestamp": _now(),
                  "event_type": tmpl["event_type"], "message": tmpl["message"]}
            if tmpl.get("details") is not None:
                ev["details"] = tmpl["details"]
            _validate_or_die(_EVENT, ev, "event")
            run.emitted.append(ev)
            if ev["event_type"] in TERMINAL_EVENTS:
                result = dict(run.result_template)
                result["run_id"] = run.run_id
                _validate_or_die(_RESULT, result, "result")
                run.result = result
                run.terminal = True
                return


app = FastAPI(title="Mock Execution Gateway (dev-only)")


@app.get("/")
def root():
    return {"module": "mock_gateway", "outcomes": list(_POOL), "active_runs": len(RUNS)}


@app.post("/runs")
async def start(request: dict = Body(...), forced_outcome: str | None = None):
    errs = _errors(_REQUEST, request)
    if errs:
        return JSONResponse(status_code=400, content={
            "rejected": True, "reason": "ValidationRequest failed schema validation on receipt",
            "module3_validation": {"passed": False, "errors": errs}})
    run_id = request["run_id"]
    outcome = forced_outcome if forced_outcome in _POOL else random.choice(list(_POOL))
    run = Run(run_id, outcome)
    RUNS[run_id] = run
    asyncio.create_task(_emit(run))
    return JSONResponse({
        "run_id": run_id, "accepted": True, "outcome_selected": outcome,
        "module3_validation": {"passed": True,
            "message": "Mock validated the request against validation-request.schema.json on receipt"}})


@app.get("/runs/{run_id}/events")
async def events(run_id: str, since: int = 0):
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run_id")
    async with run.lock:
        evs = [e for e in run.emitted if e["sequence"] > since]
        terminal = run.terminal
    return JSONResponse({"events": evs, "terminal": terminal,
        "module3_validation": {"passed": True, "count": len(evs),
            "message": "Mock validated each event against execution-event.schema.json before sending"}})


@app.get("/runs/{run_id}/result")
async def result(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run_id")
    async with run.lock:
        if not run.terminal or run.result is None:
            return JSONResponse({"result": None, "module3_validation": None})
        return JSONResponse({"result": run.result,
            "module3_validation": {"passed": True,
                "message": "Mock validated the result against validation-result.schema.json before sending"}})

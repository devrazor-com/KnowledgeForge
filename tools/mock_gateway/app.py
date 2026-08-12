"""Mock Execution Gateway — happy path plus development-only fault injection.

Implements the contract operations Module 1 needs:
    POST /runs                  (start)
    GET  /runs/{run_id}/events  (events)
    GET  /runs/{run_id}/result  (result)

It genuinely validates the incoming request and every outbound event/result
against the frozen contract schemas, failing loudly otherwise. Its own validation
status is reported OUT OF BAND (a `module3_validation` field on the envelope,
never inside a contract message) — the Workbench labels it "Mock-only".

Development-only, out-of-band `fault` query parameter (Step 2B) makes the mock
misbehave on demand so Module 1's error handling can be exercised. Faults that
require sending an invalid message deliberately bypass the mock's own validation.
The Workbench only forwards `fault`/`forced_outcome` in dev/mock mode; a real
Gateway never receives them and they never appear in a Module 2 message.

Cancellation, timeouts, and stalls are NOT here (Step 3).

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
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE.parents[1] / "contract"
FIXTURES = HERE / "fixtures"

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
EVENT_DELAY_SECONDS = 0.6

OUTCOME_POOL = {
    "success": ("events-success.json", "result-success.json"),
    "check_failure": ("events-check-failure.json", "result-check-failure.json"),
    "check_failure_large": ("events-check-failure.json", "result-check-failure-large.json"),
    "knowledge_gap": ("events-knowledge-gap.json", "result-knowledge-gap.json"),
    "technical_failure": ("events-technical-failure.json", "result-technical-failure.json"),
}
# Outcomes eligible for a random pick (the oversized one is opt-in via forced_outcome).
RANDOM_OUTCOMES = ["success", "check_failure", "knowledge_gap", "technical_failure"]

# Tampered sequence numbers, emitted as one atomic burst so the anomaly is
# delivered in a single events batch (a lower/duplicate sequence would otherwise be
# filtered out by the `sequence > since` poll and never reach Module 1).
SEQUENCE_FAULTS = {"seq_gap": [1, 3], "seq_dup": [1, 2, 2], "seq_ooo": [1, 3, 2]}

_SCHEMA_FILES = ["validation-request.schema.json", "execution-event.schema.json",
                 "validation-result.schema.json", "failure-diagnosis.schema.json"]


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
        pool[name] = {"events": json.loads((FIXTURES / ev).read_text(encoding="utf-8")),
                      "result": json.loads((FIXTURES / res).read_text(encoding="utf-8"))}
    return pool


_POOL = _load_pool()


class Run:
    def __init__(self, run_id: str, outcome: str, fault: str | None):
        self.run_id = run_id
        self.planned = _POOL[outcome]["events"]
        self.result_template = _POOL[outcome]["result"]
        self.fault = fault
        self.emitted: list[dict] = []
        self.result: dict | None = None
        self.terminal = False
        self.lock = asyncio.Lock()


RUNS: dict[str, Run] = {}


def _mk_event(run: Run, template: dict, sequence: int) -> dict:
    ev = {"run_id": run.run_id, "sequence": sequence, "timestamp": _now(),
          "event_type": template["event_type"], "message": template["message"]}
    if template.get("details") is not None:
        ev["details"] = template["details"]
    return ev


async def _emit(run: Run) -> None:
    fault = run.fault

    if fault in SEQUENCE_FAULTS:  # tamper the sequence numbers (events stay schema-valid)
        await asyncio.sleep(EVENT_DELAY_SECONDS)
        async with run.lock:  # emit the whole tampered prefix atomically (one batch)
            for i, seq in enumerate(SEQUENCE_FAULTS[fault]):
                tmpl = run.planned[min(i, len(run.planned) - 1)]
                ev = _mk_event(run, tmpl, seq)
                _validate_or_die(_EVENT, ev, "event")
                run.emitted.append(ev)
        return  # never reaches terminal; Module 1 stops on the anomaly

    if fault == "invalid_event":  # one valid event, then a deliberately schema-invalid one
        await asyncio.sleep(EVENT_DELAY_SECONDS)
        async with run.lock:
            ev = _mk_event(run, run.planned[0], 1)
            _validate_or_die(_EVENT, ev, "event")
            run.emitted.append(ev)
        await asyncio.sleep(EVENT_DELAY_SECONDS)
        async with run.lock:
            run.emitted.append({"run_id": run.run_id, "sequence": 2, "timestamp": _now(),
                                "event_type": "totally_invalid_type",
                                "message": "deliberately schema-invalid event"})  # bypass validation
        return

    # Normal emission (also used for http_* / malformed / invalid_result faults,
    # which are injected at the events/result endpoints, not here).
    for tmpl in run.planned:
        await asyncio.sleep(EVENT_DELAY_SECONDS)
        async with run.lock:
            ev = _mk_event(run, tmpl, len(run.emitted) + 1)
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
async def start(request: dict = Body(...), forced_outcome: str | None = None, fault: str | None = None):
    errs = _errors(_REQUEST, request)
    if errs:
        return JSONResponse(status_code=400, content={
            "rejected": True, "reason": "ValidationRequest failed schema validation on receipt",
            "module3_validation": {"passed": False, "errors": errs}})

    if fault == "reject":  # injected: reject the start with a reason; no run is created
        return JSONResponse(status_code=400, content={
            "rejected": True, "reason": "Injected fault: the Gateway declined this request for testing.",
            "module3_validation": {"passed": True, "message": "Mock validated the request; then rejected it (injected)"}})

    run_id = request["run_id"]
    outcome = forced_outcome if forced_outcome in _POOL else random.choice(RANDOM_OUTCOMES)
    run = Run(run_id, outcome, fault)
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
    if run.fault == "http_500":
        return JSONResponse(status_code=500, content={"error": "injected internal server error"})
    if run.fault == "http_404":
        return JSONResponse(status_code=404, content={"error": "injected not found"})
    if run.fault == "malformed":
        return Response(content='{ "events": [ this is not valid json ',
                        media_type="application/json", status_code=200)
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
        terminal = run.terminal
    if run.fault == "invalid_result" and terminal:
        # A deliberately schema-invalid result (status not in the enum). Bypass validation.
        return JSONResponse({"result": {
            "run_id": run_id, "status": "BOGUS_STATUS", "summary": "injected schema-invalid result",
            "check_results": [], "artifacts": [], "duration_seconds": 1}, "module3_validation": None})
    async with run.lock:
        if not run.terminal or run.result is None:
            return JSONResponse({"result": None, "module3_validation": None})
        return JSONResponse({"result": run.result,
            "module3_validation": {"passed": True,
                "message": "Mock validated the result against validation-result.schema.json before sending"}})

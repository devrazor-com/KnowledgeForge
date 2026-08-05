#!/usr/bin/env python3
"""KFPOCMod3 — a MOCK of Module 3 (Execution Gateway) for the KnowledgeForge POC.

>>> THIS IS THE FILE SADIA REPLACES. <<<

It exists only to prove the Module 2 contract from the Gateway side. It does no
real work: no Claude, no MCP, no connectors, no network beyond localhost. It
picks one of a pool of canned outcomes (forced out of band, or at random) and
replays the matching event sequence and result over a few seconds.

What it DOES do faithfully — because these are the contract behaviours the real
Gateway must also honour:

  * implements the four operations from contract/operations.md:
      POST   /runs                      (start)  — validate, accept, return run_id at once
      GET    /runs/{run_id}/events      (events) — events after ?since=N, in order
      GET    /runs/{run_id}/result      (result) — nothing until the run is terminal
      POST   /runs/{run_id}/cancel      (cancel) — really interrupt the run
  * asynchronous: start returns immediately; events appear over several seconds;
    the result is available only after the terminal event;
  * validates the incoming request on receipt, and validates every event and
    result against its schema BEFORE sending — failing loudly if one doesn't
    conform;
  * every run ends with exactly one of completed / failed / cancelled.

Cancellation is a REAL interruption: it stops the canned sequence part-way,
emits `cancelled` as the terminal event, and produces a status:"cancelled"
result. It does not let the run finish normally.

Run:
    ./.venv/bin/uvicorn KFPOCMod3:app --port 8003
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

POC_DIR = Path(__file__).resolve().parent
CONTRACT_DIR = POC_DIR.parent / "contract"
RESULTS_DIR = POC_DIR / "fixtures" / "results"

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
EVENT_DELAY_SECONDS = 0.8  # spacing between emitted events (whole run ~ a few seconds)

# The canned response pool. Each outcome maps to (events file, result file).
# These live under poc/fixtures/ — never under contract/.
OUTCOME_POOL = {
    "success": ("events-success.json", "result-success.json"),
    "check_failure": ("events-check-failure.json", "result-check-failure.json"),
    "knowledge_gap": ("events-knowledge-gap.json", "result-knowledge-gap.json"),
    "technical_failure": ("events-technical-failure.json", "result-technical-failure.json"),
}

# --------------------------------------------------------------------------
# Schema validation — Module 3's OWN copy. It shares no code with Module 1.
# --------------------------------------------------------------------------

_SCHEMA_FILES = [
    "validation-request.schema.json",
    "execution-event.schema.json",
    "validation-result.schema.json",
    "failure-diagnosis.schema.json",
]


def _build_registry() -> Registry:
    resources = []
    for name in _SCHEMA_FILES:
        contents = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()
_REQUEST_VALIDATOR = Draft202012Validator({"$ref": "validation-request.schema.json"}, registry=_REGISTRY)
_EVENT_VALIDATOR = Draft202012Validator({"$ref": "execution-event.schema.json"}, registry=_REGISTRY)
_RESULT_VALIDATOR = Draft202012Validator({"$ref": "validation-result.schema.json"}, registry=_REGISTRY)


def _errors(validator: Draft202012Validator, instance: Any) -> list[str]:
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        out.append(f"{loc}: {err.message}")
    return out


def _validate_or_die(validator: Draft202012Validator, instance: Any, kind: str) -> None:
    """Validate an OUTBOUND message before sending. Fail loudly if it doesn't conform."""
    errs = _errors(validator, instance)
    if errs:
        banner = f"\n!!! MODULE 3 CONTRACT VIOLATION — refusing to send an invalid {kind} !!!"
        print(banner, file=sys.stderr)
        for e in errs:
            print(f"    - {e}", file=sys.stderr)
        print(json.dumps(instance, indent=2), file=sys.stderr)
        raise RuntimeError(f"Module 3 produced an invalid {kind}: {errs}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Load the canned pool once at import.
# --------------------------------------------------------------------------

def _load_pool() -> dict:
    pool = {}
    for name, (ev_file, res_file) in OUTCOME_POOL.items():
        events = json.loads((RESULTS_DIR / ev_file).read_text(encoding="utf-8"))
        result = json.loads((RESULTS_DIR / res_file).read_text(encoding="utf-8"))
        pool[name] = {"events": events, "result": result}
    return pool


_POOL = _load_pool()


# --------------------------------------------------------------------------
# Run state
# --------------------------------------------------------------------------

class Run:
    def __init__(self, run_id: str, outcome: str):
        self.run_id = run_id
        self.outcome = outcome
        self.planned_events = _POOL[outcome]["events"]
        self.planned_result = _POOL[outcome]["result"]
        self.emitted: list[dict] = []
        self.result: dict | None = None
        self.terminal = False
        self.started = datetime.now(timezone.utc)
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None


RUNS: dict[str, Run] = {}


def _finalize_event(run: Run, template: dict, sequence: int) -> dict:
    """Stamp a canned event with this run's id, a fresh sequence and timestamp."""
    ev = {
        "run_id": run.run_id,
        "sequence": sequence,
        "timestamp": _now(),
        "event_type": template["event_type"],
        "message": template["message"],
    }
    if template.get("details") is not None:
        ev["details"] = template["details"]
    return ev


def _finalize_result(run: Run, override: dict | None = None) -> dict:
    result = dict(override if override is not None else run.planned_result)
    result["run_id"] = run.run_id
    return result


async def _emit(run: Run) -> None:
    """Replay the canned event sequence over time, validating each before sending.
    Stops early if the run was cancelled."""
    try:
        for template in run.planned_events:
            await asyncio.sleep(EVENT_DELAY_SECONDS)
            async with run.lock:
                if run.terminal:  # cancelled while we were sleeping
                    return
                ev = _finalize_event(run, template, len(run.emitted) + 1)
                _validate_or_die(_EVENT_VALIDATOR, ev, "event")
                run.emitted.append(ev)
                if ev["event_type"] in TERMINAL_EVENTS:
                    result = _finalize_result(run)
                    _validate_or_die(_RESULT_VALIDATOR, result, "result")
                    run.result = result
                    run.terminal = True
                    return
    except asyncio.CancelledError:
        return


def _choose_outcome(forced: str | None) -> str:
    if forced and forced in _POOL:
        return forced
    return random.choice(list(_POOL.keys()))


# --------------------------------------------------------------------------
# FastAPI app — the four contract operations
# --------------------------------------------------------------------------

app = FastAPI(title="KFPOCMod3 — Execution Gateway MOCK (POC)")


@app.get("/")
def root():
    return {"module": "KFPOCMod3 (mock Execution Gateway)",
            "note": "This is the file Sadia replaces with the real Gateway.",
            "outcomes": list(_POOL.keys()), "active_runs": len(RUNS)}


@app.post("/runs")
async def start(request: dict = Body(...), forced_outcome: str | None = None):
    """start — validate the incoming request, accept it, and return run_id at once.
    Does not wait for the work. forced_outcome is a POC-only out-of-band query
    parameter; it is NOT read from the request body (the contract has no such field)."""
    errs = _errors(_REQUEST_VALIDATOR, request)
    if errs:
        # Reject with a reason; no run is created.
        return JSONResponse(status_code=400, content={
            "rejected": True,
            "reason": "ValidationRequest failed schema validation on receipt",
            "module3_validation": {"passed": False,
                                   "message": "Module 3 rejected the request against validation-request.schema.json",
                                   "errors": errs},
        })

    run_id = request["run_id"]
    outcome = _choose_outcome(forced_outcome)
    run = Run(run_id, outcome)
    RUNS[run_id] = run
    run.task = asyncio.create_task(_emit(run))

    return JSONResponse({
        "run_id": run_id,
        "accepted": True,
        "outcome_selected": outcome,
        "forced": bool(forced_outcome and forced_outcome in _POOL),
        "module3_validation": {
            "passed": True,
            "message": "Module 3 validated the request against validation-request.schema.json on receipt",
        },
    })


@app.get("/runs/{run_id}/events")
async def events(run_id: str, since: int = 0):
    """events — return events in order after ?since=N. Already validated before sending."""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run_id")
    async with run.lock:
        evs = [e for e in run.emitted if e["sequence"] > since]
        terminal = run.terminal
    return JSONResponse({
        "events": evs,
        "terminal": terminal,
        "module3_validation": {
            "passed": True,
            "message": "Module 3 validated each event against execution-event.schema.json before sending",
            "count": len(evs),
        },
    })


@app.get("/runs/{run_id}/result")
async def result(run_id: str):
    """result — nothing until the run reaches a terminal state."""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run_id")
    async with run.lock:
        if not run.terminal or run.result is None:
            return JSONResponse({"result": None, "module3_validation": None})
        return JSONResponse({
            "result": run.result,
            "module3_validation": {
                "passed": True,
                "message": "Module 3 validated the result against validation-result.schema.json before sending",
            },
        })


@app.post("/runs/{run_id}/cancel")
async def cancel(run_id: str):
    """cancel — really interrupt an in-flight run: stop the sequence, emit `cancelled`
    as the terminal event, and make a status:cancelled result available."""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run_id")
    async with run.lock:
        if run.terminal:
            return JSONResponse({"run_id": run_id, "cancelled": False,
                                 "already_terminal": True,
                                 "message": "Run had already reached a terminal state."})
        # Interrupt the emitter and finalize as cancelled.
        if run.task is not None:
            run.task.cancel()
        seq = len(run.emitted) + 1
        ev = {
            "run_id": run_id, "sequence": seq, "timestamp": _now(),
            "event_type": "cancelled", "message": "Run cancelled by request",
        }
        _validate_or_die(_EVENT_VALIDATOR, ev, "event")
        run.emitted.append(ev)

        elapsed = (datetime.now(timezone.utc) - run.started).total_seconds()
        cancelled_result = {
            "run_id": run_id,
            "status": "cancelled",
            "summary": "Run cancelled by request before it completed.",
            "check_results": [],
            "diagnosis": None,
            "artifacts": ["partial-transcript.log"],
            "duration_seconds": round(elapsed, 1),
        }
        _validate_or_die(_RESULT_VALIDATOR, cancelled_result, "result")
        run.result = cancelled_result
        run.terminal = True

    return JSONResponse({"run_id": run_id, "cancelled": True,
                         "terminal_sequence": seq,
                         "message": "Run interrupted; cancelled event emitted and cancelled result available."})

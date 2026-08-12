"""Run orchestration — assemble, send, drive one validation run, and record a
clear terminal state whether the run succeeds or the Gateway misbehaves.

Live progress: Gateway → server-side background poller → validate → persist → SSE
→ browser. The poller runs independently of any browser; the SSE stream replays
persisted events then tails new ones (basic replay-on-connect; formal NFR-3 is
Step 3).

The `run` row is Module 1's LOCAL validation-attempt record. It may exist even
when Module 3 never created a run — `gateway_ack` (set only on a successful start)
is the authoritative indication that a Gateway run actually exists. When no valid
ValidationResult is obtained, the run reaches a terminal error state with a
Module-1-authored `error_kind`; a ValidationResult is never fabricated
(see REQUIREMENTS_CLARIFICATIONS.md, EXE-8).

Nothing here imports Module 3 or the mock; the only channel is HTTP via
workbench.gateway_client.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from workbench import config, contract, db, gateway_client, verdict
from workbench.config import mod3_base_url
from workbench.models import Task
from workbench.packages import assemble
from workbench.tasks import load_tasks

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
POLL_INTERVAL = 0.5
SSE_TICK = 0.5
MAX_RUN_SECONDS = 120        # resource backstop; real timeout/stall handling is Step 3.
RETRY_ATTEMPTS = 3           # total attempts on a transient 5xx (Step 2B, clarification 8)
RETRY_DELAY = 1.0            # fixed delay between attempts; 4xx is never retried.

_pollers: dict[str, asyncio.Task] = {}


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def _is_malformed(body: Any) -> bool:
    return isinstance(body, dict) and body.get("_malformed") is True


def _body_text(body: Any) -> str | None:
    if body is None:
        return None
    if _is_malformed(body):
        return body.get("_raw")
    return json.dumps(body, indent=2)


async def _gw_call(fn: Callable, *args) -> tuple[int, Any]:
    """Call a Gateway operation, retrying only on a transient 5xx (fixed delay,
    3 attempts total). 4xx is returned as-is; transport failures raise GatewayError."""
    status, body = 0, None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        status, body = await asyncio.to_thread(fn, *args)
        if status >= 500 and attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY)
            continue
        return status, body
    return status, body


def build_request(dir_name: str, task: Task, capabilities: list[str],
                  environment: str, run_id: str) -> dict:
    """Assemble the immutable ValidationRequest. The task is emitted with contract
    fields only — the operator-facing `active` flag is excluded (the request schema
    is additionalProperties:false)."""
    package = assemble(config.PACKAGES_DIR / dir_name, dir_name).package
    task_obj: dict = {"id": task.id, "title": task.title, "description": task.description,
                      "fingerprint": task.fingerprint}
    if task.business_area is not None:
        task_obj["business_area"] = task.business_area
    if task.difficulty is not None:
        task_obj["difficulty"] = task.difficulty
    if task.acceptance_criteria is not None:
        task_obj["acceptance_criteria"] = task.acceptance_criteria
    task_obj["checks"] = task.checks
    if task.metadata:
        task_obj["metadata"] = task.metadata
    return {
        "contract_version": config.CONTRACT_VERSION,
        "run_id": run_id,
        "package": package.model_dump(),
        "task": task_obj,
        "execution_context": {"target_environment": environment,
                              "timeout_seconds": config.DEFAULT_TIMEOUT_SECONDS,
                              "additional_instructions": None},
        "permitted_capabilities": sorted(capabilities),
    }


async def start_run(dir_name: str, task_id: str, capabilities: list[str],
                    environment: str, forced_outcome: str | None,
                    fault: str | None = None) -> str:
    assembly = assemble(config.PACKAGES_DIR / dir_name, dir_name)
    task = next((t for t in load_tasks(config.PACKAGES_DIR / dir_name) if t.id == task_id), None)
    if task is None:
        raise ValueError(f"Unknown task '{task_id}' in package '{dir_name}'")

    run_id = _new_run_id()
    request = build_request(dir_name, task, capabilities, environment, run_id)
    request_validation = contract.validate_request(request)
    db.create_run({
        "run_id": run_id, "package_name": assembly.package.name,
        "package_fingerprint": assembly.package.fingerprint, "task_id": task.id,
        "task_fingerprint": task.fingerprint, "capabilities": sorted(capabilities),
        "target_environment": environment, "request": request,
        "request_validation": request_validation, "run_state": "submitting",
    })

    if not request_validation["passed"]:
        db.set_run_error(run_id, "request_invalid",
                         "Outbound ValidationRequest failed Module 1 schema validation; not sent.",
                         payload_text=json.dumps(request_validation["errors"], indent=2))
        return run_id

    # forced_outcome and fault are development-only and out of band. Gate on the
    # explicit dev/mock flag (defence in depth) and never place them in the body.
    dev = config.dev_mock_mode()
    forced = forced_outcome if (dev and forced_outcome) else None
    fault = fault if (dev and fault) else None

    try:
        status, ack = await _gw_call(gateway_client.start, request, forced, fault)
    except gateway_client.GatewayError as e:
        db.set_run_error(run_id, "gateway_unreachable",
                         f"Module 3 could not be reached at {mod3_base_url()} when starting the run. "
                         f"No run was created on the Gateway.", payload_text=str(e))
        return run_id

    if status == 200 and isinstance(ack, dict) and not ack.get("_malformed") and ack.get("run_id"):
        db.set_run_running(run_id, ack)
        _pollers[run_id] = asyncio.create_task(_poll(run_id))
    elif status == 400:
        reason = ack.get("reason") if isinstance(ack, dict) else None
        db.set_run_error(run_id, "start_rejected",
                         f"Module 3 rejected the request. {reason or ''}".strip()
                         + " No run was created on the Gateway.", payload_text=_body_text(ack))
    elif _is_malformed(ack):
        db.set_run_error(run_id, "protocol_error",
                         "Module 3 sent a non-JSON response to start.", payload_text=ack.get("_raw"))
    else:
        db.set_run_error(run_id, "gateway_http_error",
                         f"Module 3 returned HTTP {status} at start.", payload_text=_body_text(ack))
    return run_id


async def _fetch_result(run_id: str):
    """Fetch/validate the result after the terminal event. Returns
    ('ok', result, rdata) or ('error',) after recording the error itself."""
    result, rdata = None, None
    for _ in range(7):
        try:
            status, rdata = await _gw_call(gateway_client.get_result, run_id)
        except gateway_client.GatewayError as e:
            db.set_run_error(run_id, "gateway_unreachable",
                             "Lost the connection to Module 3 while fetching the result.", payload_text=str(e))
            return ("error",)
        if status >= 400:
            db.set_run_error(run_id, "gateway_http_error",
                             f"Module 3 returned HTTP {status} at result.", payload_text=_body_text(rdata))
            return ("error",)
        if _is_malformed(rdata):
            db.set_run_error(run_id, "protocol_error",
                             "Module 3 sent a non-JSON response at result.", payload_text=rdata.get("_raw"))
            return ("error",)
        result = (rdata or {}).get("result") if isinstance(rdata, dict) else None
        if result is not None:
            return ("ok", result, rdata)
        await asyncio.sleep(0.3)
    db.set_run_error(run_id, "protocol_error",
                     "Terminal event received but Module 3 returned no result.", payload_text=None)
    return ("error",)


async def _poll(run_id: str) -> None:
    started = datetime.now(timezone.utc)
    max_seq = db.last_sequence(run_id)
    try:
        while (datetime.now(timezone.utc) - started).total_seconds() < MAX_RUN_SECONDS:
            try:
                status, data = await _gw_call(gateway_client.get_events, run_id, max_seq)
            except gateway_client.GatewayError as e:
                db.set_run_error(run_id, "gateway_unreachable",
                                 "Lost the connection to Module 3 during the run.", payload_text=str(e))
                return
            if status >= 400:
                db.set_run_error(run_id, "gateway_http_error",
                                 f"Module 3 returned HTTP {status} at events.", payload_text=_body_text(data))
                return
            if _is_malformed(data):
                db.set_run_error(run_id, "protocol_error",
                                 "Module 3 sent a non-JSON response at events.", payload_text=data.get("_raw"))
                return

            events = (data or {}).get("events", []) if isinstance(data, dict) else []
            terminal = False
            for ev in events:
                # Detect a sequence anomaly BEFORE persisting, so a DB constraint
                # can never hide the real protocol failure (clarification 9).
                expected = max_seq + 1
                seq = ev.get("sequence")
                if seq != expected:
                    where = "gap" if isinstance(seq, int) and seq > expected else "duplicate or out of order"
                    db.set_run_error(run_id, "protocol_error",
                                     f"Event sequence broke: expected #{expected}, received #{seq} ({where}).",
                                     payload_text=json.dumps(ev, indent=2))
                    return
                m1v = contract.validate_event(ev)
                if not m1v["passed"]:
                    db.set_run_error(run_id, "protocol_error",
                                     f"Module 1 rejected ExecutionEvent #{seq}: it failed schema validation "
                                     f"({len(m1v['errors'])} error(s)): {'; '.join(m1v['errors'][:3])}",
                                     payload_text=json.dumps(ev, indent=2))
                    return
                db.append_event(run_id, ev, m1v)
                max_seq = seq
                if ev.get("event_type") in TERMINAL_EVENTS:
                    terminal = True
                    break

            if terminal:
                outcome = await _fetch_result(run_id)
                if outcome[0] != "ok":
                    return
                _, result, rdata = outcome
                m1v = contract.validate_result(result)
                if not m1v["passed"]:
                    db.set_run_error(run_id, "protocol_error",
                                     f"Module 1 rejected the ValidationResult: it failed schema validation "
                                     f"({len(m1v['errors'])} error(s)): {'; '.join(m1v['errors'][:3])}",
                                     payload_text=json.dumps(result, indent=2))
                    return
                request = json.loads(db.get_run(run_id)["request_json"])
                vd = verdict.derive_verdict(request, result)
                db.finalize_run(run_id, result, m1v, vd, gateway_result=rdata)
                return

            await asyncio.sleep(POLL_INTERVAL)

        db.set_run_error(run_id, "protocol_error",
                         f"Safety stop: no terminal event within {MAX_RUN_SECONDS}s "
                         f"(proper timeout/stall handling is Step 3).", payload_text=None)
    except Exception as e:  # never let a poller crash silently corrupt the run
        db.set_run_error(run_id, "protocol_error", f"Unexpected error during run: {e}", payload_text=None)
    finally:
        _pollers.pop(run_id, None)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def stream(run_id: str):
    last_sent = 0
    started = datetime.now(timezone.utc)
    yield _sse("open", {"run_id": run_id})
    while (datetime.now(timezone.utc) - started).total_seconds() < MAX_RUN_SECONDS + 30:
        for item in db.get_events(run_id, last_sent):
            last_sent = item["event"]["sequence"]
            yield _sse("event", item)
        run = db.get_run(run_id)
        if run is None:
            yield _sse("stream_error", {"error": "unknown run"})
            return
        if run["run_state"] == "terminal":
            yield _sse("result", {
                "result": json.loads(run["result_json"]) if run["result_json"] else None,
                "result_validation": json.loads(run["result_validation_json"]) if run["result_validation_json"] else None,
                "verdict": json.loads(run["verdict_json"]) if run["verdict_json"] else None,
                "gateway_result": json.loads(run["gateway_result_json"]) if run["gateway_result_json"] else None,
            })
            yield _sse("done", {})
            return
        if run["run_state"] == "error":
            yield _sse("run_error", {
                "error_kind": run["error_kind"], "detail": run["error"],
                "payload_text": run["error_payload_text"],
                "gateway_run_created": run["gateway_ack_json"] is not None,
            })
            yield _sse("done", {})
            return
        await asyncio.sleep(SSE_TICK)
    yield _sse("done", {})


def run_view(run_id: str) -> dict | None:
    run = db.get_run(run_id)
    if not run:
        return None
    view = dict(run)
    for col in ("capabilities", "request", "request_validation", "gateway_ack",
                "gateway_result", "verdict", "result", "result_validation"):
        raw = run.get(f"{col}_json")
        view[col] = json.loads(raw) if raw else None
    view["gateway_run_created"] = run.get("gateway_ack_json") is not None
    view["events"] = db.get_events(run_id, 0)
    return view

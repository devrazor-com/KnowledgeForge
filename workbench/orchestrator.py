"""Run orchestration — assemble, send, and drive one validation run.

Live progress is: Gateway → server-side background poller → validate → persist →
SSE → browser. The poller runs as an asyncio task independent of any browser, so
refreshing or closing the Run screen never affects the run. The SSE stream simply
replays persisted events from the database and then tails new ones — which is why
a mid-run refresh recovers everything already stored (basic replay-on-connect).

Formal NFR-3 hardening (Last-Event-ID, guaranteed no-loss across a dropped socket,
restoring a poller after a server restart) is Step 3, not here.

Nothing in this module imports Module 3 or the mock; the only channel is HTTP via
workbench.gateway_client.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from workbench import config, contract, db, gateway_client, verdict
from workbench.models import Task
from workbench.packages import assemble
from workbench.tasks import load_tasks

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
POLL_INTERVAL = 0.5          # seconds between Gateway event polls / SSE ticks
SSE_TICK = 0.5
MAX_RUN_SECONDS = 120        # crude safety stop so a poller can't run forever;
                             # NOT the Step 3 timeout/stall feature.

_pollers: dict[str, asyncio.Task] = {}


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def build_request(dir_name: str, task: Task, capabilities: list[str],
                  environment: str, run_id: str) -> dict:
    """Assemble the immutable ValidationRequest that will cross the boundary.

    The task is emitted with contract fields only — the operator-facing `active`
    flag is Module 1 state and is deliberately excluded (the request schema is
    additionalProperties:false)."""
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
        "execution_context": {
            "target_environment": environment,
            "timeout_seconds": config.DEFAULT_TIMEOUT_SECONDS,
            "additional_instructions": None,
        },
        "permitted_capabilities": sorted(capabilities),
    }


async def start_run(dir_name: str, task_id: str, capabilities: list[str],
                    environment: str, forced_outcome: str | None) -> str:
    """Assemble, validate outbound, send to the Gateway, and launch the poller.
    Returns the run_id immediately; the work happens in the background."""
    assembly = assemble(config.PACKAGES_DIR / dir_name, dir_name)
    task = next((t for t in load_tasks(config.PACKAGES_DIR / dir_name) if t.id == task_id), None)
    if task is None:
        raise ValueError(f"Unknown task '{task_id}' in package '{dir_name}'")

    run_id = _new_run_id()
    request = build_request(dir_name, task, capabilities, environment, run_id)
    request_validation = contract.validate_request(request)

    db.create_run({
        "run_id": run_id,
        "package_name": assembly.package.name,
        "package_fingerprint": assembly.package.fingerprint,
        "task_id": task.id,
        "task_fingerprint": task.fingerprint,
        "capabilities": sorted(capabilities),
        "target_environment": environment,
        "request": request,
        "request_validation": request_validation,
        "run_state": "submitting",
    })

    # Module 1 refuses to send a request that fails its own outbound validation.
    if not request_validation["passed"]:
        db.set_run_error(run_id, "Outbound ValidationRequest failed Module 1 schema validation; not sent.")
        return run_id

    # forced_outcome is development-only and out of band. Gate it on the explicit
    # dev/mock flag (defence in depth — the UI already hides it when off) and never
    # place it in the request body.
    forced = forced_outcome if (config.dev_mock_mode() and forced_outcome) else None

    try:
        status, ack = await asyncio.to_thread(gateway_client.start, request, forced)
    except gateway_client.GatewayError as e:
        db.set_run_error(run_id, f"Gateway unreachable at start: {e}")
        return run_id

    if status == 200 and isinstance(ack, dict) and ack.get("run_id"):
        db.set_run_running(run_id, ack)
        _pollers[run_id] = asyncio.create_task(_poll(run_id))
    else:
        db.set_run_error(run_id, f"Gateway did not accept the run (HTTP {status}).")
    return run_id


async def _poll(run_id: str) -> None:
    """Background poller: pull events after the last persisted sequence, validate
    each inbound event, persist it, and on the terminal event fetch/validate the
    result and derive the verdict."""
    started = datetime.now(timezone.utc)
    try:
        while (datetime.now(timezone.utc) - started).total_seconds() < MAX_RUN_SECONDS:
            since = db.last_sequence(run_id)
            status, data = await asyncio.to_thread(gateway_client.get_events, run_id, since)
            events = (data or {}).get("events", []) if isinstance(data, dict) else []
            terminal = False
            for ev in events:
                m1v = contract.validate_event(ev)
                db.append_event(run_id, ev, m1v)
                if ev.get("event_type") in TERMINAL_EVENTS:
                    terminal = True

            if terminal:
                result, rdata = None, None
                for _ in range(6):  # result becomes available at/just after the terminal event
                    _, rdata = await asyncio.to_thread(gateway_client.get_result, run_id)
                    result = (rdata or {}).get("result") if isinstance(rdata, dict) else None
                    if result:
                        break
                    await asyncio.sleep(0.3)
                if result:
                    m1v = contract.validate_result(result)
                    request = json.loads(db.get_run(run_id)["request_json"])
                    vd = verdict.derive_verdict(request, result)
                    db.finalize_run(run_id, result, m1v, vd, gateway_result=rdata)
                else:
                    db.set_run_error(run_id, "Terminal event received but no result was returned.")
                return

            await asyncio.sleep(POLL_INTERVAL)

        db.set_run_error(run_id, f"Safety stop: run exceeded {MAX_RUN_SECONDS}s without terminating.")
    except gateway_client.GatewayError as e:
        db.set_run_error(run_id, f"Gateway error during run: {e}")
    except Exception as e:  # never let a poller crash silently corrupt the run
        db.set_run_error(run_id, f"Unexpected error during run: {e}")
    finally:
        _pollers.pop(run_id, None)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def stream(run_id: str):
    """SSE generator: replay persisted events from the start, then tail new ones
    until the run is terminal (or errors)."""
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
            yield _sse("run_error", {"error": run["error"]})
            yield _sse("done", {})
            return
        await asyncio.sleep(SSE_TICK)
    yield _sse("done", {})


def run_view(run_id: str) -> dict | None:
    """Everything persisted for a run, JSON columns parsed — for the Run screen
    and the JSON status endpoint."""
    run = db.get_run(run_id)
    if not run:
        return None
    view = dict(run)
    for col in ("capabilities", "request", "request_validation", "gateway_ack",
                "gateway_result", "verdict", "result", "result_validation"):
        raw = run.get(f"{col}_json")
        view[col] = json.loads(raw) if raw else None
    view["events"] = db.get_events(run_id, 0)
    return view

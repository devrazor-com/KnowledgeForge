"""Run orchestration — assemble, send, drive one validation run, and record a
clear terminal state whether the run succeeds, the Gateway misbehaves, the run is
cancelled, or Module 1's deadline expires.

Live progress: Gateway → server-side background poller → validate → persist → SSE
→ browser. The poller runs independently of any browser and is the SOLE normal
writer of a Gateway terminal result.

Termination policy (Step 3A):
  * `execution_context.timeout_seconds` is the Gateway's execution budget after
    acceptance. Module 1's run deadline = accepted_at + that value (read from the
    SENT request) + a small guard margin. It is authoritative: individual Gateway
    calls and 5xx retries are bounded by the remaining time to the deadline, so a
    single socket call or retry loop can never silently overrun it.
  * The per-call socket timeout (GATEWAY_HTTP_TIMEOUT) bounds ONE network call and
    is never the run budget.
  * On deadline breach the run becomes terminal `timed_out` (effective
    inconclusive) — WRITE-ONCE, so a late Gateway `cancelled` from the fire-and-
    forget cleanup cancel can never rewrite it. A user cancellation honoured
    BEFORE the deadline is a normal rule #1 `cancelled`.
  * There is NO silence-based stall heuristic: the frozen contract guarantees no
    progress cadence, so inferring a stall from quiet would depend on an unstated
    property of Module 3 (see REQUIREMENTS_CLARIFICATIONS.md open item).

A ValidationResult is never fabricated (EXE-8). Nothing here imports Module 3 or
the mock; the only channel is HTTP via workbench.gateway_client.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from workbench import config, contract, db, gateway_client, verdict
from workbench.config import mod3_base_url
from workbench.models import Task
from workbench.packages import assemble
from workbench.tasks import load_tasks

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
POLL_INTERVAL = 0.5
SSE_TICK = 0.5
RETRY_ATTEMPTS = 3           # total attempts on a transient 5xx
RETRY_DELAY = 1.0            # fixed delay between attempts; 4xx is never retried.

_pollers: dict[str, asyncio.Task] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    return f"run-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _is_malformed(body: Any) -> bool:
    return isinstance(body, dict) and body.get("_malformed") is True


def _body_text(body: Any) -> str | None:
    if body is None:
        return None
    if _is_malformed(body):
        return body.get("_raw")
    return json.dumps(body, indent=2)


# --------------------------------------------------------------------------
# Deadline helpers — the run deadline is authoritative
# --------------------------------------------------------------------------

def _deadline(run: dict | None) -> datetime | None:
    """accepted_at + execution_context.timeout_seconds (from the sent request) +
    guard. None before Gateway acceptance (no run exists on the Gateway yet)."""
    if not run or not run.get("accepted_at") or not run.get("request_json"):
        return None
    ts = (json.loads(run["request_json"]).get("execution_context") or {}).get("timeout_seconds")
    if ts is None:
        return None
    return datetime.fromisoformat(run["accepted_at"]) + timedelta(
        seconds=int(ts) + config.timeout_guard_seconds())


def _remaining(deadline: datetime | None) -> float:
    """Seconds left before the deadline. With no deadline yet (pre-acceptance),
    a single call is bounded only by the per-call socket timeout."""
    if deadline is None:
        return config.gateway_http_timeout()
    return (deadline - _now()).total_seconds()


async def _gw_call(fn: Callable, deadline: datetime | None, *args) -> tuple[int, Any]:
    """Call a Gateway operation bounded by the run deadline. Each call's timeout is
    min(GATEWAY_HTTP_TIMEOUT, remaining); a transient 5xx is retried only if a retry
    would still fit before the deadline. Raises GatewayError on transport failure
    or when no budget remains."""
    status, body = 0, None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise gateway_client.GatewayError("run deadline reached before the call")
        call_timeout = min(config.gateway_http_timeout(), remaining)
        status, body = await asyncio.to_thread(fn, *args, timeout=call_timeout)
        if status >= 500 and attempt < RETRY_ATTEMPTS:
            if _remaining(deadline) <= RETRY_DELAY:   # no budget to retry within the deadline
                return status, body
            await asyncio.sleep(RETRY_DELAY)
            continue
        return status, body
    return status, body


# --------------------------------------------------------------------------
# Request assembly and run start
# --------------------------------------------------------------------------

def build_request(dir_name: str, task: Task, capabilities: list[str],
                  environment: str, run_id: str) -> dict:
    """Assemble the immutable ValidationRequest. The task is emitted with contract
    fields only — the operator-facing `active` flag is excluded (the request schema
    is additionalProperties:false). timeout_seconds is seeded from config; once the
    request exists, THAT value is authoritative for the deadline."""
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
                              "timeout_seconds": config.run_timeout_seconds(),
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

    dev = config.dev_mock_mode()
    forced = forced_outcome if (dev and forced_outcome) else None
    fault = fault if (dev and fault) else None

    try:
        # No deadline yet — before acceptance the start call is bounded only by the
        # per-call socket timeout.
        status, ack = await _gw_call(gateway_client.start, None, request, forced, fault)
    except gateway_client.GatewayError as e:
        db.set_run_error(run_id, "gateway_unreachable",
                         f"Module 3 could not be reached at {mod3_base_url()} when starting the run. "
                         f"No run was created on the Gateway.", payload_text=str(e))
        return run_id

    if status == 200 and isinstance(ack, dict) and not ack.get("_malformed") and ack.get("run_id"):
        db.set_run_running(run_id, ack)   # anchors the deadline (accepted_at)
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


# --------------------------------------------------------------------------
# Timeout + cancellation
# --------------------------------------------------------------------------

def _record_timeout(run_id: str) -> None:
    """Record Module 1's deadline breach (WRITE-ONCE) and, only if we actually
    wrote it, fire a best-effort cleanup cancel with its own small budget."""
    run = db.get_run(run_id)
    ts = None
    if run and run.get("request_json"):
        ts = (json.loads(run["request_json"]).get("execution_context") or {}).get("timeout_seconds")
    guard = config.timeout_guard_seconds()
    wrote = db.set_run_error(
        run_id, "timed_out",
        f"Module 1's run deadline expired: the Gateway did not report completion within its "
        f"execution budget ({ts}s) plus Module 1's guard margin ({guard}s) after acceptance. "
        f"Recorded by Module 1 — no ValidationResult was received.", payload_text=None)
    if wrote:
        _schedule_cleanup_cancel(run_id)


def _schedule_cleanup_cancel(run_id: str) -> None:
    """Fire-and-forget cleanup cancel AFTER a timeout is recorded. Own fixed budget
    (GATEWAY_CANCEL_CLEANUP_TIMEOUT); never delays or changes the timed_out
    disposition, and its outcome is ignored (write-once protects the record)."""
    async def _cleanup() -> None:
        try:
            await asyncio.to_thread(gateway_client.cancel, run_id, config.GATEWAY_CANCEL_CLEANUP_TIMEOUT)
        except Exception:
            pass
    asyncio.create_task(_cleanup())


async def request_cancel(run_id: str) -> dict:
    """Operator-initiated cancellation. Marks the run and asks the Gateway to
    cancel; the poller records the terminal `cancelled` when the Gateway reports it
    (this function never writes the terminal state)."""
    run = db.get_run(run_id)
    if run is None:
        return {"ok": False, "unknown": True, "message": "No such run."}
    if run["run_state"] in ("terminal", "error"):
        return {"ok": False, "already_terminal": True,
                "message": "This run had already finished; nothing to cancel."}
    db.set_cancel_requested(run_id)
    try:
        await _gw_call(gateway_client.cancel, _deadline(run), run_id)
        return {"ok": True, "message": "Cancellation requested. The run will end when the Gateway reports it."}
    except gateway_client.GatewayError:
        return {"ok": True, "unreachable": True,
                "message": "Could not reach the Gateway to cancel; the run will end on timeout "
                           "or when the Gateway responds."}


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------

async def _fetch_result(run_id: str, deadline: datetime | None):
    """Fetch/validate the result after the terminal event. Returns
    ('ok', result, rdata) or ('error',) after recording the error itself."""
    for _ in range(7):
        try:
            status, rdata = await _gw_call(gateway_client.get_result, deadline, run_id)
        except gateway_client.GatewayError as e:
            if deadline is not None and _now() >= deadline:
                _record_timeout(run_id)
            else:
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
    max_seq = db.last_sequence(run_id)
    deadline = _deadline(db.get_run(run_id))
    try:
        while True:
            if deadline is not None and _now() >= deadline:
                _record_timeout(run_id)          # deadline wins; do not process further events
                return
            try:
                status, data = await _gw_call(gateway_client.get_events, deadline, run_id, max_seq)
            except gateway_client.GatewayError as e:
                if deadline is not None and _now() >= deadline:
                    _record_timeout(run_id)
                else:
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
                expected = max_seq + 1
                seq = ev.get("sequence")
                if seq != expected:   # sequence anomaly detected BEFORE persisting
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
                outcome = await _fetch_result(run_id, deadline)
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
                db.finalize_run(run_id, result, m1v, vd, gateway_result=rdata)   # write-once
                return

            nap = POLL_INTERVAL if deadline is None else max(0.05, min(POLL_INTERVAL, _remaining(deadline)))
            await asyncio.sleep(nap)
    except Exception as e:   # never let a poller crash silently corrupt the run
        db.set_run_error(run_id, "protocol_error", f"Unexpected error during run: {e}", payload_text=None)
    finally:
        _pollers.pop(run_id, None)


# --------------------------------------------------------------------------
# SSE + views
# --------------------------------------------------------------------------

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def stream(run_id: str):
    last_sent = 0
    run0 = db.get_run(run_id)
    ts = config.run_timeout_seconds()
    if run0 and run0.get("request_json"):
        ts = (json.loads(run0["request_json"]).get("execution_context") or {}).get("timeout_seconds") or ts
    cap = int(ts) + config.timeout_guard_seconds() + 60   # outlive the deadline; single source
    started = _now()
    yield _sse("open", {"run_id": run_id})
    while (_now() - started).total_seconds() < cap:
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

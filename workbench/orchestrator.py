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
from pathlib import Path
from typing import Any, Callable

from workbench import config, contract, db, gateway_client, status, verdict
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

# Ephemeral, in-memory recovery status — process/runtime state (a recovery poller
# is alive and e.g. can't reach the Gateway), NOT durable evidence. Exposed to the
# Run UI via SSE while recovery is happening; it vanishes if the Workbench dies and
# is reconstructed by the next reconciliation. Deliberately not persisted.
_recovery_status: dict[str, dict] = {}   # run_id -> {"kind": str, "message": str}

# The durable explanation for a pre-acceptance ambiguous start (see §4 of the scope).
START_UNRESOLVED_DETAIL = (
    "Module 1 created the local validation attempt but cannot determine whether Module 3 "
    "accepted the start request before the interruption. Retrying could create duplicate "
    "execution, so the attempt was not restarted.")

# Operator-facing wording for each persisted cancellation-DELIVERY state. This is
# durable knowledge about the OPERATOR's cancel request (never the post-timeout
# cleanup cancel). It is faithful to exactly what Module 1 knows and never claims
# the run is already cancelled — only the contract `cancelled` event/result does.
CANCEL_DELIVERY_MESSAGES = {
    "unknown": (
        "Cancellation was requested, but Module 1 cannot determine whether Module 3 received or "
        "acted on it. Module 1 will not retry automatically. You can Cancel again while this run "
        "remains non-terminal."),
    "undelivered": (
        "Cancellation was requested, but Module 1 could not reach the Gateway to deliver it — the "
        "request did not arrive. Module 1 will not retry automatically. You can try Cancel again "
        "while this run remains non-terminal."),
    "rejected": (
        "Module 3 received the cancellation request and rejected it (HTTP 4xx); the run was not "
        "cancelled. You can try Cancel again while this run remains non-terminal."),
    "acknowledged": (
        "Module 3 acknowledged the cancellation request. Waiting for the Gateway to report the "
        "cancelled result — the run is not cancelled until it does."),
}

# One-line description of a SINGLE cancel attempt's outcome. Used only in the
# immediate response when the latest attempt differs from the durable state (the
# sticky-'acknowledged' case) so the operator sees what just happened without the
# durable knowledge being downgraded.
CANCEL_ATTEMPT_LINES = {
    "undelivered": "Your latest Cancel attempt could not reach the Gateway.",
    "rejected": "Your latest Cancel attempt was rejected by the Gateway (HTTP 4xx).",
    "unknown": "Your latest Cancel attempt's delivery outcome is uncertain.",
    "acknowledged": "The Gateway acknowledged your latest Cancel attempt.",
}


def cancel_note(run: dict) -> dict | None:
    """Durable operator-cancellation note for the UI, derived from persisted fields.
    None when the operator never requested cancellation. A requested run with no
    recorded delivery yet is treated as 'unknown' (the most conservative reading)."""
    if not run.get("cancel_requested"):
        return None
    state = run.get("cancel_delivery") or "unknown"
    return {"delivery": state,
            "message": CANCEL_DELIVERY_MESSAGES.get(state, CANCEL_DELIVERY_MESSAGES["unknown"])}


def get_recovery_status(run_id: str) -> dict | None:
    return _recovery_status.get(run_id)


def _set_recovery_status(run_id: str, kind: str, message: str) -> None:
    _recovery_status[run_id] = {"kind": kind, "message": message}


def _clear_recovery_status(run_id: str) -> None:
    _recovery_status.pop(run_id, None)


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


async def _gw_call(fn: Callable, deadline: datetime | None, *args, retry_5xx: bool = True) -> tuple[int, Any]:
    """Call a Gateway operation bounded by the run deadline. Each call's timeout is
    min(GATEWAY_HTTP_TIMEOUT, remaining). A transient 5xx is retried only if a retry
    would still fit before the deadline AND `retry_5xx` is set. Raises GatewayError on
    transport failure or when no budget remains.

    `retry_5xx` defaults to True (start/events/result rely on retrying a transient 5xx).
    `cancel` passes retry_5xx=False: a repeated cancel is contractually undefined, and a
    5xx does not establish whether the first cancel was acted upon, so Module 1 sends
    exactly ONE physical cancel and records the uncertainty as 'unknown' rather than
    silently re-issuing an undefined repeat call."""
    status, body = 0, None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise gateway_client.GatewayError("run deadline reached before the call")
        call_timeout = min(config.gateway_http_timeout(), remaining)
        status, body = await asyncio.to_thread(fn, *args, timeout=call_timeout)
        if status >= 500 and retry_5xx and attempt < RETRY_ATTEMPTS:
            if _remaining(deadline) <= RETRY_DELAY:   # no budget to retry within the deadline
                return status, body
            await asyncio.sleep(RETRY_DELAY)
            continue
        return status, body
    return status, body


# --------------------------------------------------------------------------
# Request assembly and run start
# --------------------------------------------------------------------------

def build_request(root: Path, key: str, task: Task, capabilities: list[str],
                  environment: str, run_id: str) -> dict:
    """Assemble the immutable ValidationRequest. The task is emitted with contract
    fields only — the operator-facing `active` flag is excluded (the request schema
    is additionalProperties:false). timeout_seconds is seeded from config; once the
    request exists, THAT value is authoritative for the deadline. The manifest is
    structural config and is NOT part of package.model_dump()'s files."""
    package = assemble(root, key).package
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


async def start_run(root: Path, key: str, registered_package_id: str | None, task_id: str,
                    capabilities: list[str], environment: str, forced_outcome: str | None,
                    fault: str | None = None) -> str:
    assembly = assemble(root, key)   # reads the LIVE manifest (incl. its package_id)
    # Identity integrity: a run is filed under the REGISTERED identity, and only after
    # confirming the live manifest still agrees. A package_id change is an identity
    # change, not an ordinary edit — never adopt it silently, never file evidence under
    # an id the catalog registration does not know about.
    if not registered_package_id:
        raise ValueError("This package has no registered identity (package_id); "
                         "re-register it before starting a run.")
    if assembly.package_id != registered_package_id:
        raise ValueError(
            f"Package identity mismatch: this source is registered as "
            f"'{registered_package_id}' but its manifest now declares "
            f"'{assembly.package_id}'. No run was started. Changing package_id is an "
            f"identity change — unregister and re-register this package deliberately.")
    # No profile, no run: every validation run executes against an explicitly configured
    # validation context. Module 1 never manufactures one from global defaults.
    if db.get_validation_profile(registered_package_id) is None:
        raise ValueError(
            "This package has no validation profile configured. Configure its target "
            "environment and permitted capabilities before starting a validation run.")

    # New-run environment gate. The environment to be SENT must be in the CURRENT
    # configured list, read FRESH here (authoritative at start time). This runs BEFORE any
    # local run row (create_run) or physical POST (_gw_call(start)) — so an unconfigured
    # environment yields zero Module 3 start requests and no persisted run. Configuration
    # governs FUTURE runs only: this gate is never consulted by recovery, polling, result
    # retrieval, or cancellation of runs that already exist.
    try:
        allowed_environments = config.environments()
    except config.EnvironmentsConfigError as e:
        raise ValueError(e.message) from None
    if environment not in allowed_environments:
        raise ValueError(
            f"Target environment '{environment}' is not in the current configured list. "
            f"Select a currently configured environment before starting a run.")

    task = next((t for t in load_tasks(root / assembly.tasks_rel) if t.id == task_id), None)
    if task is None:
        raise ValueError(f"Unknown task '{task_id}' in package '{key}'")

    # Guarantee the exact immutable snapshot is persisted at run start (idempotent by
    # fingerprint) — evidence must never depend on someone having opened package detail.
    db.save_snapshot(assembly)

    run_id = _new_run_id()
    request = build_request(root, key, task, capabilities, environment, run_id)
    request_validation = contract.validate_request(request)
    db.create_run({
        "run_id": run_id, "package_name": assembly.package.name,
        "package_fingerprint": assembly.package.fingerprint, "task_id": task.id,
        "task_fingerprint": task.fingerprint, "capabilities": sorted(capabilities),
        "target_environment": environment, "package_id": registered_package_id,
        "request": request, "request_validation": request_validation, "run_state": "submitting",
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
    """Operator-initiated cancellation. Records intent, then attempts the Gateway
    `cancel` and records what Module 1 learns about DELIVERY (never the terminal
    state — the poller records the `cancelled` the Gateway eventually reports).

    Delivery-state model:
      * persist cancel_requested + 'unknown' BEFORE the call, so an interruption
        mid-call is reconciled as 'unknown' (exactly what Module 1 then knows);
      * definite pre-delivery failure (ECONNREFUSED / DNS)      → 'undelivered';
      * 4xx (received and declined)                             → 'rejected';
      * 2xx (received and acknowledged)                         → 'acknowledged';
      * socket timeout / 5xx / other transport                  → remain 'unknown'.
    'acknowledged' is sticky in the store, so a later failed retry cannot downgrade
    the durable fact that an earlier request was acknowledged; the immediate
    response still describes THIS attempt's outcome."""
    run = db.get_run(run_id)
    if run is None:
        return {"ok": False, "unknown": True, "message": "No such run."}
    if run["run_state"] in ("terminal", "error"):
        return {"ok": False, "already_terminal": True,
                "message": "This run had already finished; nothing to cancel."}
    db.set_cancel_requested(run_id)          # operator intent + timestamp
    db.set_cancel_delivery(run_id, "unknown")  # in-flight; sticky rule keeps any prior 'acknowledged'
    _clear_recovery_status(run_id)           # the operator has now acted; drop any recovering notice

    def _resp(attempt_state: str) -> dict:
        # Persist this attempt's evidence (sticky rule applies) and report the
        # durable state afterwards. When the durable state differs from this attempt
        # (sticky 'acknowledged' held against a failed retry), the immediate response
        # also carries a one-line note about the attempt so the operator sees what
        # just happened — without the durable knowledge being downgraded.
        db.set_cancel_delivery(run_id, attempt_state)
        persisted = (db.get_run(run_id) or {}).get("cancel_delivery") or attempt_state
        return {"ok": attempt_state == "acknowledged", "attempt": attempt_state,
                "delivery": persisted, "message": CANCEL_DELIVERY_MESSAGES[persisted],
                "attempt_message": (CANCEL_ATTEMPT_LINES[attempt_state]
                                    if attempt_state != persisted else None)}

    try:
        # Exactly ONE physical cancel: repeated cancel is contractually undefined and a
        # 5xx does not prove the first was ignored, so we do not auto-retry (retry_5xx=False).
        status, _body = await _gw_call(gateway_client.cancel, _deadline(run), run_id, retry_5xx=False)
    except gateway_client.GatewayError as e:
        if e.reason in gateway_client.NON_DELIVERY_REASONS:   # provably never reached M3
            return _resp("undelivered")
        return _resp("unknown")                               # timeout / reset / other → indeterminate
    if 200 <= status < 300:
        return _resp("acknowledged")
    if 400 <= status < 500:
        return _resp("rejected")
    return _resp("unknown")                                   # 5xx (or anything else) → indeterminate


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------

async def _retrieve_result(run_id: str):
    """Shared, bounded, authoritative result-retrieval allowance — used by BOTH the
    normal poller (after a terminal event) and restart recovery. Execution has
    already reached a terminal event; this is a publication allowance, not extra
    execution time, so it is bounded by its OWN window (config.result_retrieval_*),
    not the run deadline. Each call is bounded by min(GATEWAY_HTTP_TIMEOUT, remaining
    window); no new retry begins once the window is exhausted; on exhaustion the run
    is recorded using the 2B taxonomy for whatever actually failed.

    Returns ('ok', result, rdata) or ('error',) after recording the error itself."""
    window = config.result_retrieval_window_seconds()
    interval = config.result_retrieval_interval()
    deadline = _now() + timedelta(seconds=window)
    last: tuple[str, str, str | None] | None = None
    while _now() < deadline:
        remaining = (deadline - _now()).total_seconds()
        call_timeout = max(0.1, min(config.gateway_http_timeout(), remaining))
        try:
            status, rdata = await asyncio.to_thread(gateway_client.get_result, run_id, timeout=call_timeout)
        except gateway_client.GatewayError as e:
            last = ("gateway_unreachable",
                    "Lost the connection to Module 3 while retrieving the result.", str(e))
        else:
            if status >= 400:
                last = ("gateway_http_error",
                        f"Module 3 returned HTTP {status} at result.", _body_text(rdata))
            elif _is_malformed(rdata):
                last = ("protocol_error",
                        "Module 3 sent a non-JSON response at result.", rdata.get("_raw"))
            else:
                result = (rdata or {}).get("result") if isinstance(rdata, dict) else None
                if result is not None:
                    return ("ok", result, rdata)
                last = ("protocol_error",
                        f"Terminal event received, but Module 3 published no result within the "
                        f"{window:.0f}s retrieval allowance.", None)
        await asyncio.sleep(min(interval, max(0.0, (deadline - _now()).total_seconds())))
    kind, detail, payload = last or ("protocol_error", "No result within the retrieval allowance.", None)
    db.set_run_error(run_id, kind, detail, payload_text=payload)
    return ("error",)


async def _poll(run_id: str, recovering: bool = False) -> None:
    """Drive an in-flight run. `recovering=True` (restart recovery) tolerates a
    transient unreachable Gateway by retrying within the ORIGINAL run deadline and
    surfacing an ephemeral 'recovering' status; a fresh run keeps 3A behaviour
    (immediate gateway_unreachable). The run deadline is unchanged either way."""
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
                    return
                if recovering:
                    # Tolerate a transient outage; keep retrying within the run
                    # deadline (no separate recovery timeout) and show progress.
                    _set_recovery_status(run_id, "recovering_unreachable",
                        "Recovering existing Gateway run — Gateway currently unreachable. "
                        "Retrying within the original run deadline.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                db.set_run_error(run_id, "gateway_unreachable",
                                 "Lost the connection to Module 3 during the run.", payload_text=str(e))
                return
            # Reached the Gateway: clear a transient unreachable notice (keep any
            # cancel-ambiguity notice, which is a separate operator-facing state).
            if _recovery_status.get(run_id, {}).get("kind") == "recovering_unreachable":
                _clear_recovery_status(run_id)
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
                await _finish_from_result(run_id)   # bounded, shared result-retrieval
                return

            nap = POLL_INTERVAL if deadline is None else max(0.05, min(POLL_INTERVAL, _remaining(deadline)))
            await asyncio.sleep(nap)
    except Exception as e:   # never let a poller crash silently corrupt the run
        db.set_run_error(run_id, "protocol_error", f"Unexpected error during run: {e}", payload_text=None)
    finally:
        _pollers.pop(run_id, None)
        _clear_recovery_status(run_id)


async def _finish_from_result(run_id: str) -> None:
    """Retrieve (bounded, shared policy), validate, and finalize the result once a
    terminal event has been observed. Write-once terminal semantics apply. Safe to
    call from the normal poller or directly from restart recovery."""
    try:
        outcome = await _retrieve_result(run_id)
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
    finally:
        _pollers.pop(run_id, None)
        _clear_recovery_status(run_id)


async def recover_inflight_runs() -> None:
    """Reconcile non-terminal local attempts at startup (the 3B-1 state machine).
    Launches background tasks; never blocks startup. Contains NO call to `start`:
    accepted runs are only observed/completed by the existing run_id; a run whose
    acceptance is unknown is recorded as start_unresolved, never restarted. Safe to
    run repeatedly — it only acts on non-terminal runs and every write is write-once
    or idempotent, so a Workbench killed mid-recovery reconciles cleanly on restart."""
    for run in db.runs_in_state(("submitting", "running")):
        run_id = run["run_id"]

        # Rule 1 — pre-acceptance ambiguity: never re-call start.
        if run["run_state"] == "submitting" or not run.get("gateway_ack_json") or not run.get("accepted_at"):
            db.set_run_error(run_id, "start_unresolved", START_UNRESOLVED_DETAIL, payload_text=None)
            continue

        deadline = _deadline(run)
        last_is_terminal = db.last_event_type(run_id) in TERMINAL_EVENTS

        # Rule 2 — terminal event persisted, result not retrieved: complete it.
        if last_is_terminal and not run.get("result_json"):
            _pollers[run_id] = asyncio.create_task(_finish_from_result(run_id))
            continue

        # Rule 3 — deadline expired while down: timeout wins (write-once) + cleanup.
        if deadline is not None and _now() >= deadline:
            _record_timeout(run_id)
            continue

        # Rule 4 — operator cancellation was requested before the interruption. The
        # durable cancel_delivery field already records what Module 1 knew (unknown /
        # undelivered / rejected / acknowledged) and the Run screen surfaces it from
        # that field. Recovery resumes OBSERVING only; it NEVER auto-reissues cancel
        # (repeated cancel is contractually undefined) — the operator decides whether
        # to Cancel again. A requested run with no recorded delivery reads as unknown.
        if run.get("cancel_requested"):
            _pollers[run_id] = asyncio.create_task(_poll(run_id, recovering=True))
            continue

        # Rule 5 — accepted, events but no terminal yet: resume observing.
        _pollers[run_id] = asyncio.create_task(_poll(run_id, recovering=True))


# --------------------------------------------------------------------------
# SSE + views
# --------------------------------------------------------------------------

def _sse(event: str, payload: dict, id: int | None = None) -> str:
    """One SSE frame. `id` is emitted ONLY for numbered ExecutionEvents, so it
    becomes the browser's Last-Event-ID / resume cursor. State-snapshot frames
    (open/recovering/cancel/result/run_error/done) carry no id and therefore never
    advance that cursor — a reconnect resumes from the last ExecutionEvent only."""
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(payload)}\n\n"


async def stream(run_id: str, last_event_id: int = 0):
    """SSE for one run. `last_event_id` is the resume cursor from the request's
    Last-Event-ID header: replay begins strictly AFTER it, so a native EventSource
    reconnect receives only ExecutionEvents with sequence > N and no persisted event
    at/below N is deliberately re-sent. A fresh connection (no header) arrives as 0
    and gets the full persisted history from the start. A cursor above the current
    max simply waits for later events. This is NOT an exactly-once delivery
    guarantee — only that the endpoint honours a valid cursor."""
    try:
        last_sent = int(last_event_id or 0)
    except (TypeError, ValueError):
        last_sent = 0   # conservative: malformed cursor → full replay
    run0 = db.get_run(run_id)
    ts = config.run_timeout_seconds()
    if run0 and run0.get("request_json"):
        ts = (json.loads(run0["request_json"]).get("execution_context") or {}).get("timeout_seconds") or ts
    cap = int(ts) + config.timeout_guard_seconds() + config.result_retrieval_window_seconds() + 60
    started = _now()
    last_recovery: dict | None = None
    last_cancel: dict | None = None
    yield _sse("open", {"run_id": run_id})
    while (_now() - started).total_seconds() < cap:
        for item in db.get_events(run_id, last_sent):
            last_sent = item["event"]["sequence"]
            yield _sse("event", item, id=last_sent)
        run = db.get_run(run_id)
        if run is None:
            yield _sse("stream_error", {"error": "unknown run"})
            return
        # Ephemeral recovery status (in-memory) — emit on change; {} clears the note.
        rec = get_recovery_status(run_id)
        if rec != last_recovery:
            last_recovery = rec
            yield _sse("recovering", rec or {})
        # Durable operator-cancel delivery state — emit on change; {} clears the note.
        cn = cancel_note(run)
        if cn != last_cancel:
            last_cancel = cn
            yield _sse("cancel", cn or {})
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
    view["recovery_status"] = get_recovery_status(run_id)   # ephemeral, in-memory
    view["cancel_note"] = cancel_note(run)                  # durable operator-cancel delivery state
    view["package_id"] = run.get("package_id")
    # Factual current-vs-execution snapshot comparison (NOT formal staleness — 3C-3):
    # what fingerprint this run used vs what the currently-registered package assembles
    # to now. If no source is registered under this package_id, there is no "current".
    cur_fp = None
    cur_src_id = None
    if run.get("package_id"):
        src = db.get_package_source_by_package_id(run["package_id"])
        if src:
            cur_src_id = src["id"]
            try:
                cur_fp = assemble(Path(src["root_path"]), src["id"]).package.fingerprint
            except Exception:
                cur_fp = None
    view["current_package_fingerprint"] = cur_fp
    view["current_source_registered"] = cur_fp is not None
    view["current_source_id"] = cur_src_id
    view["snapshot_is_current"] = cur_fp is not None and cur_fp == run.get("package_fingerprint")
    # 3C-3 current interpretation over the immutable evidence: review-aware effective
    # outcome and context-based staleness (package + task + capabilities + environment).
    profile = db.get_validation_profile(run.get("package_id"))
    review = db.get_review_resolution(run_id)
    st = status.run_current_status(run, cur_fp, profile, review)
    view["profile_configured"] = profile is not None
    view["review"] = review
    view["effective_outcome"] = st["effective_outcome"]
    view["outcome_source"] = st["outcome_source"]
    view["context_comparable"] = st["context_comparable"]
    view["context_is_current"] = st["is_current"]
    view["context_is_stale"] = st["is_stale"]
    view["events"] = db.get_events(run_id, 0)
    return view

"""HTTP client to Module 3 — transport only.

Module 1 talks to Module 3 exclusively over HTTP at MOD3_BASE_URL. This module
carries no contract knowledge and no validation logic: it sends and receives
JSON. Validation (outbound and inbound) is the orchestrator's job, using
workbench.contract. Nothing here imports Module 3 or the mock.

Step 2A uses the standard library (urllib) called off the event loop via
asyncio.to_thread. Graceful handling of transport failures (unreachable, 4xx/5xx,
malformed JSON) is Step 2B; here a failure raises GatewayError, which the
orchestrator records as a run in an error state without corrupting anything.
"""

from __future__ import annotations

import errno
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from workbench.config import mod3_base_url


class GatewayError(Exception):
    """Any transport-level failure talking to Module 3.

    `reason` categorises the failure so the orchestrator can tell *definite
    non-delivery* from an *indeterminate* failure — needed to classify an operator
    cancellation attempt (undelivered vs unknown). It is an internal transport
    detail only; it carries no Module 2 / contract meaning.
        'refused'  — connection actively refused (ECONNREFUSED): the endpoint was
                     reached at the transport layer and rejected the connection, so
                     no request bytes were delivered
        'dns'      — name resolution of the configured host failed, so no connection
                     to the configured endpoint was even attempted for this request
        'timeout'  — socket timeout: may or may not have reached M3 (indeterminate)
        'other'    — any other transport error (reset, broken pipe, …): indeterminate
    Only 'refused' and 'dns' are positive evidence that THIS attempt did not reach
    the configured endpoint — not a broader claim about all name-resolution errors.
    In urllib's flow name resolution precedes connect(), which precedes any request
    bytes, so a gaierror on this attempt means the configured endpoint was not
    contacted."""

    def __init__(self, message: str, reason: str = "other"):
        super().__init__(message)
        self.reason = reason


# Transport reasons that positively establish the request never reached Module 3.
NON_DELIVERY_REASONS = frozenset({"refused", "dns"})


def _classify(exc: BaseException) -> str:
    """Categorise a transport failure conservatively. Anything we cannot tie to a
    definite pre-delivery failure is 'timeout'/'other' (i.e. indeterminate) — a
    connection reset is NOT treated as non-delivery, since bytes may already have
    reached Module 3."""
    inner = getattr(exc, "reason", exc)   # urllib.error.URLError wraps the cause in .reason
    if isinstance(inner, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(inner, socket.gaierror):
        return "dns"
    if isinstance(inner, ConnectionRefusedError) or (
            isinstance(inner, OSError) and inner.errno == errno.ECONNREFUSED):
        return "refused"
    return "other"


def _parse(body: str) -> Any:
    """Parse a JSON body, or flag it as malformed while preserving the raw text.
    A malformed body is not JSON, so it is kept verbatim for the operator to see."""
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_malformed": True, "_raw": body}


def _call(method: str, path: str, payload: Any = None, timeout: float = 30.0) -> tuple[int, Any]:
    url = f"{mod3_base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx: return the status and any body
        return e.code, _parse(e.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError(f"{method} {url}: {e}", reason=_classify(e)) from e


def start(request: dict, forced_outcome: str | None = None, fault: str | None = None,
          timeout: float = 30.0) -> tuple[int, Any]:
    """start — POST the ValidationRequest. forced_outcome and fault, when present,
    are development-only out-of-band query parameters; they are never placed in the
    body, which carries only the contract ValidationRequest, and are only sent in
    dev/mock mode (the caller gates them). `timeout` bounds this single call."""
    params = {}
    if forced_outcome:
        params["forced_outcome"] = forced_outcome
    if fault:
        params["fault"] = fault
    path = "/runs" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return _call("POST", path, request, timeout=timeout)


def get_events(run_id: str, since: int = 0, timeout: float = 30.0) -> tuple[int, Any]:
    return _call("GET", f"/runs/{run_id}/events?since={since}", timeout=timeout)


def get_result(run_id: str, timeout: float = 30.0) -> tuple[int, Any]:
    return _call("GET", f"/runs/{run_id}/result", timeout=timeout)


def cancel(run_id: str, timeout: float = 30.0) -> tuple[int, Any]:
    """cancel — ask the Gateway to end the run. The Gateway emits a cancelled
    event and a cancelled result; Module 1's poller ingests them."""
    return _call("POST", f"/runs/{run_id}/cancel", timeout=timeout)

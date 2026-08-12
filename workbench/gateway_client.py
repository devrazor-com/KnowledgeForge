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

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from workbench.config import mod3_base_url


class GatewayError(Exception):
    """Any transport-level failure talking to Module 3."""


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
        raise GatewayError(f"{method} {url}: {e}") from e


def start(request: dict, forced_outcome: str | None = None, fault: str | None = None) -> tuple[int, Any]:
    """start — POST the ValidationRequest. forced_outcome and fault, when present,
    are development-only out-of-band query parameters; they are never placed in the
    body, which carries only the contract ValidationRequest, and are only sent in
    dev/mock mode (the caller gates them)."""
    params = {}
    if forced_outcome:
        params["forced_outcome"] = forced_outcome
    if fault:
        params["fault"] = fault
    path = "/runs" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return _call("POST", path, request)


def get_events(run_id: str, since: int = 0) -> tuple[int, Any]:
    return _call("GET", f"/runs/{run_id}/events?since={since}")


def get_result(run_id: str) -> tuple[int, Any]:
    return _call("GET", f"/runs/{run_id}/result")

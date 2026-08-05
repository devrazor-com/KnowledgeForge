#!/usr/bin/env python3
"""KFPOCMod1 — Module 1 (Validation Workbench) for the KnowledgeForge POC.

This is the complete Module 1 side of the Module 2 contract demonstration:

  * assembles a ValidationRequest from a Markdown knowledge package and a task
    (reading the index, following relative links, ordering deterministically,
    and computing REAL sha256 fingerprints);
  * validates the request against validation-request.schema.json before it
    would ever leave Module 1;
  * sends the request to Module 3 over HTTP, polls for ExecutionEvents,
    validates every event and the final ValidationResult it receives;
  * derives the verdict locally from the result.

Module 1 talks to Module 3 only over HTTP (base URL from the MOD3_BASE_URL
environment variable). It shares no Python code with Module 3 and imports
nothing from it — the only thing that crosses the boundary is Module 2 JSON.

IMPORTANT: Module 1 never invents events or results. It reports observable
facts it computed (the request, its own validation) and whatever Module 3
actually returns. With no Module 3 running, the events/result/verdict stages
show a plain "Module 3 not connected" state — nothing is simulated here.

Run:  MOD3_BASE_URL is optional (defaults to http://127.0.0.1:8003)
    ./.venv/bin/uvicorn KFPOCMod1:app --reload --port 8001
Then open http://127.0.0.1:8001
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# --------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------

POC_DIR = Path(__file__).resolve().parent
CONTRACT_DIR = POC_DIR.parent / "contract"
FIXTURES = POC_DIR / "fixtures"
PACKAGES_DIR = FIXTURES / "package"
TASKS_DIR = FIXTURES / "tasks"

# Where Module 3 lives. Sadia points this at the real Gateway by changing one
# environment variable; nothing else in Module 1 changes.
MOD3_BASE_URL = os.environ.get("MOD3_BASE_URL", "http://127.0.0.1:8003").rstrip("/")

CONTRACT_VERSION = "0.1"
TARGET_ENVIRONMENT = "larkspur-sandbox"
TIMEOUT_SECONDS = 1800
PERMITTED_CAPABILITIES = ["filesystem", "shell", "billing", "database-read"]

VALID_OUTCOMES = {"random", "success", "knowledge_gap", "technical_failure"}
TERMINAL_EVENTS = {"completed", "failed", "cancelled"}

# --------------------------------------------------------------------------
# Schema validation — Module 1's OWN copy. Module 3 has its own, independently.
# Neither imports the other; both load the same frozen files from contract/.
# --------------------------------------------------------------------------

from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

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
_REQUEST_VALIDATOR = Draft202012Validator(
    {"$ref": "validation-request.schema.json"}, registry=_REGISTRY
)
_EVENT_VALIDATOR = Draft202012Validator(
    {"$ref": "execution-event.schema.json"}, registry=_REGISTRY
)
_RESULT_VALIDATOR = Draft202012Validator(
    {"$ref": "validation-result.schema.json"}, registry=_REGISTRY
)


def _validate(validator: Draft202012Validator, instance: Any) -> dict:
    """Return a plain {passed, errors} report for the UI."""
    errors = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{loc}: {err.message}")
    return {"passed": not errors, "errors": errors}


# --------------------------------------------------------------------------
# Fingerprints — Module 1's responsibility ALONE. Module 3 never computes these.
# --------------------------------------------------------------------------

def _normalize_content(text: str) -> str:
    """Normalise line endings to \\n before hashing or sending."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def package_fingerprint(ordered_files: list[dict]) -> str:
    """sha256 over the ordered sequence of (relative path, normalised content)."""
    h = hashlib.sha256()
    for f in ordered_files:
        h.update(f["path"].encode("utf-8"))
        h.update(b"\n")
        h.update(f["content"].encode("utf-8"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def task_fingerprint(task: dict) -> str:
    """sha256 over the task's id, description, acceptance_criteria and checks."""
    h = hashlib.sha256()

    def upd(s: str) -> None:
        h.update(s.encode("utf-8"))
        h.update(b"\n")

    upd(task["id"])
    upd(task["description"])
    upd(task.get("acceptance_criteria") or "")
    for chk in task.get("checks", []) or []:
        upd(chk["id"])
        upd(chk["description"])
        upd(chk["command"])
    return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------------
# Package discovery and assembly
# --------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _find_main_file(pkg_dir: Path) -> str:
    """The package index: a top-level *.md whose name contains 'index'."""
    candidates = sorted(
        p.name for p in pkg_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and "index" in p.name.lower()
    )
    if not candidates:
        # Fall back to a lone top-level markdown file.
        md = sorted(p.name for p in pkg_dir.iterdir() if p.is_file() and p.suffix == ".md")
        if len(md) == 1:
            return md[0]
        raise ValueError(f"Cannot identify an index file in package '{pkg_dir.name}'")
    return candidates[0]


def _md_links(content: str) -> list[str]:
    out = []
    for m in _LINK_RE.finditer(content):
        target = m.group(1).strip()
        # Drop any "title" part and any #anchor.
        target = target.split()[0] if target else target
        target = target.split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.endswith(".md"):
            out.append(target)
    return out


def assemble_package(pkg_name: str) -> dict:
    """Read the index, follow relative links, order deterministically, fingerprint.

    Returns a dict with the assembled KnowledgePackage plus assembly detail for
    the UI (main file, link traversal, ordered file list).
    """
    pkg_dir = (PACKAGES_DIR / pkg_name).resolve()
    if not pkg_dir.is_dir() or PACKAGES_DIR.resolve() not in pkg_dir.parents:
        raise ValueError(f"Unknown package '{pkg_name}'")

    main_file = _find_main_file(pkg_dir)

    # Breadth-first traversal from the index, following relative markdown links.
    ordered_seen: list[str] = [main_file]
    queue: list[str] = [main_file]
    traversal: list[dict] = []
    while queue:
        current = queue.pop(0)
        current_path = pkg_dir / current
        content = _normalize_content(current_path.read_text(encoding="utf-8"))
        for link in _md_links(content):
            base = posixpath.dirname(current)
            resolved = posixpath.normpath(posixpath.join(base, link))
            # Never escape the package root.
            if resolved.startswith("..") or resolved.startswith("/"):
                traversal.append({"from": current, "link": link, "status": "skipped (outside package)"})
                continue
            if not (pkg_dir / resolved).is_file():
                traversal.append({"from": current, "link": link, "status": "skipped (missing file)"})
                continue
            if resolved in ordered_seen:
                traversal.append({"from": current, "link": link, "resolved": resolved, "status": "already included"})
                continue
            ordered_seen.append(resolved)
            queue.append(resolved)
            traversal.append({"from": current, "link": link, "resolved": resolved, "status": "included"})

    # Deterministic order: index first, then the rest sorted by path.
    rest = sorted(p for p in ordered_seen if p != main_file)
    ordered_paths = [main_file] + rest

    files = []
    for rel in ordered_paths:
        content = _normalize_content((pkg_dir / rel).read_text(encoding="utf-8"))
        files.append({"path": rel, "content": content})

    fp = package_fingerprint(files)

    package = {
        "name": pkg_name.capitalize() if pkg_name.islower() else pkg_name,
        "version": "1.4",
        "main_file": main_file,
        "files": files,
        "metadata": {"owner": f"{pkg_name} SME group", "source": f"fixtures/package/{pkg_name}"},
        "fingerprint": fp,
    }
    assembly = {
        "main_file": main_file,
        "link_traversal": traversal,
        "ordered_files": [{"path": f["path"], "chars": len(f["content"])} for f in files],
        "package_fingerprint": fp,
    }
    return {"package": package, "assembly": assembly}


_TASK_FIELDS = ["id", "title", "description", "business_area", "difficulty",
                "acceptance_criteria", "checks", "metadata"]


def load_task(task_file: str) -> dict:
    """Load a task fixture and build a ValidationTask with a freshly computed
    fingerprint (Module 1 recomputes it; it never trusts a stored value)."""
    if "/" in task_file or "\\" in task_file or not task_file.endswith(".json"):
        raise ValueError(f"Invalid task file '{task_file}'")
    path = (TASKS_DIR / task_file).resolve()
    if TASKS_DIR.resolve() != path.parent or not path.is_file():
        raise ValueError(f"Unknown task file '{task_file}'")
    raw = json.loads(path.read_text(encoding="utf-8"))

    task = {k: raw[k] for k in _TASK_FIELDS if k in raw}
    computed = task_fingerprint(task)
    stored = raw.get("fingerprint")
    task["fingerprint"] = computed
    return {
        "task": task,
        "task_fingerprint": computed,
        "fingerprint_matches_fixture": (stored == computed),
        "stored_fingerprint": stored,
    }


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def assemble_request(pkg_name: str, task_file: str, run_id: str | None = None) -> dict:
    """Assemble a complete ValidationRequest and report the assembly detail."""
    pkg = assemble_package(pkg_name)
    tsk = load_task(task_file)
    request = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id or new_run_id(),
        "package": pkg["package"],
        "task": tsk["task"],
        "execution_context": {
            "target_environment": TARGET_ENVIRONMENT,
            "timeout_seconds": TIMEOUT_SECONDS,
            "additional_instructions": None,
        },
        "permitted_capabilities": list(PERMITTED_CAPABILITIES),
    }
    assembly = dict(pkg["assembly"])
    assembly["task_fingerprint"] = tsk["task_fingerprint"]
    assembly["task_fingerprint_matches_fixture"] = tsk["fingerprint_matches_fixture"]
    return {"request": request, "assembly": assembly}


# --------------------------------------------------------------------------
# Verdict — Module 1's logic ALONE. Module 3 returns no outcome.
# Rules are evaluated in order; the first that matches wins.
# "Checks declared" is read from the SUBMITTED TASK, never from check_results.
# --------------------------------------------------------------------------

def derive_verdict(request: dict, result: dict) -> dict:
    status = result.get("status")
    declared = list((request.get("task") or {}).get("checks") or [])
    by_id = {c.get("check_id"): c for c in (result.get("check_results") or [])}

    def did_not_run(check_id: str) -> bool:
        c = by_id.get(check_id)
        return c is None or c.get("exit_code") is None

    def ran_and_failed(check_id: str) -> bool:
        c = by_id.get(check_id)
        return c is not None and c.get("exit_code") is not None and c.get("passed") is False

    # Rule 1
    if status == "cancelled":
        return {"outcome": "cancelled", "rule": 1, "reasoning": [
            {"sym": "info", "text": "Run status is 'cancelled' — someone stopped the run."},
            {"sym": "rule", "text": "Rule #1 applied."},
        ]}
    # Rule 2
    if status == "failed":
        return {"outcome": "inconclusive", "rule": 2, "reasoning": [
            {"sym": "info", "text": "Run status is 'failed' — a technical failure; the run did not complete."},
            {"sym": "info", "text": "The package was never really tested, so this is neither for nor against it."},
            {"sym": "rule", "text": "Rule #2 applied."},
        ]}
    # From here the run completed.
    # Rule 3
    if not declared:
        return {"outcome": "needs_review", "rule": 3, "reasoning": [
            {"sym": "info", "text": "No validation checks were declared in the submitted task."},
            {"sym": "rule", "text": "Rule #3 applied."},
        ]}

    ran = [c["id"] for c in declared if not did_not_run(c["id"])]
    failed = [c["id"] for c in declared if ran_and_failed(c["id"])]
    not_run = [c["id"] for c in declared if did_not_run(c["id"])]

    # Rule 4 (before Rule 5): a check that ran and failed is real evidence.
    if failed:
        reasoning = [
            {"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."},
            {"sym": "ok", "text": f"{len(ran)} check(s) executed."},
        ]
        for cid in failed:
            c = by_id[cid]
            reasoning.append({"sym": "bad", "text": f"{cid} ran and failed (exit {c.get('exit_code')})."})
        reasoning.append({"sym": "rule", "text": "Rule #4 applied."})
        return {"outcome": "failed", "rule": 4, "reasoning": reasoning}

    # Rule 5: some declared check did not run.
    if not_run:
        reasoning = [
            {"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."},
        ]
        if ran:
            reasoning.append({"sym": "ok", "text": f"{len(ran)} declared check(s) ran and passed."})
        for cid in not_run:
            reasoning.append({"sym": "info", "text": f"{cid} did not run (no result, or exit_code is null)."})
        reasoning.append({"sym": "rule", "text": "Rule #5 applied."})
        return {"outcome": "needs_review", "rule": 5, "reasoning": reasoning}

    # Rule 6: all declared checks ran and passed.
    return {"outcome": "passed", "rule": 6, "reasoning": [
        {"sym": "ok", "text": f"{len(declared)} check(s) were declared in the submitted task."},
        {"sym": "ok", "text": f"All {len(declared)} declared check(s) ran and passed."},
        {"sym": "rule", "text": "Rule #6 applied."},
    ]}


# --------------------------------------------------------------------------
# HTTP client to Module 3 (standard library only). Real calls, never faked.
# --------------------------------------------------------------------------

class Mod3Unreachable(Exception):
    pass


def _mod3_call(method: str, path: str, payload: Any = None, timeout: float = 30.0):
    url = f"{MOD3_BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, (json.loads(body) if body else None)
    except urllib.error.URLError as e:
        raise Mod3Unreachable(str(e.reason)) from e
    except (ConnectionError, OSError) as e:
        raise Mod3Unreachable(str(e)) from e


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="KFPOCMod1 — Validation Workbench (POC)")


def _list_packages() -> list[str]:
    out = []
    if PACKAGES_DIR.is_dir():
        for d in sorted(PACKAGES_DIR.iterdir()):
            if d.is_dir():
                try:
                    _find_main_file(d)
                    out.append(d.name)
                except ValueError:
                    pass
    return out


def _list_tasks() -> list[str]:
    return sorted(p.name for p in TASKS_DIR.glob("*.json")) if TASKS_DIR.is_dir() else []


@app.get("/api/assemble")
def api_assemble(package: str, task: str, outcome: str = "random"):
    """Everything Module 1 can do on its own: assemble, fingerprint, validate.
    Does NOT contact Module 3 — it only reports the HTTP operation it WOULD use."""
    if outcome not in VALID_OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {sorted(VALID_OUTCOMES)}")
    try:
        assembled = assemble_request(package, task)
    except ValueError as e:
        raise HTTPException(400, str(e))

    request = assembled["request"]
    validation = _validate(_REQUEST_VALIDATOR, request)

    forced = None if outcome == "random" else outcome
    planned_url = f"{MOD3_BASE_URL}/runs" + (f"?forced_outcome={forced}" if forced else "")

    return JSONResponse({
        "run_id": request["run_id"],
        "inputs": {
            "package": package,
            "task_file": task,
            "outcome_selector": outcome,
            "selected_files": [f["path"] for f in request["package"]["files"]],
        },
        "assembly": assembled["assembly"],
        "request": request,
        "request_validation_module1": {
            "validator": "Module 1",
            "schema": "validation-request.schema.json",
            **validation,
        },
        "planned_http": {
            "operation": "start",
            "method": "POST",
            "url": planned_url,
            "forced_outcome_param": forced,
            "note": "This is the request that would cross the Module 2 boundary. "
                    "'forced_outcome' is a POC-only, out-of-band query parameter — "
                    "it is NOT part of the contract (the request schema is "
                    "additionalProperties:false).",
        },
    })


@app.post("/api/runs")
def api_start_run(body: dict = Body(...)):
    """Assemble + validate (again, before sending), then really send to Module 3.
    With no Module 3 running this returns a clean 'module3_unreachable' state —
    Module 1 does not invent a run."""
    package = body.get("package")
    task = body.get("task")
    outcome = body.get("outcome", "random")
    if outcome not in VALID_OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {sorted(VALID_OUTCOMES)}")
    try:
        assembled = assemble_request(package, task)
    except ValueError as e:
        raise HTTPException(400, str(e))

    request = assembled["request"]
    validation = _validate(_REQUEST_VALIDATOR, request)
    if not validation["passed"]:
        # Module 1 refuses to send an invalid request.
        return JSONResponse({"sent": False, "reason": "request_invalid",
                             "request": request,
                             "request_validation_module1": validation})

    forced = None if outcome == "random" else outcome
    path = "/runs" + (f"?forced_outcome={forced}" if forced else "")
    try:
        status, data = _mod3_call("POST", path, request)
    except Mod3Unreachable as e:
        return JSONResponse({"sent": False, "reason": "module3_unreachable",
                             "detail": str(e), "attempted_url": f"{MOD3_BASE_URL}{path}",
                             "request": request,
                             "request_validation_module1": validation})

    return JSONResponse({"sent": True, "run_id": request["run_id"],
                        "http": {"method": "POST", "url": f"{MOD3_BASE_URL}{path}", "status": status},
                        "request": request,
                        "request_validation_module1": validation,
                        "module3_response": data})


@app.get("/api/runs/{run_id}/events")
def api_events(run_id: str, since: int = 0):
    """Fetch events after `since` from Module 3 and validate each one (Module 1 side)."""
    try:
        status, data = _mod3_call("GET", f"/runs/{run_id}/events?since={since}")
    except Mod3Unreachable as e:
        return JSONResponse({"connected": False, "detail": str(e)})
    events = (data or {}).get("events", data) or []
    annotated = []
    terminal = False
    for ev in events:
        annotated.append({"event": ev, "validation_module1": _validate(_EVENT_VALIDATOR, ev)})
        if isinstance(ev, dict) and ev.get("event_type") in TERMINAL_EVENTS:
            terminal = True
    return JSONResponse({"connected": True, "events": annotated, "terminal": terminal,
                        "module3_validation": (data or {}).get("module3_validation")})


@app.get("/api/runs/{run_id}/result")
def api_result(run_id: str):
    """Fetch the result from Module 3, validate it, and derive the verdict."""
    try:
        status, data = _mod3_call("GET", f"/runs/{run_id}/result")
    except Mod3Unreachable as e:
        return JSONResponse({"connected": False, "detail": str(e)})
    result = (data or {}).get("result", data)
    if not result:
        return JSONResponse({"connected": True, "ready": False})
    validation = _validate(_RESULT_VALIDATOR, result)
    payload = {"connected": True, "ready": True, "result": result,
               "result_validation_module1": validation,
               "module3_validation": (data or {}).get("module3_validation")}
    if validation["passed"]:
        # We still need the original request's task to know what checks were declared.
        # The caller supplies it via a follow-up; here we derive from the result alone
        # is impossible, so the browser passes the request task in. See /api/verdict.
        pass
    return JSONResponse(payload)


@app.post("/api/verdict")
def api_verdict(body: dict = Body(...)):
    """Derive the verdict from a request + result (both already seen by the UI)."""
    request = body.get("request")
    result = body.get("result")
    if not request or not result:
        raise HTTPException(400, "request and result are required")
    return JSONResponse({"verdict": derive_verdict(request, result)})


@app.post("/api/runs/{run_id}/cancel")
def api_cancel(run_id: str):
    try:
        status, data = _mod3_call("POST", f"/runs/{run_id}/cancel")
    except Mod3Unreachable as e:
        return JSONResponse({"connected": False, "detail": str(e)})
    return JSONResponse({"connected": True, "acknowledgement": data})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    packages = _list_packages()
    tasks = _list_tasks()
    pkg_options = "".join(f'<option value="{p}">{p}</option>' for p in packages)
    task_options = "".join(f'<option value="{t}">{t}</option>' for t in tasks)
    return PAGE.replace("{{PKG_OPTIONS}}", pkg_options) \
               .replace("{{TASK_OPTIONS}}", task_options) \
               .replace("{{MOD3_BASE_URL}}", MOD3_BASE_URL)


# --------------------------------------------------------------------------
# The single server-rendered page (inline CSS + vanilla JS, no build step).
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>KnowledgeForge POC — Module 1 (Validation Workbench)</title>
<style>
  :root {
    --m1: #1f6feb; --m1bg: #e8f0fe;
    --m2: #0d9488; --m2bg: #e6f6f4;
    --m3: #d97706; --m3bg: #fdf2e2;
    --verdict: #6d28d9; --verdictbg: #f1ebfb;
    --ok: #157347; --bad: #b02a37; --muted: #6b7280; --line: #d0d7de;
  }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; color: #1f2328; background: #f6f8fa; }
  header { background: #24292f; color: #fff; padding: 16px 24px; }
  header h1 { margin: 0; font-size: 18px; }
  header p { margin: 4px 0 0; color: #c9d1d9; font-size: 13px; }
  main { max-width: 1120px; margin: 0 auto; padding: 20px 24px 80px; }
  .controls { background: #fff; border: 1px solid var(--line); border-radius: 10px;
              padding: 16px; margin-bottom: 20px; }
  .row { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-weight: 600; font-size: 12px; color: #57606a; }
  select, button { font: inherit; padding: 7px 10px; border-radius: 7px;
                   border: 1px solid var(--line); background: #fff; }
  button { cursor: pointer; font-weight: 600; }
  button.primary { background: var(--m1); color: #fff; border-color: var(--m1); }
  button.send { background: var(--m2); color: #fff; border-color: var(--m2); }
  button.ghost { background: #fff; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .poc { border: 1px dashed var(--m3); background: var(--m3bg); border-radius: 8px;
         padding: 8px 10px; }
  .poc .tag { display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: .04em;
              text-transform: uppercase; color: var(--m3); margin-right: 6px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 0; font-size: 12px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
  .flow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 18px 0; }
  .flow .node { font-size: 12px; font-weight: 600; padding: 6px 10px; border-radius: 999px;
                border: 1px solid var(--line); background: #fff; cursor: pointer; }
  .flow .arrow { color: var(--muted); }
  .stage { background: #fff; border: 1px solid var(--line); border-left-width: 5px;
           border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }
  .stage h2 { margin: 0 0 2px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
  .stage .who { font-size: 11px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .04em; padding: 2px 7px; border-radius: 999px; }
  .stage .sub { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
  .m1 { border-left-color: var(--m1); } .m1 .who { background: var(--m1bg); color: var(--m1); }
  .m2 { border-left-color: var(--m2); } .m2 .who { background: var(--m2bg); color: var(--m2); }
  .m3 { border-left-color: var(--m3); } .m3 .who { background: var(--m3bg); color: var(--m3); }
  .vd { border-left-color: var(--verdict); } .vd .who { background: var(--verdictbg); color: var(--verdict); }
  .panel { border: 1px solid var(--line); border-radius: 8px; margin: 8px 0; overflow: hidden; }
  .panel > summary { cursor: pointer; padding: 8px 12px; background: #f6f8fa; font-weight: 600;
                     font-size: 12px; display: flex; justify-content: space-between; align-items: center; }
  .panel pre { margin: 0; padding: 12px; overflow-x: auto; background: #fbfcfd;
               font: 12px/1.5 SFMono-Regular, Consolas, monospace; }
  .copy { font-size: 11px; padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line);
          background: #fff; cursor: pointer; font-weight: 600; }
  .badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
           font-size: 12px; font-weight: 600; }
  .badge.pass { background: #e6f4ea; color: var(--ok); border: 1px solid #abd5b8; }
  .badge.fail { background: #fbe9eb; color: var(--bad); border: 1px solid #e6b3ba; }
  .badge.wait { background: #eef1f4; color: var(--muted); border: 1px solid var(--line); }
  .notconn { border: 1px dashed var(--line); border-radius: 8px; padding: 16px; text-align: center;
             color: var(--muted); background: #fbfcfd; }
  .notconn strong { color: #57606a; }
  ul.reason { list-style: none; padding: 0; margin: 8px 0 0; }
  ul.reason li { padding: 2px 0; }
  .sym-ok::before { content: "✓ "; color: var(--ok); font-weight: 700; }
  .sym-bad::before { content: "✗ "; color: var(--bad); font-weight: 700; }
  .sym-info::before { content: "• "; color: var(--muted); }
  .sym-rule { font-weight: 700; margin-top: 4px; }
  .sym-rule::before { content: "→ "; }
  .verdict-word { font-size: 22px; font-weight: 800; letter-spacing: .02em; }
  table.mini { border-collapse: collapse; font-size: 12px; margin-top: 6px; }
  table.mini td, table.mini th { border: 1px solid var(--line); padding: 4px 8px; text-align: left; }
  .kv { font-size: 12px; color: #57606a; } .kv b { color: #1f2328; }
  .fp { font-family: SFMono-Regular, Consolas, monospace; font-size: 11px; word-break: break-all; }
</style>
</head>
<body>
<header>
  <h1>KnowledgeForge POC — Module 1 · Validation Workbench</h1>
  <p>Assembles a ValidationRequest, computes real fingerprints, and validates it against the Module 2 schema before it would ever leave Module 1. Module 3 is a separate process (Step 3).</p>
</header>
<main>
  <div class="controls">
    <div class="row">
      <div class="field">
        <label for="pkg">Knowledge package (Markdown folder)</label>
        <select id="pkg">{{PKG_OPTIONS}}</select>
      </div>
      <div class="field">
        <label for="task">Task (JSON)</label>
        <select id="task">{{TASK_OPTIONS}}</select>
      </div>
      <div class="field poc">
        <label for="outcome"><span class="tag">POC-only</span>Forced outcome — not a contract field</label>
        <select id="outcome">
          <option value="random">Random</option>
          <option value="success">Success</option>
          <option value="knowledge_gap">Knowledge gap</option>
          <option value="technical_failure">Technical failure</option>
        </select>
      </div>
      <button id="assembleBtn" class="primary">Assemble &amp; Validate</button>
      <button id="sendBtn" class="send" disabled>Send to Module 3 ▶</button>
      <button id="cancelBtn" class="ghost" disabled>Cancel run</button>
    </div>
    <div class="legend">
      <span><span class="swatch" style="background:var(--m1)"></span> Produced by Module 1</span>
      <span><span class="swatch" style="background:var(--m2)"></span> Crosses the Module 2 boundary</span>
      <span><span class="swatch" style="background:var(--m3)"></span> Produced by Module 3</span>
      <span><span class="swatch" style="background:var(--verdict)"></span> Conclusion — Module 1 only</span>
    </div>
    <div class="flow" id="flow"></div>
  </div>

  <div id="stages"></div>
</main>

<script>
const MOD3_BASE_URL = "{{MOD3_BASE_URL}}";
const FLOW = ["Inputs","Module 1 Assembly","ValidationRequest","Module 2 Contract",
              "Module 3 Mock","ExecutionEvents","ValidationResult","Module 1 Verdict"];
const flowEl = document.getElementById("flow");
FLOW.forEach((n, i) => {
  const node = document.createElement("span");
  node.className = "node"; node.textContent = (i+1)+". "+n;
  node.onclick = () => document.getElementById("stage-"+i)?.scrollIntoView({behavior:"smooth", block:"start"});
  flowEl.appendChild(node);
  if (i < FLOW.length-1) { const a = document.createElement("span"); a.className="arrow"; a.textContent="→"; flowEl.appendChild(a); }
});

let LAST = { request: null, result: null };

function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function jsonPanel(title, obj, open=false){
  const text = JSON.stringify(obj, null, 2);
  const id = "p"+Math.random().toString(36).slice(2);
  return `<details class="panel" ${open?"open":""}>
      <summary>${esc(title)}
        <button class="copy" data-copy="${id}">Copy</button></summary>
      <pre id="${id}">${esc(text)}</pre></details>`;
}

function validationBadge(v){
  if(!v) return `<span class="badge wait">not validated</span>`;
  return v.passed
    ? `<span class="badge pass">✓ ${esc(v.validator||"validated")} — schema OK</span>`
    : `<span class="badge fail">✗ ${esc(v.validator||"validation")} failed (${v.errors.length})</span>`;
}

function stage(i, cls, who, title, sub, inner){
  return `<section class="stage ${cls}" id="stage-${i}">
    <h2><span class="who">${esc(who)}</span> ${i+1}. ${esc(title)}</h2>
    <p class="sub">${sub}</p>${inner}</section>`;
}

function notConnected(what){
  return `<div class="notconn"><strong>Module 3 not connected.</strong><br/>
    ${what} appear here once Module 3 is running and the request has been sent (Step 3).</div>`;
}

function render(a){
  LAST.request = a.request; LAST.result = null;
  const s = [];
  const rv = a.request_validation_module1;

  // 1. Inputs
  s.push(stage(0, "m1", "Module 1 · input", "Inputs",
    "The Markdown package and task you selected.",
    `<div class="kv">Package: <b>${esc(a.inputs.package)}</b> &nbsp;·&nbsp; Task file: <b>${esc(a.inputs.task_file)}</b>
       &nbsp;·&nbsp; Forced outcome: <b>${esc(a.inputs.outcome_selector)}</b> <span class="kv">(POC-only)</span></div>
     <table class="mini"><tr><th>Selected files (assembly order)</th></tr>
       ${a.inputs.selected_files.map(f=>`<tr><td class="fp">${esc(f)}</td></tr>`).join("")}</table>`));

  // 2. Module 1 Assembly
  const asm = a.assembly;
  s.push(stage(1, "m1", "Module 1", "Module 1 Assembly",
    "Read the index, followed relative links, ordered deterministically, and computed real sha256 fingerprints.",
    `<div class="kv">Index (main_file): <b>${esc(asm.main_file)}</b></div>
     <div class="kv">Package fingerprint: <span class="fp">${esc(asm.package_fingerprint)}</span></div>
     <div class="kv">Task fingerprint: <span class="fp">${esc(asm.task_fingerprint)}</span>
       ${asm.task_fingerprint_matches_fixture ? '<span class="badge pass">matches fixture</span>' : '<span class="badge wait">recomputed</span>'}</div>
     ${jsonPanel("Link traversal + ordered files", {main_file: asm.main_file, link_traversal: asm.link_traversal, ordered_files: asm.ordered_files})}`));

  // 3. ValidationRequest
  s.push(stage(2, "m2", "Crosses Module 2", "ValidationRequest",
    "The complete message Module 1 would send. Module 1 validates it before sending.",
    `${validationBadge(rv)}
     ${rv && !rv.passed ? jsonPanel("Validation errors", rv.errors, true) : ""}
     ${jsonPanel("ValidationRequest JSON", a.request, true)}`));

  // 4. Module 2 Contract (the operation)
  const h = a.planned_http;
  s.push(stage(3, "m2", "Module 2 · operation", "Module 2 Contract",
    "The 'start' operation that carries the request across the boundary.",
    `<div class="kv"><b>${esc(h.method)}</b> <span class="fp">${esc(h.url)}</span></div>
     <div class="kv" style="margin-top:6px">${esc(h.note)}</div>`));

  // 5. Module 3 Mock
  s.push(stage(4, "m3", "Module 3", "Module 3 Mock",
    "The Execution Gateway. Validates the request on receipt and runs it.",
    `<div id="m3ack">${notConnected("Module 3's acceptance and run_id")}</div>`));

  // 6. ExecutionEvents
  s.push(stage(5, "m3", "Module 3 → Module 1", "ExecutionEvents",
    "Streamed while the run is in flight. Module 3 validates before sending; Module 1 validates on receipt.",
    `<div id="events">${notConnected("ExecutionEvents")}</div>`));

  // 7. ValidationResult
  s.push(stage(6, "m3", "Module 3 → Module 1", "ValidationResult",
    "Returned once the run is terminal. Validated by both sides.",
    `<div id="result">${notConnected("The ValidationResult")}</div>`));

  // 8. Verdict
  s.push(stage(7, "vd", "Module 1 only", "Module 1 Verdict",
    "Derived locally from the result. Module 3 returns no outcome.",
    `<div id="verdict">${notConnected("The verdict and its reasoning")}</div>`));

  document.getElementById("stages").innerHTML = s.join("");
  document.getElementById("sendBtn").disabled = !(rv && rv.passed);
}

async function assemble(){
  const pkg = document.getElementById("pkg").value;
  const task = document.getElementById("task").value;
  const outcome = document.getElementById("outcome").value;
  const btn = document.getElementById("assembleBtn"); btn.disabled = true; btn.textContent = "Assembling…";
  try {
    const r = await fetch(`/api/assemble?package=${encodeURIComponent(pkg)}&task=${encodeURIComponent(task)}&outcome=${encodeURIComponent(outcome)}`);
    if(!r.ok){ alert("Assembly failed: " + (await r.text())); return; }
    render(await r.json());
  } finally { btn.disabled = false; btn.textContent = "Assemble & Validate"; }
}

function renderEvents(container, list){
  container.innerHTML = list.map((a, i) => {
    const ev = a.event;
    return `<div style="margin-bottom:6px">
      <div class="kv">#${ev.sequence} <b>${esc(ev.event_type)}</b> — ${esc(ev.message)}
        &nbsp; ${validationBadge(a.validation_module1)}</div>
      ${jsonPanel("ExecutionEvent #"+ev.sequence, ev)}</div>`;
  }).join("");
}

function renderVerdict(container, v){
  const word = v.outcome.toUpperCase().replace("_"," ");
  container.innerHTML = `<div class="verdict-word" style="color:var(--verdict)">Verdict: ${esc(word)}</div>
    <ul class="reason">${v.reasoning.map(r=>`<li class="sym-${r.sym}">${esc(r.text)}</li>`).join("")}</ul>`;
}

async function pollRun(runId){
  const cancelBtn = document.getElementById("cancelBtn"); cancelBtn.disabled = false;
  let since = 0, terminal = false;
  const evBox = document.getElementById("events"); evBox.innerHTML = "";
  const all = [];
  while(!terminal){
    const r = await fetch(`/api/runs/${runId}/events?since=${since}`);
    const d = await r.json();
    if(!d.connected){ evBox.innerHTML = notConnected("ExecutionEvents") + `<div class="kv">${esc(d.detail||"")}</div>`; return; }
    for(const a of d.events){ all.push(a); since = Math.max(since, a.event.sequence); }
    renderEvents(evBox, all);
    terminal = d.terminal;
    if(!terminal) await new Promise(res=>setTimeout(res, 800));
  }
  // result
  const rr = await fetch(`/api/runs/${runId}/result`); const rd = await rr.json();
  const resBox = document.getElementById("result");
  if(rd.connected && rd.ready){
    resBox.innerHTML = `${validationBadge(rd.result_validation_module1)}
      ${rd.module3_validation ? `<span class="badge pass">✓ Module 3 — validated before sending</span>` : ""}
      ${jsonPanel("ValidationResult JSON", rd.result, true)}`;
    const vr = await fetch(`/api/verdict`, {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({request: LAST.request, result: rd.result})});
    renderVerdict(document.getElementById("verdict"), (await vr.json()).verdict);
  }
  cancelBtn.disabled = true;
}

async function send(){
  if(!LAST.request){ return; }
  const pkg = document.getElementById("pkg").value;
  const task = document.getElementById("task").value;
  const outcome = document.getElementById("outcome").value;
  const m3 = document.getElementById("m3ack");
  m3.innerHTML = `<div class="kv">Sending to Module 3…</div>`;
  const r = await fetch(`/api/runs`, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({package: pkg, task: task, outcome: outcome})});
  const d = await r.json();
  if(!d.sent){
    if(d.reason === "module3_unreachable"){
      m3.innerHTML = `<div class="notconn"><strong>Module 3 not connected.</strong><br/>
        Module 1 attempted <span class="fp">POST ${esc(d.attempted_url)}</span> and got:
        <div class="kv">${esc(d.detail)}</div>
        Module 1 did not invent a run. Start Module 3 (Step 3) and send again.</div>`;
    } else {
      m3.innerHTML = `<div class="notconn"><strong>Request not sent — ${esc(d.reason)}.</strong></div>`;
    }
    return;
  }
  m3.innerHTML = `<div class="kv">Module 3 accepted the run.
    ${d.module3_response && d.module3_response.module3_validation ? `<span class="badge pass">✓ Module 3 — validated request on receipt</span>`:""}</div>
    ${jsonPanel("Module 3 response", d.module3_response||{})}`;
  LAST.request = d.request;
  pollRun(d.run_id);
}

async function cancelRun(){
  if(!LAST.request) return;
  await fetch(`/api/runs/${LAST.request.run_id}/cancel`, {method:"POST"});
}

document.addEventListener("click", e => {
  if(e.target.classList.contains("copy")){
    const pre = document.getElementById(e.target.dataset.copy);
    navigator.clipboard.writeText(pre.textContent).then(()=>{
      const t = e.target.textContent; e.target.textContent = "Copied"; setTimeout(()=>e.target.textContent=t, 900);
    });
  }
});
document.getElementById("assembleBtn").onclick = assemble;
document.getElementById("sendBtn").onclick = send;
document.getElementById("cancelBtn").onclick = cancelRun;
</script>
</body>
</html>
"""

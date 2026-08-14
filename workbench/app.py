"""FastAPI app and screens.

Step 1: Packages, Package detail.
Step 2A: Start a run, the live Run screen (SSE), and a JSON status endpoint.

Server-rendered Jinja templates + light JavaScript, no build step. No route-module
split — kept as one file until readability actually demands otherwise.

Run from the repository root:
    ./workbench/.venv/bin/uvicorn workbench.app:app --reload --port 8010
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from workbench import config, db, orchestrator
from workbench import status as vstatus
from workbench.packages import (
    PackageError, catalog_status, load_source, normalize_root, read_manifest, source_id,
)

_NO_STORE = {"Cache-Control": "no-store"}   # the catalog/detail reflect live registry state

app = FastAPI(title="KnowledgeForge — Validation Workbench")
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    # Reconcile any non-terminal local attempts left by a previous process. Runs as
    # a background task so startup is never blocked. (The registry is NOT auto-
    # populated: the real Workbench starts empty and the operator registers roots.)
    asyncio.create_task(orchestrator.recover_inflight_runs())


def _source_or_404(source_id_: str) -> dict:
    src = db.get_package_source(source_id_)
    if src is None:
        raise HTTPException(404, f"Unknown package source '{source_id_}'")
    return src


def _packages_view(request: Request, add_error: str | None = None, status: int = 200):
    # CHEAP per-source status only (M2) — structural health + durable-state flags; NO
    # full assembly/fingerprint of every package. Authoritative current/stale/eligible
    # status is computed on package detail, where assembly is warranted.
    rows = []
    for s in db.list_package_sources():
        r = catalog_status(s)
        pid = s.get("package_id")
        r["profile_configured"] = pid is not None and db.get_validation_profile(pid) is not None
        r["approval_recorded"] = pid is not None and db.latest_approval(pid) is not None
        if r["status"] != "ok":
            r["board"] = "unhealthy"
        elif not r["profile_configured"]:
            r["board"] = "needs_profile"
        elif r["approval_recorded"]:
            r["board"] = "approval_recorded"   # narrow: currency is confirmed on detail
        else:
            r["board"] = "no_approval"
        rows.append(r)
    return templates.TemplateResponse(request, "packages.html",
                                      {"rows": rows, "add_error": add_error},
                                      status_code=status, headers=_NO_STORE)


@app.get("/")
def packages(request: Request):
    return _packages_view(request)


@app.post("/packages")
def add_package(request: Request, root_path: str = Form(...)):
    """Register an operator-supplied package root (trusted-localhost model, NFR-6).
    Reject only a path that cannot be a package root at all (missing / not a dir);
    a directory with a bad or missing manifest is still registered and shown
    unhealthy so the operator can see and fix what broke."""
    raw = (root_path or "").strip()
    if not raw:
        return _packages_view(request, "A package root path is required.", status=400)
    norm = normalize_root(raw)
    p = Path(norm)
    if not p.exists():
        return _packages_view(request, f"Path does not exist: {norm}", status=400)
    if not p.is_dir():
        return _packages_view(request, f"Not a directory: {norm}", status=400)
    existing = db.get_package_source_by_path(norm)
    if existing is not None:                       # already registered (any id) → open it
        return RedirectResponse(url=f"/packages/{existing['id']}", status_code=303)
    # Resolve the durable identity if the package is structurally valid. An unhealthy
    # root (no/invalid manifest or package_id) is still registered — with a NULL id —
    # so it stays visible as Unhealthy with a reason.
    package_id = None
    try:
        package_id = read_manifest(p).package_id
    except PackageError:
        package_id = None
    if package_id is not None:
        conflict = db.get_package_source_by_package_id(package_id)
        if conflict is not None:                   # one ACTIVE source per package_id
            return _packages_view(
                request, f"A package with id '{package_id}' is already registered from "
                f"{conflict['root_path']}. Remove that registration first to register a "
                f"different root under the same identity.", status=409)
    sid = source_id(norm)
    db.add_package_source(sid, norm, package_id)
    return RedirectResponse(url=f"/packages/{sid}", status_code=303)


@app.post("/packages/{source_id_}/remove")
def remove_package(source_id_: str):
    db.remove_package_source(source_id_)
    return RedirectResponse(url="/", status_code=303)


@app.post("/packages/{source_id_}/change-root")
def change_root(source_id_: str, root_path: str = Form(...)):
    """Repoint an existing registration at a new machine-local root WITHOUT changing
    identity. This is the operator repair for a package whose files moved (e.g. a
    folder transferred from another machine): the new root must be a valid package
    whose manifest declares the SAME durable package_id. Only the mutable
    source/root association moves — package_id, validation profile, run history,
    immutable snapshots and review/approval history are all preserved, and no
    historical evidence is reinterpreted. A pure location change therefore never
    makes otherwise-identical knowledge stale (staleness keys off the package
    FINGERPRINT and validation context, never the root)."""
    src = _source_or_404(source_id_)
    registered_id = src.get("package_id")
    if not registered_id:
        # A NULL-id registration has no identity to preserve; there is nothing to keep
        # continuous. The operator should register the (now valid) new root directly.
        raise HTTPException(400, "This registration has no durable identity, so its root "
                            "cannot be repointed. Remove it and register the new root directly.")
    raw = (root_path or "").strip()
    if not raw:
        raise HTTPException(400, "A new package root path is required.")
    norm = normalize_root(raw)
    p = Path(norm)
    if not p.exists():
        raise HTTPException(400, f"Path does not exist: {norm}")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {norm}")
    try:
        manifest = read_manifest(p)
    except PackageError as e:
        raise HTTPException(400, f"The new root is not a valid package: {e}")
    if manifest.package_id != registered_id:
        raise HTTPException(400,
            f"Identity mismatch: this package is registered as '{registered_id}', but the new "
            f"root declares package_id '{manifest.package_id}'. Change root repoints the SAME "
            f"package to a new location — it never changes identity. Register the new root "
            f"separately if it is a different package.")
    other = db.get_package_source_by_path(norm)
    if other is not None and other["id"] != src["id"]:
        raise HTTPException(409, f"That root is already registered (as '{other['id']}'). "
                            "Remove that registration first.")
    db.update_package_source_root(src["id"], norm)
    return RedirectResponse(url=f"/packages/{src['id']}", status_code=303)


def _package_context(source_id_: str):
    """Assemble a healthy package, overlay task active-state, and compute current status.
    Returns (src_view, profile, tasks, pstat) — pstat/tasks empty if unhealthy."""
    src = _source_or_404(source_id_)
    view = load_source(src)
    package_id = src.get("package_id")
    profile = db.get_validation_profile(package_id)
    tasks, pstat, newly_stored = [], None, False
    if view["assembly"] is not None:
        newly_stored = db.save_snapshot(view["assembly"])
        for t in view["tasks"]:
            db.save_task(view["name"], t)
            t.active = db.get_active(view["name"], t.id)
            tasks.append(t)
        pstat = vstatus.package_status(package_id, view["fingerprint"], tasks, profile)
    return src, view, profile, tasks, pstat, newly_stored


@app.get("/packages/{source_id_}")
def package_detail(request: Request, source_id_: str):
    _src, view, profile, tasks, pstat, newly_stored = _package_context(source_id_)
    return templates.TemplateResponse(request, "package_detail.html", {
        "src": view, "assembly": view["assembly"], "package": view["assembly"].package if view["assembly"] else None,
        "tasks": tasks, "newly_stored": newly_stored, "profile": profile, "pstat": pstat,
        "capabilities": config.CAPABILITIES, "environments": config.ENVIRONMENTS,
        "dev_mock": config.dev_mock_mode(),
    }, headers=_NO_STORE)


@app.post("/packages/{source_id_}/profile")
def configure_profile(source_id_: str, environment: str = Form(...),
                      capabilities: list[str] = Form(default=[]), configured_by: str = Form(default="")):
    src = _source_or_404(source_id_)
    package_id = src.get("package_id")
    if not package_id:
        raise HTTPException(400, "This source has no valid package identity; fix the package first.")
    if environment not in config.ENVIRONMENTS:
        raise HTTPException(400, f"Unknown target environment '{environment}'.")
    caps = [c for c in capabilities if c in config.CAPABILITIES]
    db.set_validation_profile(package_id, environment, caps, (configured_by or "").strip() or None)
    return RedirectResponse(url=f"/packages/{source_id_}", status_code=303)


@app.post("/packages/{source_id_}/approve")
def approve_package(source_id_: str, approved_by: str = Form(...)):
    src, view, profile, tasks, pstat, _ = _package_context(source_id_)
    if pstat is None:
        raise HTTPException(400, "Cannot approve an unhealthy package.")
    if not (approved_by or "").strip():
        raise HTTPException(400, "An approver name is required.")
    if not pstat["eligible"]:                       # APR-2: server re-checks; never auto-approves
        raise HTTPException(400, f"Not approval-eligible: {pstat['eligibility_reason']}")
    db.add_approval(src["package_id"], approved_by.strip(), view["fingerprint"],
                    pstat["active_task_fingerprints"], profile["target_environment"],
                    profile["capabilities"], pstat["qualifying_runs"])
    return RedirectResponse(url=f"/packages/{source_id_}", status_code=303)


@app.post("/runs/{run_id}/review")
def resolve_review(run_id: str, resolution: str = Form(...), resolved_by: str = Form(...)):
    view = orchestrator.run_view(run_id)
    if view is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    if resolution not in ("passed", "failed"):
        raise HTTPException(400, "Resolution must be 'passed' or 'failed'.")
    if not (resolved_by or "").strip():
        raise HTTPException(400, "A resolver name is required.")
    v = view.get("verdict")
    if not (v and v.get("outcome") == "needs_review"):
        raise HTTPException(400, "Only a needs_review run can be resolved by a human.")
    db.set_review_resolution(run_id, resolved_by.strip(), resolution)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.post("/packages/{source_id_}/tasks/{task_id}/toggle")
def toggle_task(source_id_: str, task_id: str):
    src = _source_or_404(source_id_)
    view = load_source(src)
    if view["assembly"] is not None:
        db.set_active(view["name"], task_id, not db.get_active(view["name"], task_id))
    return RedirectResponse(url=f"/packages/{source_id_}#task-{task_id}", status_code=303)


@app.post("/runs")
async def start_run(
    source_id_: str = Form(..., alias="source_id"),
    task: str = Form(...),
    environment: str = Form(...),
    capabilities: list[str] = Form(default=[]),
    forced_outcome: str | None = Form(default=None),
    fault: str | None = Form(default=None),
):
    src = _source_or_404(source_id_)
    root = Path(src["root_path"])
    # forced_outcome and fault are dev-only; ignored entirely unless dev/mock mode is on.
    dev = config.dev_mock_mode()
    forced = forced_outcome if (dev and forced_outcome not in (None, "", "random")) else None
    fault_val = fault if (dev and fault not in (None, "", "none")) else None
    try:
        run_id = await orchestrator.start_run(
            root, src["id"], src.get("package_id"), task, capabilities, environment, forced, fault_val)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.get("/packages/{source_id_}/history")
def package_history(request: Request, source_id_: str):
    """This package's validation history (HST-1), keyed by durable package_id — spans
    every package_fingerprint it has had, independent of the mutable source. Each row
    is immutable evidence; the current-vs-execution snapshot flag is factual only."""
    src = _source_or_404(source_id_)
    view = load_source(src)
    package_id = src.get("package_id")
    current_fp = view.get("fingerprint")
    profile = db.get_validation_profile(package_id)
    runs = db.runs_for_package(package_id) if package_id else []
    for r in runs:
        r["capabilities"] = json.loads(r["capabilities_json"]) if r.get("capabilities_json") else []
        r["verdict"] = json.loads(r["verdict_json"]) if r.get("verdict_json") else None
        # Context-based currency (matches eligibility + the evidence view): a run is
        # current only if its full validation context equals the current one — so an
        # env/capability override under the same fingerprint reads as stale here too.
        if profile and current_fp:
            cur_ctx = vstatus.validation_context_id(
                current_fp, r["task_fingerprint"], profile["capabilities"], profile["target_environment"])
            r["context_current"] = vstatus.run_context_id(r) == cur_ctx
        else:
            r["context_current"] = None
    return templates.TemplateResponse(request, "history.html",
                                      {"src": view, "runs": runs, "current_fingerprint": current_fp,
                                       "profile_configured": profile is not None},
                                      headers=_NO_STORE)


@app.get("/runs/{run_id}")
def run_screen(request: Request, run_id: str):
    view = orchestrator.run_view(run_id)
    if view is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return templates.TemplateResponse(request, "run.html", {
        "run": view, "mod3_base_url": config.mod3_base_url()})


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    if orchestrator.run_view(run_id) is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return JSONResponse(await orchestrator.request_cancel(run_id))


@app.get("/runs/{run_id}/stream")
async def run_stream(request: Request, run_id: str):
    if orchestrator.run_view(run_id) is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    # SSE resume cursor. A native EventSource sends Last-Event-ID on auto-reconnect;
    # a fresh connection sends nothing. Parse conservatively — missing/blank/invalid
    # falls back to 0 (full replay).
    raw = request.headers.get("last-event-id")
    try:
        last_event_id = int(raw) if raw not in (None, "") else 0
    except ValueError:
        last_event_id = 0
    return StreamingResponse(orchestrator.stream(run_id, last_event_id),
                             media_type="text/event-stream")


@app.get("/runs/{run_id}/panel")
def run_panel(request: Request, run_id: str):
    """Server-rendered readable ValidationResult fragment. app.js injects this on
    live completion; it is also included inline in the Run screen for a terminal
    run, and is what the diagnosis-rendering test asserts against."""
    view = orchestrator.run_view(run_id)
    if view is None or not view.get("result"):
        raise HTTPException(404, "no result for this run")
    return templates.TemplateResponse(request, "_result.html", {"run": view})


@app.get("/help")
def help_page(request: Request):
    from workbench import vocab
    return templates.TemplateResponse(request, "help.html", {
        "outcomes": vocab.OUTCOMES, "error_kinds": vocab.ERROR_KINDS,
        "cancel_states": vocab.CANCEL_DELIVERY_STATES,
    })


@app.get("/api/runs/{run_id}")
def run_json(run_id: str):
    view = orchestrator.run_view(run_id)
    if view is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return JSONResponse(view)

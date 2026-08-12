"""FastAPI app and screens.

Step 1: Packages, Package detail.
Step 2A: Start a run, the live Run screen (SSE), and a JSON status endpoint.

Server-rendered Jinja templates + light JavaScript, no build step. No route-module
split — kept as one file until readability actually demands otherwise.

Run from the repository root:
    ./workbench/.venv/bin/uvicorn workbench.app:app --reload --port 8010
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from workbench import config, db, orchestrator
from workbench.packages import PackageError, assemble, list_package_dirs
from workbench.tasks import load_tasks

app = FastAPI(title="KnowledgeForge — Validation Workbench")
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


@app.on_event("startup")
def _startup() -> None:
    db.init()


def _assemble_dir(dir_name: str):
    pkg_dir = (config.PACKAGES_DIR / dir_name).resolve()
    if config.PACKAGES_DIR.resolve() not in pkg_dir.parents or not pkg_dir.is_dir():
        raise HTTPException(404, f"Unknown package '{dir_name}'")
    try:
        return assemble(pkg_dir, dir_name)
    except PackageError as e:
        raise HTTPException(400, str(e))


@app.get("/")
def packages(request: Request):
    rows = []
    for dir_name in list_package_dirs():
        a = _assemble_dir(dir_name)
        rows.append({
            "dir_name": dir_name, "name": a.package.name, "version": a.package.version,
            "fingerprint": a.package.fingerprint, "file_count": len(a.package.files),
            "task_count": len(load_tasks(config.PACKAGES_DIR / dir_name)),
            "problem_count": len(a.problems),
        })
    return templates.TemplateResponse(request, "packages.html", {"rows": rows})


@app.get("/packages/{dir_name}")
def package_detail(request: Request, dir_name: str):
    a = _assemble_dir(dir_name)
    newly_stored = db.save_snapshot(a)
    tasks = []
    for t in load_tasks(config.PACKAGES_DIR / dir_name):
        db.save_task(a.package.name, t)
        t.active = db.get_active(a.package.name, t.id)
        tasks.append(t)
    return templates.TemplateResponse(request, "package_detail.html", {
        "assembly": a, "package": a.package, "tasks": tasks, "newly_stored": newly_stored,
        "capabilities": config.CAPABILITIES, "environments": config.ENVIRONMENTS,
        "dev_mock": config.dev_mock_mode(),
    })


@app.post("/packages/{dir_name}/tasks/{task_id}/toggle")
def toggle_task(dir_name: str, task_id: str):
    a = _assemble_dir(dir_name)
    db.set_active(a.package.name, task_id, not db.get_active(a.package.name, task_id))
    return RedirectResponse(url=f"/packages/{dir_name}#task-{task_id}", status_code=303)


@app.post("/runs")
async def start_run(
    dir_name: str = Form(...),
    task: str = Form(...),
    environment: str = Form(...),
    capabilities: list[str] = Form(default=[]),
    forced_outcome: str | None = Form(default=None),
    fault: str | None = Form(default=None),
):
    _assemble_dir(dir_name)  # validates the package exists
    # forced_outcome and fault are dev-only; ignored entirely unless dev/mock mode is on.
    dev = config.dev_mock_mode()
    forced = forced_outcome if (dev and forced_outcome not in (None, "", "random")) else None
    fault_val = fault if (dev and fault not in (None, "", "none")) else None
    try:
        run_id = await orchestrator.start_run(dir_name, task, capabilities, environment, forced, fault_val)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


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
async def run_stream(run_id: str):
    if orchestrator.run_view(run_id) is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return StreamingResponse(orchestrator.stream(run_id), media_type="text/event-stream")


@app.get("/runs/{run_id}/panel")
def run_panel(request: Request, run_id: str):
    """Server-rendered readable ValidationResult fragment. app.js injects this on
    live completion; it is also included inline in the Run screen for a terminal
    run, and is what the diagnosis-rendering test asserts against."""
    view = orchestrator.run_view(run_id)
    if view is None or not view.get("result"):
        raise HTTPException(404, "no result for this run")
    return templates.TemplateResponse(request, "_result.html", {"run": view})


@app.get("/api/runs/{run_id}")
def run_json(run_id: str):
    view = orchestrator.run_view(run_id)
    if view is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return JSONResponse(view)

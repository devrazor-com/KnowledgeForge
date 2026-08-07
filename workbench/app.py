"""FastAPI app and the Step 1 screens: Packages and Package detail.

Server-rendered Jinja templates, light JavaScript, no build step (house pattern).
This step has no Gateway interaction and no run persistence — it proves that a
package can be loaded, discovered, ordered, fingerprinted, stored immutably, and
seen, with its tasks, in the UI.

Run from the repository root:
    ./workbench/.venv/bin/uvicorn workbench.app:app --reload --port 8010
Then open http://127.0.0.1:8010
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from workbench import config, db
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
        assembly = _assemble_dir(dir_name)
        tasks = load_tasks(config.PACKAGES_DIR / dir_name)
        rows.append({
            "dir_name": dir_name,
            "name": assembly.package.name,
            "version": assembly.package.version,
            "fingerprint": assembly.package.fingerprint,
            "file_count": len(assembly.package.files),
            "task_count": len(tasks),
            "problem_count": len(assembly.problems),
        })
    return templates.TemplateResponse(request, "packages.html", {"rows": rows})


@app.get("/packages/{dir_name}")
def package_detail(request: Request, dir_name: str):
    assembly = _assemble_dir(dir_name)
    # Store the snapshot immutably (PKG-8). Newly-seen content becomes a new row;
    # identical content is a no-op.
    newly_stored = db.save_snapshot(assembly)

    tasks = load_tasks(config.PACKAGES_DIR / dir_name)
    task_rows = []
    for t in tasks:
        db.save_task(assembly.package.name, t)
        t.active = db.get_active(assembly.package.name, t.id)
        task_rows.append(t)

    return templates.TemplateResponse(request, "package_detail.html", {
        "assembly": assembly,
        "package": assembly.package,
        "tasks": task_rows,
        "newly_stored": newly_stored,
    })


@app.post("/packages/{dir_name}/tasks/{task_id}/toggle")
def toggle_task(dir_name: str, task_id: str):
    assembly = _assemble_dir(dir_name)
    current = db.get_active(assembly.package.name, task_id)
    db.set_active(assembly.package.name, task_id, not current)
    return RedirectResponse(url=f"/packages/{dir_name}#task-{task_id}", status_code=303)

"""Package registration end to end over real HTTP (Step 3C-1).

Registers package roots through the real POST /packages path (seeding disabled),
proves registration persists across a restart, that the detail screen exposes root/
entry/health, that a structurally-different second package runs through the SAME
production pipeline and returns its own domain's result, that the manifest is absent
from the KnowledgePackage.files sent over Module 2, that an unhealthy root stays
visible, and that removal works.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = str(REPO_ROOT / "workbench" / "examples" / "larkspur")
CLAIMS = str(REPO_ROOT / "workbench" / "examples" / "claims")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_ready(port, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _start(module, port, env):
    return subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
                            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p is None:
            continue
        try:
            p.terminate(); p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def _env(mock_port, dbpath):
    e = os.environ.copy()
    e["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
    e["WORKBENCH_TIMEOUT_SECONDS"] = "60"
    e["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
    return e


class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post(wb_port, path, fields):
    """POST form-encoded; return (status, location, body)."""
    try:
        with urllib.request.build_opener(_NR).open(urllib.request.Request(
                f"http://127.0.0.1:{wb_port}{path}",
                data=urllib.parse.urlencode(fields).encode(), method="POST")) as r:
            return r.status, r.headers.get("Location"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.read().decode()


def _register(wb_port, root_path):
    status, loc, _ = _post(wb_port, "/packages", [("root_path", root_path)])
    assert status == 303, (status, root_path)
    return loc.rsplit("/", 1)[-1]              # source id


def _get(wb_port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}{path}", timeout=5) as r:
        return r.read().decode()


def _api(wb_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _run(wb_port, source_id, task, environment, forced):
    status, loc, _ = _post(wb_port, "/runs", [
        ("source_id", source_id), ("task", task), ("environment", environment),
        ("capabilities", "filesystem"), ("forced_outcome", forced)])
    assert status == 303, status
    return loc.rsplit("/", 1)[-1]


def _poll(wb_port, run_id, until, timeout=40.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        v = _api(wb_port, run_id)
        if until(v):
            return v
        time.sleep(0.25)
    return v


def test_operator_path_form_registrations_accumulate(tmp_path):
    """Regression for the disappearing-package defect, via the EXACT operator path:
    empty → register A through POST /packages → A listed → open A's detail → register
    B through POST /packages → A AND B listed → open A → open B. Adding B must never
    make A vanish from the catalog."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        assert "No packages registered yet" in _get(wb_port, "/")
        a = _register(wb_port, LARKSPUR)
        assert "Larkspur" in _get(wb_port, "/")                       # A listed
        assert "Larkspur" in _get(wb_port, f"/packages/{a}")          # operator opens A's detail
        b = _register(wb_port, CLAIMS)
        home = _get(wb_port, "/")
        assert "Larkspur" in home and "Claims Adjudication" in home   # A did NOT disappear
        assert "Larkspur" in _get(wb_port, f"/packages/{a}")          # A still opens
        assert "Claims Adjudication" in _get(wb_port, f"/packages/{b}")
        # The live catalog must not be cached by the browser.
        with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/", timeout=5) as r:
            assert "no-store" in (r.headers.get("Cache-Control") or "")
    finally:
        _stop(wb)


def test_remove_keeps_other_sources_and_retains_evidence(tmp_path):
    """Three sources accumulate; removing one unregisters ONLY that source — the other
    sources stay, the removed source's files are untouched, its historical run/evidence
    remains interpretable, and it can be registered again."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    third = tmp_path / "third" / "larkspur"
    shutil.copytree(LARKSPUR, third)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        a = _register(wb_port, LARKSPUR)
        b = _register(wb_port, CLAIMS)
        c = _register(wb_port, str(third))
        assert len({a, b, c}) == 3
        assert "Claims Adjudication" in _get(wb_port, "/")

        # Produce durable evidence against B (claims).
        rid = _run(wb_port, b, "CLAIM-TASK-001", "claims-sandbox", "success_claims")
        v = _poll(wb_port, rid, lambda v: v["run_state"] == "terminal")
        assert v["outcome"] == "passed"
        fp = v["package_fingerprint"]

        # Remove B's registration only.
        _post(wb_port, f"/packages/{b}/remove", [])
        home = _get(wb_port, "/")
        assert "Claims Adjudication" not in home                       # B unregistered from the catalog
        assert "Larkspur" in _get(wb_port, f"/packages/{a}")           # A still opens
        assert "Larkspur" in _get(wb_port, f"/packages/{c}")           # C still opens

        # B's source files are NOT deleted.
        assert (Path(CLAIMS) / "package.yaml").is_file()
        assert (Path(CLAIMS) / "claims-overview.md").is_file()
        assert (Path(CLAIMS) / "validation" / "task-adjudicate.json").is_file()

        # B's historical run/evidence remains and is interpretable via its snapshot fingerprint.
        v2 = _api(wb_port, rid)
        assert v2["run_state"] == "terminal" and v2["outcome"] == "passed" and v2["package_fingerprint"] == fp

        # B can be registered again.
        b2 = _register(wb_port, CLAIMS)
        assert "Claims Adjudication" in _get(wb_port, "/") and b2 == b
    finally:
        _stop(wb, mock)


def test_register_persist_run_both_packages_remove(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)

        # Starts empty (seeding disabled).
        assert "No packages registered yet" in _get(wb_port, "/")

        # 1) register both roots.
        lark = _register(wb_port, LARKSPUR)
        clm = _register(wb_port, CLAIMS)
        home = _get(wb_port, "/")
        assert "Larkspur" in home and "Claims Adjudication" in home

        # 3) detail exposes root, entry point, health.
        detail = _get(wb_port, f"/packages/{clm}")
        assert CLAIMS in detail and "claims-overview.md" in detail and "Healthy" in detail

        # 2) registration survives a restart.
        _stop(wb)
        wb = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        assert "Larkspur" in _get(wb_port, "/") and "Claims Adjudication" in _get(wb_port, "/")

        # 5/6) both run through the same pipeline; the second returns its own domain.
        lr = _run(wb_port, lark, "LARK-TASK-001", "larkspur-sandbox", "success")
        v1 = _poll(wb_port, lr, lambda v: v["run_state"] == "terminal")
        assert v1["outcome"] == "passed" and v1["verdict"]["rule"] == 6

        cr = _run(wb_port, clm, "CLAIM-TASK-001", "claims-sandbox", "success_claims")
        v2 = _poll(wb_port, cr, lambda v: v["run_state"] == "terminal")
        assert v2["outcome"] == "passed" and v2["verdict"]["rule"] == 6
        assert "adjudication" in v2["result"]["summary"].lower()          # claims-domain evidence
        assert "priority_support" not in v2["result"]["summary"]          # not Larkspur's

        # 10) package.yaml is absent from the KnowledgePackage.files sent over Module 2.
        sent_paths = [f["path"] for f in v2["request"]["package"]["files"]]
        assert "package.yaml" not in sent_paths
        assert "claims-overview.md" in sent_paths

        # 7) an unhealthy registered root stays visible.
        broken = tmp_path / "broken"; broken.mkdir()
        (broken / "readme.md").write_text("# not a package\n", encoding="utf-8")
        bid = _register(wb_port, str(broken))
        assert "Unhealthy" in _get(wb_port, "/")
        assert "cannot be assembled" in _get(wb_port, f"/packages/{bid}")

        # 8) removal works.
        _post(wb_port, f"/packages/{bid}/remove", [])
        assert "Unhealthy" not in _get(wb_port, "/")
        _post(wb_port, f"/packages/{lark}/remove", [])
        assert "Larkspur" not in _get(wb_port, "/")
        assert "Claims Adjudication" in _get(wb_port, "/")    # unaffected
    finally:
        _stop(wb, mock)

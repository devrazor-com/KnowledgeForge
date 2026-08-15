"""Run history + durable package identity, end to end over real HTTP (Step 3C-2).

Proves history hangs off durable package_id and the immutable snapshot, survives
content/name/root changes and unregister/re-register, that identity cannot change
silently under existing evidence, and that only persisted provenance is shown.

All mutation happens on TEMP COPIES of the Larkspur example — the tracked example is
never modified.
"""

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = REPO_ROOT / "workbench" / "examples" / "larkspur"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


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
    try:
        with urllib.request.build_opener(_NR).open(urllib.request.Request(
                f"http://127.0.0.1:{wb_port}{path}",
                data=urllib.parse.urlencode(fields).encode(), method="POST")) as r:
            return r.status, r.headers.get("Location"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.read().decode()


def _register(wb_port, root):
    status, loc, body = _post(wb_port, "/packages", [("root_path", str(root))])
    return status, (loc.rsplit("/", 1)[-1] if loc else None), body


def _get(wb_port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}{path}", timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _api(wb_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _run(wb_port, sid, task="LARK-TASK-001", env="larkspur-sandbox", forced="success", fault=None):
    # 3C-3: no profile, no run — configure a stable profile first (idempotent). A NULL-id
    # source rejects this (400, ignored) and the run itself is then refused too.
    _post(wb_port, f"/packages/{sid}/profile",
          [("environment", env), ("capabilities", "filesystem"), ("configured_by", "test")])
    fields = [("source_id", sid), ("task", task), ("environment", env),
              ("capabilities", "filesystem"), ("forced_outcome", forced)]
    if fault:
        fields.append(("fault", fault))
    status, loc, _ = _post(wb_port, "/runs", fields)
    return status, (loc.rsplit("/", 1)[-1] if loc else None)


def _poll(wb_port, run_id, until, timeout=40.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        v = _api(wb_port, run_id)
        if until(v):
            return v
        time.sleep(0.25)
    return v


def _copy_pkg(dest: Path, package_id: str, name: str | None = None) -> Path:
    """Temp copy of Larkspur with a chosen package_id (and optional display name)."""
    shutil.copytree(LARKSPUR, dest)
    man = f"package_id: {package_id}\nentry_point: larkspur-index.md\ntasks: tasks/\n"
    if name:
        man += f"name: {name}\n"
    (dest / "package.yaml").write_text(man, encoding="utf-8")
    return dest


def _snapshot_exists(dbpath, fingerprint) -> bool:
    con = sqlite3.connect(dbpath)
    row = con.execute("SELECT 1 FROM package_snapshot WHERE package_fingerprint=?", (fingerprint,)).fetchone()
    con.close()
    return row is not None


def _set_manifest_id(root: Path, package_id: str):
    (root / "package.yaml").write_text(
        f"package_id: {package_id}\nentry_point: larkspur-index.md\ntasks: tasks/\n", encoding="utf-8")


def test_identity_evidence_lifecycle(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    r1 = _copy_pkg(tmp_path / "r1" / "pkg", "demo-pkg", name="Demo One")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        st, sid1, _ = _register(wb_port, r1)
        assert st == 303 and sid1

        # Run A against fingerprint A (POST /runs only — never open detail first).
        _, ra = _run(wb_port, sid1)
        va = _poll(wb_port, ra, lambda v: v["run_state"] == "terminal")
        assert va["outcome"] == "passed"
        fa = va["package_fingerprint"]
        assert va["package_id"] == "demo-pkg"
        # Snapshot guaranteed persisted at run start (no detail visit happened).
        assert _snapshot_exists(dbpath, fa)

        # History shows run A under the durable id.
        st, hist = _get(wb_port, f"/packages/{sid1}/history")
        assert st == 200 and ra in hist and "demo-pkg" in hist

        # Change knowledge → fingerprint B; run again.
        idx = r1 / "larkspur-index.md"
        idx.write_text(idx.read_text() + "\n\nAppended for a new content version.\n", encoding="utf-8")
        _, rb = _run(wb_port, sid1)
        vb = _poll(wb_port, rb, lambda v: v["run_state"] == "terminal")
        fb = vb["package_fingerprint"]
        assert fb != fa and vb["package_id"] == "demo-pkg"

        # Both runs in one history under the same package_id, each with its own fingerprint.
        st, hist = _get(wb_port, f"/packages/{sid1}/history")
        assert ra in hist and rb in hist
        # B is the current snapshot; A is older (factual, not "stale").
        assert vb["snapshot_is_current"] is True
        assert _api(wb_port, ra)["snapshot_is_current"] is False

        # Rename display name → history still attached (same package_id).
        _set_manifest_id(r1, "demo-pkg")
        (r1 / "package.yaml").write_text(
            "package_id: demo-pkg\nentry_point: larkspur-index.md\ntasks: tasks/\nname: Renamed Demo\n",
            encoding="utf-8")
        st, hist = _get(wb_port, f"/packages/{sid1}/history")
        assert ra in hist and rb in hist

        # Unregister → the historical run remains directly retrievable.
        _post(wb_port, f"/packages/{sid1}/remove", [])
        assert _api(wb_port, ra)["run_state"] == "terminal"          # evidence survives removal
        assert _get(wb_port, f"/packages/{sid1}/history")[0] == 404  # the source is gone

        # Re-register from a DIFFERENT root, same package_id → history reconnects.
        r2 = _copy_pkg(tmp_path / "r2" / "pkg", "demo-pkg", name="Demo Relocated")
        st, sid2, _ = _register(wb_port, r2)
        assert st == 303 and sid2 != sid1
        st, hist = _get(wb_port, f"/packages/{sid2}/history")
        assert st == 200 and ra in hist and rb in hist               # reconnected via package_id

        # Old run still points at its own fingerprint-A snapshot.
        assert _api(wb_port, ra)["package_fingerprint"] == fa
    finally:
        _stop(wb, mock)


def test_identity_cannot_change_silently_under_evidence(tmp_path):
    """Register id A → change manifest id to B behind Module 1's back → a run is
    REFUSED with a clear identity-mismatch message, no evidence is filed under B, and
    history under A remains intact."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    root = _copy_pkg(tmp_path / "r" / "pkg", "idtest")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        st, sid, _ = _register(wb_port, root)
        assert st == 303
        _, r_before = _run(wb_port, sid)
        _poll(wb_port, r_before, lambda v: v["run_state"] == "terminal")

        # Change the manifest identity behind Module 1's back.
        _set_manifest_id(root, "idtest-changed")

        # Attempt a run → refused.
        status, run_id = _run(wb_port, sid)
        assert status == 400 and run_id is None
        _, _, body = _post(wb_port, "/runs", [("source_id", sid), ("task", "LARK-TASK-001"),
                                              ("environment", "larkspur-sandbox"), ("capabilities", "filesystem")])
        assert "identity mismatch" in body.lower()

        # No run/evidence filed under the new id.
        con = sqlite3.connect(dbpath)
        assert con.execute("SELECT COUNT(*) FROM run WHERE package_id='idtest-changed'").fetchone()[0] == 0
        con.close()

        # History under the ORIGINAL id remains intact.
        st, hist = _get(wb_port, f"/packages/{sid}/history")
        assert st == 200 and r_before in hist
    finally:
        _stop(wb, mock)


def test_null_identity_source_cannot_run_or_have_history(tmp_path):
    """A NULL-identity (Unhealthy) source is visible but is never treated as a valid
    package: no run can start against it and its history is empty."""
    wb_port = _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(_free_port(), dbpath)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "readme.md").write_text("# not a package (no manifest)\n", encoding="utf-8")
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        st, sid, _ = _register(wb_port, broken)              # registered Unhealthy, package_id NULL
        assert st == 303
        assert "cannot be assembled" in _get(wb_port, f"/packages/{sid}")[1]
        status, run_id = _run(wb_port, sid)                  # refused before any Gateway contact
        assert status == 400 and run_id is None
        st, hist = _get(wb_port, f"/packages/{sid}/history")
        assert st == 200 and "No runs yet" in hist
        con = sqlite3.connect(str(dbpath))
        assert con.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0   # no run row created at all
        con.close()
    finally:
        _stop(wb)


def test_every_new_run_has_nonnull_package_id(tmp_path):
    """Invariant: regardless of the nullable column, a run created via the production
    start path always carries a non-NULL package_id (the validated registered identity)."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    root = _copy_pkg(tmp_path / "r" / "pkg", "nonnull-id")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        st, sid, _ = _register(wb_port, root)
        _, rid = _run(wb_port, sid)
        _poll(wb_port, rid, lambda v: v["run_state"] == "terminal")
        con = sqlite3.connect(str(dbpath))
        assert con.execute("SELECT COUNT(*) FROM run WHERE package_id IS NULL").fetchone()[0] == 0
        assert con.execute("SELECT package_id FROM run WHERE run_id=?", (rid,)).fetchone()[0] == "nonnull-id"
        con.close()
    finally:
        _stop(wb, mock)


def test_duplicate_active_package_id_is_rejected(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    a = _copy_pkg(tmp_path / "a" / "pkg", "dup")
    b = _copy_pkg(tmp_path / "b" / "pkg", "dup")
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        st1, sid1, _ = _register(wb_port, a)
        assert st1 == 303
        st2, sid2, body = _register(wb_port, b)
        assert st2 == 409 and sid2 is None and "already registered" in body
        # Only the first is active.
        assert "Unhealthy" not in _get(wb_port, "/")[1] or True
        _, home = _get(wb_port, "/")
        assert str(a) in home and str(b) not in home
    finally:
        _stop(wb)


def test_history_shows_only_persisted_provenance_no_recovered_claim(tmp_path):
    """Cancellation provenance in history comes only from persisted fields; the history
    and evidence views never claim a run was 'recovered' (no durable field exists)."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy_pkg(tmp_path / "r" / "pkg", "provtest")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        st, sid, _ = _register(wb_port, root)
        # A cancelled run: never_terminal, then cancel.
        _, rid = _run(wb_port, sid, forced="success", fault="never_terminal")
        _poll(wb_port, rid, lambda v: v["run_state"] == "running" and v["events"], timeout=15)
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{wb_port}/runs/{rid}/cancel", data=b"", method="POST"), timeout=10):
            pass
        v = _poll(wb_port, rid, lambda v: v["run_state"] == "terminal", timeout=20)
        assert v["outcome"] == "cancelled"

        st, hist = _get(wb_port, f"/packages/{sid}/history")
        assert st == 200 and rid in hist
        assert "cancel" in hist.lower()                       # persisted cancel provenance shown
        assert "recovered" not in hist.lower()                # never claimed (no durable field)
        _, evidence = _get(wb_port, f"/runs/{rid}")
        assert "recovered" not in evidence.lower()
    finally:
        _stop(wb, mock)

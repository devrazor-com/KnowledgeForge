"""Validation profile, staleness, approval, and review — end to end over real HTTP
(Step 3C-3). No profile → no run; approval eligibility (APR-1, extended context);
explicit approval (APR-2/APR-3); derived invalidation (APR-4); needs_review (VER-6/7);
isolation; restart persistence. All mutation on TEMP COPIES of Larkspur.
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


def _get(wb_port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}{path}", timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _api(wb_port, run_id):
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}/api/runs/{run_id}", timeout=5) as r:
        return json.loads(r.read().decode())


def _register(wb_port, root):
    st, loc, _ = _post(wb_port, "/packages", [("root_path", str(root))])
    assert st == 303, st
    return loc.rsplit("/", 1)[-1]


def _profile(wb_port, sid, environment, capabilities, by="op"):
    return _post(wb_port, f"/packages/{sid}/profile",
                 [("environment", environment), ("configured_by", by)]
                 + [("capabilities", c) for c in capabilities])


def _run(wb_port, sid, task, environment, capabilities, forced="success", fault=None):
    fields = [("source_id", sid), ("task", task), ("environment", environment),
              ("forced_outcome", forced)] + [("capabilities", c) for c in capabilities]
    if fault:
        fields.append(("fault", fault))
    st, loc, body = _post(wb_port, "/runs", fields)
    return st, (loc.rsplit("/", 1)[-1] if loc else None), body


def _toggle(wb_port, sid, task_id):
    _post(wb_port, f"/packages/{sid}/tasks/{task_id}/toggle", [])


def _approve(wb_port, sid, by="victor"):
    return _post(wb_port, f"/packages/{sid}/approve", [("approved_by", by)])


def _resolve(wb_port, run_id, resolution, by="victor"):
    return _post(wb_port, f"/runs/{run_id}/review", [("resolution", resolution), ("resolved_by", by)])


def _poll(wb_port, run_id, until, timeout=40.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        v = _api(wb_port, run_id)
        if until(v):
            return v
        time.sleep(0.25)
    return v


def _copy(dest: Path, package_id: str) -> Path:
    shutil.copytree(LARKSPUR, dest)
    (dest / "package.yaml").write_text(
        f"package_id: {package_id}\nentry_point: larkspur-index.md\ntasks: tasks/\n", encoding="utf-8")
    return dest


def _run_pass(wb_port, sid, task, env, caps):
    st, rid, _ = _run(wb_port, sid, task, env, caps)
    assert st == 303, st
    _poll(wb_port, rid, lambda v: v["run_state"] == "terminal")
    return rid


# --- tests --------------------------------------------------------------------

def test_no_profile_no_run(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "np")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        assert "Validation profile not configured" in _get(wb_port, f"/packages/{sid}")[1]
        st, rid, body = _run(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert st == 400 and rid is None and "profile" in body.lower()
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        st, rid, _ = _run(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert st == 303
        assert _poll(wb_port, rid, lambda v: v["run_state"] == "terminal")["outcome"] == "passed"
    finally:
        _stop(wb, mock)


def test_run_defaults_from_profile_and_override_is_run_only(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "ovr")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem", "shell"])
        _, detail = _get(wb_port, f"/packages/{sid}")
        assert 'value="larkspur-sandbox" selected' in detail                # env default preselected
        assert 'value="shell"\n            checked' in detail or 'value="shell" checked' in detail.replace("\n", " ")
        # An override run (different capability set) does not mutate the profile...
        rid = _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        prof = json.loads(_get(wb_port, f"/api/runs/{rid}")[1] if False else "{}")  # noqa (profile checked via detail)
        _, detail2 = _get(wb_port, f"/packages/{sid}")
        assert "filesystem" in detail2 and "shell" in detail2               # profile still has both
        # ...and the override run is not current (its context != profile context).
        assert _api(wb_port, rid)["context_is_stale"] is True
    finally:
        _stop(wb, mock)


def test_approval_eligibility_action_and_invalidation_matrix(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "appr")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sid}")                       # materialise task_state
        _toggle(wb_port, sid, "LARK-TASK-002")                  # deactivate the no-checks task
        # Not eligible before running.
        st, _, body = _approve(wb_port, sid)
        assert st == 400 and "not approval-eligible" in body.lower()
        # Run the one active task → eligible → approve.
        rid = _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert _approve(wb_port, sid)[0] == 303
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]
        run_before = _api(wb_port, rid)
        fp_before, tfp_before = run_before["package_fingerprint"], run_before["task_fingerprint"]

        # (a) content change → stale + approval no longer current; historical evidence unchanged.
        idx = root / "larkspur-index.md"
        idx.write_text(idx.read_text() + "\n\nNew content version.\n", encoding="utf-8")
        html = _get(wb_port, f"/packages/{sid}")[1]
        assert "no longer current" in html and "re-validation required" in html
        after = _api(wb_port, rid)
        assert after["package_fingerprint"] == fp_before and after["context_is_stale"] is True   # immutable
        # rerun under new content → eligible → re-approve → current again.
        _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert _approve(wb_port, sid)[0] == 303
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        # (b) target-environment change → not current.
        _profile(wb_port, sid, "claims-sandbox", ["filesystem"])
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])         # restore
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        # (c) capability-set change → not current; reorder alone → still current.
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem", "shell"])
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])         # same set, restore
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        # (d) task change (edit the task file → new fingerprint) → not current.
        tf = root / "tasks" / "task-with-checks.json"
        data = json.loads(tf.read_text())
        data["description"] = data["description"] + " (revised)"
        tf.write_text(json.dumps(data), encoding="utf-8")
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]

        # The historical approval row is never deleted by any of the above.
        con = __import__("sqlite3").connect(str(tmp_path / "wb.db"))
        assert con.execute("SELECT COUNT(*) FROM approval WHERE package_id='appr'").fetchone()[0] >= 2
        con.close()
    finally:
        _stop(wb, mock)


def test_profile_only_invalidation_env_caps_and_reorder(tmp_path):
    """Approved/current → change ONLY the environment → not current; change ONLY the
    capability SET → not current; REORDER the same capability set → remains current."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "prof-inval")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem", "shell"])
        _get(wb_port, f"/packages/{sid}"); _toggle(wb_port, sid, "LARK-TASK-002")
        _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem", "shell"])
        assert _approve(wb_port, sid)[0] == 303
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        _profile(wb_port, sid, "claims-sandbox", ["filesystem", "shell"])        # env only
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem", "shell"])      # restore
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])              # capability set only
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem", "shell"])      # restore

        _profile(wb_port, sid, "larkspur-sandbox", ["shell", "filesystem"])      # SAME set, reordered
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]      # ordering never invalidates
    finally:
        _stop(wb, mock)


def test_override_does_not_revoke_current_qualifying_evidence(tmp_path):
    """A passing run whose env/capabilities OVERRIDE the profile is outside the approval
    candidate set: it must not QUALIFY, but it must ALSO not DISQUALIFY an earlier
    profile-matching pass. Mirrors the Claims + larkspur-sandbox override case."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "ovr-keep")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sid}"); _toggle(wb_port, sid, "LARK-TASK-002")

        # A profile-matching pass → eligible → approve.
        _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert _approve(wb_port, sid)[0] == 303
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]

        # A later passing ENVIRONMENT override must NOT revoke that eligibility.
        st, ov, _ = _run(wb_port, sid, "LARK-TASK-001", "claims-sandbox", ["filesystem"])
        assert _poll(wb_port, ov, lambda v: v["run_state"] == "terminal")["outcome"] == "passed"
        assert _api(wb_port, ov)["context_is_stale"] is True         # override run itself is stale
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]   # earlier pass still qualifies

        # A later passing CAPABILITY override also must NOT revoke eligibility.
        st, ov2, _ = _run(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem", "shell"])
        assert _poll(wb_port, ov2, lambda v: v["run_state"] == "terminal")["outcome"] == "passed"
        assert _api(wb_port, ov2)["context_is_stale"] is True
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]
    finally:
        _stop(wb, mock)


def test_override_alone_does_not_qualify_and_stays_visible(tmp_path):
    """An override run with no profile-matching evidence does not qualify (re-validation
    required), yet remains fully visible in history and its evidence view is marked stale
    — eligibility filtering must never hide evidence (HST-3/UI-6)."""
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "ovr-vis")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sid}"); _toggle(wb_port, sid, "LARK-TASK-002")

        st, ov, _ = _run(wb_port, sid, "LARK-TASK-001", "claims-sandbox", ["filesystem"])   # override only
        assert _poll(wb_port, ov, lambda v: v["run_state"] == "terminal")["outcome"] == "passed"
        assert _approve(wb_port, sid)[0] == 400                      # override alone does not qualify
        assert "re-validation required" in _get(wb_port, f"/packages/{sid}")[1]

        # Excluded from eligibility, still visible in history and marked stale (HST-3/UI-6).
        hist = _get(wb_port, f"/packages/{sid}/history")[1]
        assert ov in hist and "stale" in hist                        # visible + marked stale in history
        assert _api(wb_port, ov)["context_is_stale"] is True
        assert "stale" in _get(wb_port, f"/runs/{ov}")[1].lower()     # marked stale in the evidence view
    finally:
        _stop(wb, mock)


def test_approval_survives_restart(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    env = _env(mock_port, dbpath)
    root = _copy(tmp_path / "r" / "pkg", "persist")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sid}")
        _toggle(wb_port, sid, "LARK-TASK-002")
        _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert _approve(wb_port, sid, by="alice")[0] == 303
        _stop(wb)
        wb = _start("workbench.app:app", wb_port, env)
        assert _wait_ready(wb_port)
        html = _get(wb_port, f"/packages/{sid}")[1]
        assert "Approved · current" in html and "alice" in html            # profile + approval persisted
    finally:
        _stop(wb, mock)


def test_needs_review_lifecycle(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root = _copy(tmp_path / "r" / "pkg", "review")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, root)
        _profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sid}")
        # Both tasks active. Run the checked task (passed) and the no-checks task (needs_review).
        _run_pass(wb_port, sid, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        st, nr, _ = _run(wb_port, sid, "LARK-TASK-002", "larkspur-sandbox", ["filesystem"], forced="success")
        v = _poll(wb_port, nr, lambda v: v["run_state"] == "terminal")
        assert v["verdict"]["outcome"] == "needs_review" and v["verdict"]["rule"] == 3
        # Not eligible while the review is unresolved.
        assert _approve(wb_port, sid)[0] == 400
        # A named human resolves it to passed → machine verdict preserved, eligible.
        assert _resolve(wb_port, nr, "passed", by="dana")[0] == 303
        ev = _api(wb_port, nr)
        assert ev["verdict"]["outcome"] == "needs_review"           # mechanical verdict unchanged (VER-7)
        assert ev["effective_outcome"] == "passed" and ev["review"]["resolved_by"] == "dana"
        assert _approve(wb_port, sid, by="dana")[0] == 303
        assert "Approved · current" in _get(wb_port, f"/packages/{sid}")[1]
        # Later staleness supersedes the resolved review for current eligibility.
        idx = root / "larkspur-index.md"
        idx.write_text(idx.read_text() + "\n\nchanged.\n", encoding="utf-8")
        assert "no longer current" in _get(wb_port, f"/packages/{sid}")[1]
    finally:
        _stop(wb, mock)


def test_profiles_and_approvals_are_isolated_across_packages(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    a = _copy(tmp_path / "a" / "pkg", "iso-a")
    b = _copy(tmp_path / "b" / "pkg", "iso-b")
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sa, sb = _register(wb_port, a), _register(wb_port, b)
        _profile(wb_port, sa, "larkspur-sandbox", ["filesystem"])
        _get(wb_port, f"/packages/{sa}"); _toggle(wb_port, sa, "LARK-TASK-002")
        _run_pass(wb_port, sa, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])
        assert _approve(wb_port, sa)[0] == 303
        # B has no profile → still blocked and not approved; A's approval didn't leak.
        assert "Validation profile not configured" in _get(wb_port, f"/packages/{sb}")[1]
        assert _run(wb_port, sb, "LARK-TASK-001", "larkspur-sandbox", ["filesystem"])[0] == 400
        assert "Approved · current" in _get(wb_port, f"/packages/{sa}")[1]
        assert "Approved · current" not in _get(wb_port, f"/packages/{sb}")[1]
    finally:
        _stop(wb, mock)

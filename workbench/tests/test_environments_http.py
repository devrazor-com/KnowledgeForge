"""Configurable Module 3 target environments — end to end over real HTTP.

Covers the run-route gate (an unconfigured environment yields a local error, NO run row,
and ZERO physical POST /runs), the authoritative-read/TOCTOU rule (the state-changing POST
rereads the current file, not the list captured at render time), and the historical-vs-
future separation (removing an environment preserves stored evidence exactly while blocking
a new run that would use it).
"""
import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop

REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = str(REPO_ROOT / "workbench" / "examples" / "larkspur")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _env(mock_port, dbpath, envfile):
    e = os.environ.copy()
    e["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
    e["WORKBENCH_ENVIRONMENTS_FILE"] = str(envfile)   # our controlled config (overrides harness default)
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
    with urllib.request.urlopen(f"http://127.0.0.1:{wb_port}{path}", timeout=5) as r:
        return r.read().decode()


def _api(wb_port, run_id):
    return json.loads(_get(wb_port, f"/api/runs/{run_id}"))


def _register(wb_port):
    st, loc, _ = _post(wb_port, "/packages", [("root_path", LARKSPUR)])
    assert st == 303
    return loc.rsplit("/", 1)[-1]


def _profile(wb_port, sid, environment):
    return _post(wb_port, f"/packages/{sid}/profile",
                 [("environment", environment), ("capabilities", "filesystem"), ("configured_by", "op")])


def _run(wb_port, sid, environment, forced=None):
    fields = [("source_id", sid), ("task", "LARK-TASK-001"),
              ("environment", environment), ("capabilities", "filesystem")]
    if forced:
        fields.append(("forced_outcome", forced))
    st, loc, body = _post(wb_port, "/runs", fields)
    return st, (loc.rsplit("/", 1)[-1] if st == 303 and loc else None), body


def _mock_starts_total(mock_port):
    with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/", timeout=5) as r:
        return sum(json.loads(r.read().decode()).get("starts", {}).values())


def _db_run_count(dbpath):
    con = sqlite3.connect(dbpath)
    try:
        return con.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    finally:
        con.close()


def _poll(wb_port, run_id, until, timeout=30.0):
    end = time.time() + timeout
    v = _api(wb_port, run_id)
    while time.time() < end:
        if until(v):
            return v
        time.sleep(0.25)
        v = _api(wb_port, run_id)
    return v


def test_new_run_with_unconfigured_environment_makes_zero_physical_starts(tmp_path):
    """The run-route gate: a profile whose environment has been removed from config yields
    a local 400, NO run row, and ZERO physical POST /runs (proved by the mock start count)."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    envfile = tmp_path / "environments.txt"
    envfile.write_text("larkspur-sandbox\nclaims-sandbox\n", encoding="utf-8")
    env = _env(mock_port, dbpath, envfile)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port)
        assert _profile(wb_port, sid, "larkspur-sandbox")[0] == 303
        # Remove larkspur-sandbox from the CURRENT configuration (reread on demand).
        envfile.write_text("claims-sandbox\n", encoding="utf-8")

        st, run_id, body = _run(wb_port, sid, "larkspur-sandbox")
        assert st == 400 and run_id is None
        assert "not in the current configured list" in body
        # The decisive proof: no local run row, and zero physical Module 3 start requests.
        assert _db_run_count(dbpath) == 0
        assert _mock_starts_total(mock_port) == 0
    finally:
        _stop(wb, mock)


def test_profile_post_rereads_config_toctou(tmp_path):
    """Authoritative read: an environment present when the form was rendered but removed
    before the POST is rejected by the state-changing request."""
    mock_port, wb_port = _free_port(), _free_port()
    envfile = tmp_path / "environments.txt"
    envfile.write_text("idr\nlarkspur-sandbox\n", encoding="utf-8")
    env = _env(mock_port, tmp_path / "wb.db", envfile)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port)
        # Render-time list includes idr; then it is removed before the POST.
        assert "idr" in _get(wb_port, f"/packages/{sid}")
        envfile.write_text("larkspur-sandbox\n", encoding="utf-8")
        st, _, body = _profile(wb_port, sid, "idr")
        assert st == 400 and "not in the current configured list" in body
    finally:
        _stop(wb)


_PLACEHOLDER = '<option value="" disabled selected>Select an environment</option>'


def test_forms_require_explicit_choice_when_stored_env_unconfigured(tmp_path):
    """Rendered-HTML defect fix: when a profile's stored environment is no longer
    configured, BOTH the run form and the profile (reconfigure) form must render a
    disabled placeholder as the selected option — so `required` blocks submission — and
    must NOT auto-select a configured option, nor offer the obsolete env in the dropdown."""
    mock_port, wb_port = _free_port(), _free_port()
    envfile = tmp_path / "environments.txt"
    envfile.write_text("idr\nlarkspur-sandbox\n", encoding="utf-8")
    env = _env(mock_port, tmp_path / "wb.db", envfile)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port)
        assert _profile(wb_port, sid, "idr")[0] == 303       # stored env = idr
        envfile.write_text("larkspur-sandbox\n", encoding="utf-8")   # idr removed

        html = _get(wb_port, f"/packages/{sid}")
        assert html.count(_PLACEHOLDER) == 2                 # both env selects: run form + profile form
        assert '<option value="idr"' not in html             # obsolete env NOT offered in either select
        assert '<option value="larkspur-sandbox" selected' not in html   # no configured option auto-selected
        assert "no longer configured" in html and "idr" in html          # shown verbatim in the warning/context
    finally:
        _stop(wb)


def test_profile_form_requires_explicit_choice_when_no_profile_yet(tmp_path):
    """Fresh package, no profile: the profile form must render the placeholder selected and
    auto-select no configured option (a first-time Save must not silently persist the
    first configured environment)."""
    mock_port, wb_port = _free_port(), _free_port()
    envfile = tmp_path / "environments.txt"
    envfile.write_text("ifastbase\nwebplus\n", encoding="utf-8")
    env = _env(mock_port, tmp_path / "wb.db", envfile)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port)                              # no profile configured yet
        html = _get(wb_port, f"/packages/{sid}")
        assert _PLACEHOLDER in html                           # profile form placeholder selected
        assert '<option value="ifastbase" selected' not in html   # first configured NOT auto-selected
    finally:
        _stop(wb)


def test_stored_env_still_configured_stays_selected(tmp_path):
    """State 1: when the stored environment is still configured it remains the selected
    default and NO placeholder is rendered."""
    mock_port, wb_port = _free_port(), _free_port()
    envfile = tmp_path / "environments.txt"
    envfile.write_text("larkspur-sandbox\nclaims-sandbox\n", encoding="utf-8")
    env = _env(mock_port, tmp_path / "wb.db", envfile)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port)
        assert _profile(wb_port, sid, "larkspur-sandbox")[0] == 303
        html = _get(wb_port, f"/packages/{sid}")
        assert '<option value="larkspur-sandbox" selected' in html   # stored env selected
        assert _PLACEHOLDER not in html                              # no placeholder when a valid current exists
    finally:
        _stop(wb)


def test_empty_environment_post_creates_no_run_and_no_profile_change(tmp_path):
    """Server-side belt to the placeholder: an empty environment POST (as a crafted/scripted
    submission) is rejected and changes nothing — no run created, no profile overwritten."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    envfile = tmp_path / "environments.txt"
    envfile.write_text("idr\nlarkspur-sandbox\n", encoding="utf-8")
    env = _env(mock_port, dbpath, envfile)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port)
        assert _profile(wb_port, sid, "idr")[0] == 303
        envfile.write_text("larkspur-sandbox\n", encoding="utf-8")   # idr removed; stored profile still idr

        # Save profile with an empty environment -> rejected (422 empty required field),
        # stored env unchanged (still idr). Whichever 4xx, the guarantee is "changes nothing".
        assert _profile(wb_port, sid, "")[0] in (400, 422)
        assert "idr" in _get(wb_port, f"/packages/{sid}")            # profile NOT overwritten to a first configured value
        # Start a run with an empty environment -> rejected, no run row, zero physical starts.
        st, run_id, _ = _run(wb_port, sid, "")
        assert st in (400, 422) and run_id is None
        assert _db_run_count(dbpath) == 0 and _mock_starts_total(mock_port) == 0
    finally:
        _stop(wb, mock)


def test_removed_environment_preserves_history_but_blocks_new_run(tmp_path):
    """Configuration governs FUTURE selection only. A run recorded under `idr` stays
    readable exactly as stored (value, verdict, current/stale interpretation) after `idr`
    is removed; the profile shows it as no longer configured; a new run using it is blocked."""
    mock_port, wb_port = _free_port(), _free_port()
    dbpath = tmp_path / "wb.db"
    envfile = tmp_path / "environments.txt"
    envfile.write_text("idr\nlarkspur-sandbox\n", encoding="utf-8")
    env = _env(mock_port, dbpath, envfile)
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port)
        assert _profile(wb_port, sid, "idr")[0] == 303
        st, run_id, _ = _run(wb_port, sid, "idr", forced="success")
        assert st == 303
        v = _poll(wb_port, run_id, lambda v: v["run_state"] == "terminal")
        assert v["run_state"] == "terminal" and v["outcome"] == "passed"
        assert v["target_environment"] == "idr" and v.get("context_is_current") is True

        # Remove idr from CURRENT configuration.
        envfile.write_text("larkspur-sandbox\n", encoding="utf-8")

        # Historical evidence unchanged: stored value, verdict, and current/stale reading.
        v2 = _api(wb_port, run_id)
        assert v2["target_environment"] == "idr"
        assert v2["outcome"] == "passed" and v2.get("context_is_current") is True
        assert "idr" in _get(wb_port, f"/runs/{run_id}")            # run screen still shows it
        assert "idr" in _get(wb_port, f"/packages/{sid}/history")   # history still shows it

        # Future use blocked: profile marked no-longer-configured; a new idr run is refused.
        detail = _get(wb_port, f"/packages/{sid}")
        assert "idr" in detail and "no longer configured" in detail
        starts_before = _mock_starts_total(mock_port)
        st2, run2, body2 = _run(wb_port, sid, "idr")
        assert st2 == 400 and run2 is None
        assert _mock_starts_total(mock_port) == starts_before   # no new physical start
    finally:
        _stop(wb, mock)

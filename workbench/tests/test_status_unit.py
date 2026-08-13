"""Validation-context identity, staleness, and status logic (Step 3C-3), in-process.

Fast unit coverage for the extended validation context (package + task + capability
set + environment), capability-order canonicalisation, review-aware effective outcome,
and per-task/eligibility/approval-currency derivation.
"""

import json
import sqlite3
from types import SimpleNamespace

from workbench import db, status


def _task(tid, fp, active=True):
    return SimpleNamespace(id=tid, fingerprint=fp, active=active)


def _seed_run(dbpath, run_id, package_id, task_id, task_fp, pkg_fp, caps, env, outcome, rule=6):
    con = sqlite3.connect(dbpath)
    verdict = json.dumps({"outcome": outcome, "rule": rule, "reasoning": []})
    con.execute(
        "INSERT INTO run (run_id, package_name, package_fingerprint, task_id, task_fingerprint, "
        "capabilities_json, target_environment, package_id, request_json, request_validation_json, "
        "run_state, outcome, verdict_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "P", pkg_fp, task_id, task_fp, json.dumps(caps), env, package_id, "{}",
         '{"passed":true,"errors":[]}', "terminal", outcome, verdict, run_id))
    con.commit(); con.close()


# --- context identity ---------------------------------------------------------

def test_context_id_canonical_capability_order():
    a = status.validation_context_id("p", "t", ["filesystem", "network"], "env")
    b = status.validation_context_id("p", "t", ["network", "filesystem"], "env")
    assert a == b                                       # ordering alone never changes identity


def test_context_id_changes_on_each_input():
    base = status.validation_context_id("p", "t", ["fs"], "env")
    assert base != status.validation_context_id("p2", "t", ["fs"], "env")     # package
    assert base != status.validation_context_id("p", "t2", ["fs"], "env")     # task
    assert base != status.validation_context_id("p", "t", ["fs", "net"], "env")  # capability set
    assert base != status.validation_context_id("p", "t", ["fs"], "env2")     # environment


def test_effective_outcome_review_and_error():
    run_pass = {"run_state": "terminal", "verdict_json": json.dumps({"outcome": "passed", "rule": 6})}
    assert status.effective_outcome(run_pass, None) == ("passed", "machine")
    run_nr = {"run_state": "terminal", "verdict_json": json.dumps({"outcome": "needs_review", "rule": 3})}
    assert status.effective_outcome(run_nr, None) == ("needs_review", "machine")
    assert status.effective_outcome(run_nr, {"resolution": "passed"}) == ("passed", "review")
    run_err = {"run_state": "error", "error_kind": "timed_out"}
    assert status.effective_outcome(run_err, None) == ("inconclusive", "error")


# --- package_status: eligibility + currency -----------------------------------

def test_package_status_eligibility_and_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid, pkg_fp = "demo", "sha256:pkg1"
    tasks = [_task("T1", "tfp1"), _task("T2", "tfp2")]
    db.set_validation_profile(pid, "env-a", ["filesystem"], "victor")
    profile = db.get_validation_profile(pid)

    # Only T1 has a current passing run → not eligible (T2 has no run).
    _seed_run(dbpath, "r1", pid, "T1", "tfp1", pkg_fp, ["filesystem"], "env-a", "passed")
    st = status.package_status(pid, pkg_fp, tasks, profile)
    assert st["eligible"] is False and "1 of 2" in st["eligibility_reason"]

    # T2 gets a current passing run → eligible.
    _seed_run(dbpath, "r2", pid, "T2", "tfp2", pkg_fp, ["filesystem"], "env-a", "passed")
    st = status.package_status(pid, pkg_fp, tasks, profile)
    assert st["eligible"] is True and set(st["qualifying_runs"]) == {"r1", "r2"}

    # Environment change in the profile → both runs' context no longer current → not eligible.
    db.set_validation_profile(pid, "env-b", ["filesystem"], "victor")
    profile2 = db.get_validation_profile(pid)
    st = status.package_status(pid, pkg_fp, tasks, profile2)
    assert st["eligible"] is False                       # env change → no current-context run
    assert all(r["status"] == "revalidation_required" for r in st["task_rows"])

    # Capability reorder in the profile does NOT cause staleness.
    db.set_validation_profile(pid, "env-a", ["filesystem"], "victor")   # back to env-a
    st = status.package_status(pid, pkg_fp, tasks, db.get_validation_profile(pid))
    assert st["eligible"] is True


def test_apr1_keys_on_latest_conclusive_run(tmp_path, monkeypatch):
    """APR-1 keys on the latest CONCLUSIVE run: a later inconclusive/cancelled run
    (non-conclusive — VER-3) never erases a prior current passing conclusive run."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid, pkg_fp = "demo", "sha256:pkg1"
    db.set_validation_profile(pid, "env-a", ["filesystem"], None)
    profile = db.get_validation_profile(pid)
    tasks = [_task("T1", "tfp1")]

    def qualifies(sequence):
        con = sqlite3.connect(dbpath); con.execute("DELETE FROM run"); con.commit(); con.close()
        for i, oc in enumerate(sequence):   # r0 older, r1 newer (created_at == run_id)
            _seed_run(dbpath, f"r{i}", pid, "T1", "tfp1", pkg_fp, ["filesystem"], "env-a", oc)
        return status.package_status(pid, pkg_fp, tasks, profile)["eligible"]

    assert qualifies(["passed", "failed"]) is False
    assert qualifies(["passed", "inconclusive"]) is True     # inconclusive doesn't erase the pass
    assert qualifies(["passed", "cancelled"]) is True        # cancelled doesn't erase the pass
    assert qualifies(["failed", "passed"]) is True


def test_apr1_conclusive_run_must_still_be_current(tmp_path, monkeypatch):
    """The latest conclusive passing run only qualifies while its context is current."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid = "demo"
    db.set_validation_profile(pid, "env-a", ["filesystem"], None)
    tasks = [_task("T1", "tfp1")]
    _seed_run(dbpath, "r0", pid, "T1", "tfp1", "sha256:pkg1", ["filesystem"], "env-a", "passed")
    # passed, then inconclusive under the SAME context → still qualifies (conclusive pass).
    _seed_run(dbpath, "r1", pid, "T1", "tfp1", "sha256:pkg1", ["filesystem"], "env-a", "inconclusive")
    assert status.package_status(pid, "sha256:pkg1", tasks, db.get_validation_profile(pid))["eligible"] is True
    # But if the package content changed, the conclusive pass is stale → not eligible.
    assert status.package_status(pid, "sha256:pkg2", tasks, db.get_validation_profile(pid))["eligible"] is False


def test_current_context_candidate_selection(tmp_path, monkeypatch):
    """Only current-context runs are approval candidates; a later override (different
    env/caps) neither qualifies nor disqualifies. Within candidates newest-first:
    inconclusive/cancelled skip, unresolved needs_review blocks, resolved/passed/failed decide."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid, pkg_fp = "demo", "sha256:pkg1"
    db.set_validation_profile(pid, "env-a", ["filesystem"], None)
    profile = db.get_validation_profile(pid)
    tasks = [_task("T1", "tfp1")]

    def run(setup):
        con = sqlite3.connect(dbpath)
        con.execute("DELETE FROM run"); con.execute("DELETE FROM review_resolution")
        con.commit(); con.close()
        setup()
        return status.package_status(pid, pkg_fp, tasks, profile)

    def seed(rid, oc, caps=("filesystem",), env="env-a", review=None):
        _seed_run(dbpath, rid, pid, "T1", "tfp1", pkg_fp, list(caps), env, oc)
        if review:
            db.set_review_resolution(rid, "human", review)

    # An override (env or caps) must NOT disqualify an earlier current-context pass.
    st = run(lambda: (seed("r0", "passed"), seed("r1", "passed", env="env-b")))
    assert st["eligible"] and st["task_rows"][0]["status"] == "passing_current"
    assert run(lambda: (seed("r0", "passed"), seed("r1", "passed", caps=("filesystem", "shell"))))["eligible"]

    # Within current-context candidates: inconclusive/cancelled skip, needs_review governs.
    assert run(lambda: (seed("r0", "passed"), seed("r1", "inconclusive")))["eligible"]
    assert run(lambda: (seed("r0", "passed"), seed("r1", "cancelled")))["eligible"]
    st = run(lambda: (seed("r0", "passed"), seed("r1", "needs_review")))
    assert not st["eligible"] and st["task_rows"][0]["status"] == "needs_review"   # unresolved → blocks
    assert run(lambda: (seed("r0", "passed"), seed("r1", "needs_review", review="passed")))["eligible"]
    assert not run(lambda: (seed("r0", "passed"), seed("r1", "needs_review", review="failed")))["eligible"]


def test_not_validated_vs_revalidation_required(tmp_path, monkeypatch):
    """UI-4 distinguishes never-validated from previously-validated-but-context-changed."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid = "demo"
    db.set_validation_profile(pid, "env-a", ["filesystem"], None)
    profile = db.get_validation_profile(pid)
    tasks = [_task("T1", "tfp1")]
    # No runs at all → not_validated.
    assert status.package_status(pid, "sha256:pkg1", tasks, profile)["task_rows"][0]["status"] == "not_validated"
    # A run exists but the package fingerprint changed → no current-context candidate.
    _seed_run(dbpath, "r0", pid, "T1", "tfp1", "sha256:pkg1", ["filesystem"], "env-a", "passed")
    st = status.package_status(pid, "sha256:pkg2", tasks, profile)   # current fp differs
    assert st["task_rows"][0]["status"] == "revalidation_required" and st["eligible"] is False


def test_inactive_task_excluded_from_eligibility(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    dbpath = str(tmp_path / "db.db")
    pid, pkg_fp = "demo", "sha256:pkg1"
    db.set_validation_profile(pid, "env-a", ["filesystem"], None)
    profile = db.get_validation_profile(pid)
    tasks = [_task("T1", "tfp1", active=True), _task("T2", "tfp2", active=False)]
    _seed_run(dbpath, "r1", pid, "T1", "tfp1", pkg_fp, ["filesystem"], "env-a", "passed")
    st = status.package_status(pid, pkg_fp, tasks, profile)
    assert st["eligible"] is True and st["active_task_count"] == 1        # inactive T2 ignored

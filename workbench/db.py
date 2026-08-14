"""SQLite persistence.

Step 1 tables: immutable package snapshots + task records, mutable task_state.
Step 2A adds run and run_event: a run row that grows until terminal then is
frozen, and an append-only event log that powers live streaming and replay.

Runs are immutable once terminal. Resolution and approval tables arrive later.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from workbench import config
from workbench.models import Assembly, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS package_snapshot (
    package_fingerprint TEXT PRIMARY KEY,
    dir_name TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
    metadata_json TEXT NOT NULL, main_file TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_file (
    package_fingerprint TEXT NOT NULL, ordinal INTEGER NOT NULL,
    path TEXT NOT NULL, content TEXT NOT NULL,
    PRIMARY KEY (package_fingerprint, path)
);
CREATE TABLE IF NOT EXISTS task (
    task_fingerprint TEXT PRIMARY KEY, package_name TEXT NOT NULL, task_id TEXT NOT NULL,
    title TEXT NOT NULL, description TEXT NOT NULL, business_area TEXT, difficulty TEXT,
    acceptance_criteria TEXT, checks_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_state (
    package_name TEXT NOT NULL, task_id TEXT NOT NULL,
    active INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (package_name, task_id)
);
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    package_name TEXT NOT NULL, package_fingerprint TEXT NOT NULL,
    task_id TEXT NOT NULL, task_fingerprint TEXT NOT NULL,
    capabilities_json TEXT NOT NULL, target_environment TEXT NOT NULL,
    package_id TEXT,                -- durable logical identity of the package this run belongs to
    request_json TEXT NOT NULL, request_validation_json TEXT NOT NULL,
    run_state TEXT NOT NULL,
    gateway_ack_json TEXT, gateway_result_json TEXT, contract_status TEXT, outcome TEXT,
    verdict_json TEXT, result_json TEXT, result_validation_json TEXT,
    error TEXT, error_kind TEXT, error_payload_text TEXT,
    accepted_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, cancel_requested_at TEXT,
    cancel_delivery TEXT,
    created_at TEXT NOT NULL, terminal_at TEXT
);
CREATE TABLE IF NOT EXISTS run_event (
    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL,
    event_json TEXT NOT NULL, m1_validation_json TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS package_source (
    id TEXT PRIMARY KEY,           -- stable, URL-safe slug derived from the root path
    root_path TEXT NOT NULL UNIQUE, -- machine-local absolute path; NEVER in any fingerprint
    package_id TEXT UNIQUE,        -- durable logical identity captured at registration; UNIQUE
                                   -- enforces one ACTIVE source per package_id (NULLs allowed for
                                   -- an unhealthy source with no resolvable id)
    added_at TEXT NOT NULL
);
-- 3C-3: current MUTABLE operator configuration — the package's canonical validation
-- context (target environment + permitted capabilities). Keyed by durable package_id.
-- No profile => no run (enforced in the orchestrator). Excluded from all fingerprints.
CREATE TABLE IF NOT EXISTS validation_profile (
    package_id TEXT PRIMARY KEY,
    target_environment TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,   -- canonical (sorted) permitted capability set
    configured_by TEXT,
    configured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 3C-3: immutable human review resolution for a needs_review run (VER-6/VER-7).
-- Stored ALONGSIDE the machine verdict, never replacing it.
CREATE TABLE IF NOT EXISTS review_resolution (
    run_id TEXT PRIMARY KEY,
    resolved_by TEXT NOT NULL,
    resolution TEXT NOT NULL,           -- 'passed' | 'failed'
    resolved_at TEXT NOT NULL
);
-- 3C-3: immutable approval decision (APR-2/APR-3). Records who/when, the package
-- fingerprint, the set of active task fingerprints, and the profile context it was
-- granted against. Invalidation (APR-4) is DERIVED by comparing to current state,
-- never by mutating/erasing this row. The latest row for a package_id is current.
CREATE TABLE IF NOT EXISTS approval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    package_fingerprint TEXT NOT NULL,
    task_fingerprints_json TEXT NOT NULL,
    target_environment TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    qualifying_runs_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    """Sub-second, persistent wall-clock timestamp. Used as the deadline anchor
    (accepted_at) so that `accepted_at + timeout_seconds + guard` is exact — and
    remains correct across a Workbench restart, since it's a stored wall-clock
    value, not an in-memory monotonic reading."""
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.db_path(), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)


# --------------------------------------------------------------------------
# Packages and tasks (Step 1)
# --------------------------------------------------------------------------

def save_snapshot(assembly: Assembly) -> bool:
    pkg = assembly.package
    with _connect() as con:
        if con.execute("SELECT 1 FROM package_snapshot WHERE package_fingerprint=?",
                       (pkg.fingerprint,)).fetchone():
            return False
        con.execute(
            "INSERT INTO package_snapshot (package_fingerprint, dir_name, name, version, "
            "metadata_json, main_file, created_at) VALUES (?,?,?,?,?,?,?)",
            (pkg.fingerprint, assembly.dir_name, pkg.name, pkg.version,
             json.dumps(pkg.metadata), pkg.main_file, _now()))
        con.executemany(
            "INSERT INTO package_file (package_fingerprint, ordinal, path, content) VALUES (?,?,?,?)",
            [(pkg.fingerprint, i, f.path, f.content) for i, f in enumerate(pkg.files)])
        return True


def save_task(package_name: str, task: Task) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO task (task_fingerprint, package_name, task_id, title, "
            "description, business_area, difficulty, acceptance_criteria, checks_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (task.fingerprint, package_name, task.id, task.title, task.description,
             task.business_area, task.difficulty, task.acceptance_criteria, json.dumps(task.checks)))


# --------------------------------------------------------------------------
# Package sources (Step 3C-1) — the operator-registered package roots. Only the
# machine-local root path is stored here; it never participates in any fingerprint.
# --------------------------------------------------------------------------

def add_package_source(source_id: str, root_path: str, package_id: str | None = None) -> bool:
    """Register a package root under its durable `package_id`. Idempotent on the
    resolved path (UNIQUE). Returns True if newly added, False if that path was
    already registered. The caller must enforce the duplicate-package_id policy
    before calling (see get_package_source_by_package_id)."""
    with _connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO package_source (id, root_path, package_id, added_at) VALUES (?,?,?,?)",
            (source_id, root_path, package_id, _now_precise()))   # sub-second: stable list ordering
        return cur.rowcount > 0


def get_package_source_by_package_id(package_id: str) -> dict | None:
    """The ACTIVE source registered under a given durable package_id, if any (used to
    enforce the one-active-source-per-package_id policy)."""
    with _connect() as con:
        row = con.execute("SELECT * FROM package_source WHERE package_id=?", (package_id,)).fetchone()
    return dict(row) if row else None


def list_package_sources() -> list[dict]:
    with _connect() as con:
        rows = con.execute("SELECT * FROM package_source ORDER BY added_at, id").fetchall()
    return [dict(r) for r in rows]


def get_package_source(source_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM package_source WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else None


def get_package_source_by_path(root_path: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM package_source WHERE root_path=?", (root_path,)).fetchone()
    return dict(row) if row else None


def remove_package_source(source_id: str) -> bool:
    """Unregister a package source. Returns True if a row was removed. Registered
    package snapshots/runs are untouched — this only forgets the root registration."""
    with _connect() as con:
        cur = con.execute("DELETE FROM package_source WHERE id=?", (source_id,))
        return cur.rowcount > 0


def update_package_source_root(source_id: str, root_path: str) -> bool:
    """Repoint an existing registration at a new machine-local root (the "Change
    root" operator action). ONLY the mutable location moves: the row's `id`,
    durable `package_id`, and `added_at` are preserved, and nothing keyed off
    `package_id` (validation profile, run history, snapshots, review/approval) is
    touched — the caller has already verified the new root's manifest declares the
    SAME package_id. Returns True if a row was updated. Raises sqlite3.IntegrityError
    if the new path already belongs to a different registration (UNIQUE)."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE package_source SET root_path=? WHERE id=?", (root_path, source_id))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Validation profile / review / approval (Step 3C-3)
# --------------------------------------------------------------------------

def set_validation_profile(package_id: str, target_environment: str,
                           capabilities: list[str], configured_by: str | None) -> None:
    """Create or update a package's validation profile (mutable operator config).
    Capabilities are stored canonicalised (sorted, de-duplicated)."""
    caps = json.dumps(sorted(set(capabilities)))
    now = _now()
    with _connect() as con:
        con.execute(
            "INSERT INTO validation_profile (package_id, target_environment, capabilities_json, "
            "configured_by, configured_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(package_id) DO UPDATE SET target_environment=excluded.target_environment, "
            "capabilities_json=excluded.capabilities_json, configured_by=excluded.configured_by, "
            "updated_at=excluded.updated_at",
            (package_id, target_environment, caps, configured_by, now, now))


def get_validation_profile(package_id: str | None) -> dict | None:
    if not package_id:
        return None
    with _connect() as con:
        row = con.execute("SELECT * FROM validation_profile WHERE package_id=?", (package_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["capabilities"] = json.loads(d["capabilities_json"])
    return d


def set_review_resolution(run_id: str, resolved_by: str, resolution: str) -> None:
    """Record (or replace) a human resolution of a needs_review run. Stored alongside
    the machine verdict; the mechanical verdict is never overwritten."""
    with _connect() as con:
        con.execute(
            "INSERT INTO review_resolution (run_id, resolved_by, resolution, resolved_at) VALUES (?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET resolved_by=excluded.resolved_by, "
            "resolution=excluded.resolution, resolved_at=excluded.resolved_at",
            (run_id, resolved_by, resolution, _now()))


def get_review_resolution(run_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM review_resolution WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def reviews_for_runs(run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    with _connect() as con:
        rows = con.execute(
            f"SELECT * FROM review_resolution WHERE run_id IN ({','.join('?' * len(run_ids))})",
            run_ids).fetchall()
    return {r["run_id"]: dict(r) for r in rows}


def add_approval(package_id: str, approved_by: str, package_fingerprint: str,
                 task_fingerprints: list[str], target_environment: str,
                 capabilities: list[str], qualifying_runs: list[str]) -> None:
    """Append an immutable approval decision (APR-2/APR-3). Never updates a prior row."""
    with _connect() as con:
        con.execute(
            "INSERT INTO approval (package_id, approved_by, approved_at, package_fingerprint, "
            "task_fingerprints_json, target_environment, capabilities_json, qualifying_runs_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (package_id, approved_by, _now(), package_fingerprint,
             json.dumps(sorted(task_fingerprints)), target_environment,
             json.dumps(sorted(set(capabilities))), json.dumps(qualifying_runs)))


def latest_approval(package_id: str | None) -> dict | None:
    if not package_id:
        return None
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM approval WHERE package_id=? ORDER BY id DESC LIMIT 1", (package_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["task_fingerprints"] = json.loads(d["task_fingerprints_json"])
    d["capabilities"] = json.loads(d["capabilities_json"])
    d["qualifying_runs"] = json.loads(d["qualifying_runs_json"])
    return d


def get_active(package_name: str, task_id: str) -> bool:
    with _connect() as con:
        row = con.execute("SELECT active FROM task_state WHERE package_name=? AND task_id=?",
                          (package_name, task_id)).fetchone()
    return config.DEFAULT_TASK_ACTIVE if row is None else bool(row["active"])


def set_active(package_name: str, task_id: str, active: bool) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO task_state (package_name, task_id, active, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(package_name, task_id) DO UPDATE SET active=excluded.active, "
            "updated_at=excluded.updated_at",
            (package_name, task_id, 1 if active else 0, _now()))


# --------------------------------------------------------------------------
# Runs (Step 2A)
# --------------------------------------------------------------------------

def create_run(run: dict) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO run (run_id, package_name, package_fingerprint, task_id, task_fingerprint, "
            "capabilities_json, target_environment, package_id, request_json, request_validation_json, "
            "run_state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["run_id"], run["package_name"], run["package_fingerprint"], run["task_id"],
             run["task_fingerprint"], json.dumps(run["capabilities"]), run["target_environment"],
             run.get("package_id"), json.dumps(run["request"]), json.dumps(run["request_validation"]),
             run["run_state"], _now()))


def set_run_running(run_id: str, gateway_ack: dict | None) -> None:
    """Record Gateway acceptance. accepted_at anchors the run deadline
    (accepted_at + timeout_seconds + guard)."""
    with _connect() as con:
        con.execute("UPDATE run SET run_state='running', gateway_ack_json=?, accepted_at=? WHERE run_id=?",
                    (json.dumps(gateway_ack) if gateway_ack is not None else None, _now_precise(), run_id))


def set_cancel_requested(run_id: str) -> None:
    """Record OPERATOR cancellation intent (never the post-timeout cleanup cancel).
    Delivery knowledge is tracked separately by set_cancel_delivery()."""
    with _connect() as con:
        con.execute("UPDATE run SET cancel_requested=1, cancel_requested_at=? WHERE run_id=?",
                    (_now(), run_id))


def set_cancel_delivery(run_id: str, state: str) -> None:
    """Record what Module 1 knows about the delivery of the operator's cancellation
    request. `state` is one of unknown | undelivered | rejected | acknowledged.

    STICKY 'acknowledged': once the Gateway has acknowledged at least one cancel
    request, a later failed/rejected/unknown attempt must NOT erase that durable
    fact. Only 'acknowledged' is sticky; unknown/undelivered/rejected move freely as
    new explicit attempts provide fresh evidence. Enforced in the WHERE clause so
    the rule holds regardless of caller ordering."""
    with _connect() as con:
        con.execute(
            "UPDATE run SET cancel_delivery=? WHERE run_id=? "
            "AND (cancel_delivery IS NULL OR cancel_delivery != 'acknowledged' OR ?='acknowledged')",
            (state, run_id, state))


def set_run_error(run_id: str, error_kind: str, detail: str, payload_text: str | None = None) -> bool:
    """Record a run that reached a terminal Workbench error state — no valid
    ValidationResult was obtained. `error_kind` is Module-1-authored (not part of
    the contract); `payload_text` holds the raw offending response as text (a
    malformed body is not JSON). result_json is deliberately never set here.

    WRITE-ONCE: only writes if the run is not already terminal, so a timeout can
    never be overwritten by a late Gateway cancelled (or vice versa). Returns True
    if it actually wrote."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE run SET run_state='error', error_kind=?, error=?, error_payload_text=?, "
            "terminal_at=? WHERE run_id=? AND run_state NOT IN ('terminal','error')",
            (error_kind, detail, payload_text, _now(), run_id))
        return cur.rowcount > 0


def last_sequence(run_id: str) -> int:
    with _connect() as con:
        row = con.execute("SELECT MAX(sequence) AS s FROM run_event WHERE run_id=?", (run_id,)).fetchone()
    return int(row["s"]) if row and row["s"] is not None else 0


def append_event(run_id: str, event: dict, m1_validation: dict) -> bool:
    """Append one received event. Idempotent on (run_id, sequence)."""
    with _connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO run_event (run_id, sequence, timestamp, event_type, message, "
            "event_json, m1_validation_json, received_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, event["sequence"], event["timestamp"], event["event_type"], event["message"],
             json.dumps(event), json.dumps(m1_validation), _now()))
        return cur.rowcount > 0


def finalize_run(run_id: str, result: dict, result_validation: dict, verdict: dict,
                 gateway_result: dict | None = None) -> bool:
    """Freeze a run as terminal with a real ValidationResult. WRITE-ONCE (see
    set_run_error) — returns True only if it actually wrote, so it cannot overwrite
    an already-terminal run (e.g. one already recorded as timed_out).

    `gateway_result` is the mock's out-of-band result envelope (its own validation
    signal), stored for display and clearly labelled as mock-only in the UI;
    Module 1 never treats its absence as a failure."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE run SET run_state='terminal', contract_status=?, outcome=?, verdict_json=?, "
            "result_json=?, result_validation_json=?, gateway_result_json=?, terminal_at=? "
            "WHERE run_id=? AND run_state NOT IN ('terminal','error')",
            (result.get("status"), verdict.get("outcome"), json.dumps(verdict),
             json.dumps(result), json.dumps(result_validation),
             json.dumps(gateway_result) if gateway_result is not None else None, _now(), run_id))
        return cur.rowcount > 0


def get_run(run_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_events(run_id: str, since: int = 0) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT sequence, event_json, m1_validation_json FROM run_event "
            "WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, since)).fetchall()
    return [{"event": json.loads(r["event_json"]),
             "m1_validation": json.loads(r["m1_validation_json"])} for r in rows]


# --------------------------------------------------------------------------
# Restart recovery (Step 3B-1) — read-only reconciliation helpers, no new columns
# --------------------------------------------------------------------------

def runs_in_state(states: tuple[str, ...]) -> list[dict]:
    """All runs whose run_state is one of `states` (used at startup to find
    non-terminal local attempts to reconcile)."""
    with _connect() as con:
        rows = con.execute(
            f"SELECT * FROM run WHERE run_state IN ({','.join('?' * len(states))}) ORDER BY created_at",
            states).fetchall()
    return [dict(r) for r in rows]


def runs_for_package(package_id: str) -> list[dict]:
    """All runs belonging to a durable logical package, newest-first (HST-1). Keyed by
    the run's persisted package_id, so it spans every package_fingerprint the package
    has had over time and is independent of the mutable source registration."""
    with _connect() as con:
        rows = con.execute(
            "SELECT run_id, task_id, task_fingerprint, package_id, package_fingerprint, "
            "target_environment, capabilities_json, run_state, outcome, contract_status, "
            "verdict_json, error_kind, cancel_requested, cancel_requested_at, cancel_delivery, "
            "created_at, terminal_at "
            "FROM run WHERE package_id=? ORDER BY created_at DESC, run_id DESC", (package_id,)).fetchall()
    return [dict(r) for r in rows]


def last_event_type(run_id: str) -> str | None:
    """The event_type of the highest-sequence persisted event, or None if there
    are no events yet."""
    with _connect() as con:
        row = con.execute(
            "SELECT event_type FROM run_event WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
            (run_id,)).fetchone()
    return row["event_type"] if row else None

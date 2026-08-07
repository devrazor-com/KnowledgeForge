"""SQLite foundation — the Step 1 slice of persistence.

Tables here cover immutable package snapshots (PKG-8), immutable task records,
and mutable per-task active state (TSK-5). Run, event, resolution, and approval
tables arrive in later steps; they are deliberately absent now.

A run is immutable once terminal — that rule shapes the later schema, not this one,
but the principle starts here: snapshots and task records are write-once, keyed by
their fingerprint.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from workbench.config import DATA_DIR, DB_PATH, DEFAULT_TASK_ACTIVE
from workbench.models import Assembly, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS package_snapshot (
    package_fingerprint TEXT PRIMARY KEY,
    dir_name            TEXT NOT NULL,
    name                TEXT NOT NULL,
    version             TEXT NOT NULL,
    metadata_json       TEXT NOT NULL,
    main_file           TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_file (
    package_fingerprint TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    path                TEXT NOT NULL,
    content             TEXT NOT NULL,
    PRIMARY KEY (package_fingerprint, path)
);
CREATE TABLE IF NOT EXISTS task (
    task_fingerprint    TEXT PRIMARY KEY,
    package_name        TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    business_area       TEXT,
    difficulty          TEXT,
    acceptance_criteria TEXT,
    checks_json         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_state (
    package_name TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    active       INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (package_name, task_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.executescript(_SCHEMA)


def save_snapshot(assembly: Assembly) -> bool:
    """Store an assembled snapshot immutably. Returns True if newly stored, False
    if this exact fingerprint was already on record (identical input, identical id)."""
    pkg = assembly.package
    with _connect() as con:
        exists = con.execute(
            "SELECT 1 FROM package_snapshot WHERE package_fingerprint = ?",
            (pkg.fingerprint,),
        ).fetchone()
        if exists:
            return False
        con.execute(
            "INSERT INTO package_snapshot "
            "(package_fingerprint, dir_name, name, version, metadata_json, main_file, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pkg.fingerprint, assembly.dir_name, pkg.name, pkg.version,
             json.dumps(pkg.metadata), pkg.main_file, _now()),
        )
        con.executemany(
            "INSERT INTO package_file (package_fingerprint, ordinal, path, content) VALUES (?,?,?,?)",
            [(pkg.fingerprint, i, f.path, f.content) for i, f in enumerate(pkg.files)],
        )
        return True


def save_task(package_name: str, task: Task) -> None:
    """Record a task immutably by fingerprint (INSERT OR IGNORE — same content,
    same identity)."""
    with _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO task "
            "(task_fingerprint, package_name, task_id, title, description, "
            " business_area, difficulty, acceptance_criteria, checks_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (task.fingerprint, package_name, task.id, task.title, task.description,
             task.business_area, task.difficulty, task.acceptance_criteria,
             json.dumps(task.checks)),
        )


def get_active(package_name: str, task_id: str) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT active FROM task_state WHERE package_name = ? AND task_id = ?",
            (package_name, task_id),
        ).fetchone()
    if row is None:
        return DEFAULT_TASK_ACTIVE
    return bool(row["active"])


def set_active(package_name: str, task_id: str, active: bool) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO task_state (package_name, task_id, active, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(package_name, task_id) DO UPDATE SET active = excluded.active, "
            "updated_at = excluded.updated_at",
            (package_name, task_id, 1 if active else 0, _now()),
        )


def snapshot_count(package_fingerprint: str) -> int:
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM package_snapshot WHERE package_fingerprint = ?",
            (package_fingerprint,),
        ).fetchone()
    return int(row["n"])

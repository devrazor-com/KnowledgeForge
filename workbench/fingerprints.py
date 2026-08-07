"""Fingerprints — the load-bearing, machine-independent identities of a package
and a task. Reused verbatim from the proven POC implementation (poc/KFPOCMod1.py),
because every staleness and approval decision rests on this being deterministic:
identical input must produce an identical value on any machine, on Windows or
macOS alike.

Determinism guarantees:
  * content line endings are normalised to \\n before hashing;
  * paths are relative and POSIX ('/'), so a Windows checkout hashes identically;
  * the byte layout below is fixed and independent of dict ordering or locale.
"""

from __future__ import annotations

import hashlib


def normalize_content(text: str) -> str:
    """Normalise line endings to \\n before hashing or sending."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def package_fingerprint(ordered_files: list[dict]) -> str:
    """sha256 over the ordered sequence of (relative path, normalised content).

    `ordered_files` is a list of {"path": str, "content": str} in the package's
    deterministic order. Changing any file's content, or the set/order of files,
    changes the value (PKG-7).
    """
    h = hashlib.sha256()
    for f in ordered_files:
        h.update(f["path"].encode("utf-8"))
        h.update(b"\n")
        h.update(f["content"].encode("utf-8"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def task_fingerprint(task: dict) -> str:
    """sha256 over the task's id, description, acceptance_criteria and checks (TSK-4).

    Deliberately excludes title, business_area, difficulty, metadata, and the
    active flag — none of those change what the task actually asks for.
    """
    h = hashlib.sha256()

    def upd(s: str) -> None:
        h.update(s.encode("utf-8"))
        h.update(b"\n")

    upd(task["id"])
    upd(task["description"])
    upd(task.get("acceptance_criteria") or "")
    for chk in task.get("checks", []) or []:
        upd(chk["id"])
        upd(chk["description"])
        upd(chk["command"])
    return "sha256:" + h.hexdigest()

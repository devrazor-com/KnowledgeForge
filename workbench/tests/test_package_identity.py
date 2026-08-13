"""Durable package identity (Step 3C-2), in-process.

package_id is a mandatory, route-safe, durable logical identity with NO fallback.
It is orthogonal to package_fingerprint (content) and source registration (location).
"""

from pathlib import Path

import pytest

from workbench import db
from workbench.packages import (
    MANIFEST_NAME, PackageError, assemble, catalog_status, load_source, read_manifest,
)


def _pkg(root: Path, pid: str = "test-pkg", body: str = "# Root\n", extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(
        f"package_id: {pid}\nentry_point: idx.md\ntasks: tasks/\n{extra}", encoding="utf-8")
    (root / "idx.md").write_text(body, encoding="utf-8")
    return root


def _src(root: Path, registered_pid: str | None) -> dict:
    return {"id": "k", "root_path": str(root), "added_at": "t", "package_id": registered_pid}


def test_package_id_is_mandatory_with_no_fallback(tmp_path):
    """A manifest name and entry-point front-matter name must NOT stand in for a
    missing package_id — identity is never inferred from mutable display metadata."""
    root = tmp_path / "p"
    root.mkdir()
    (root / MANIFEST_NAME).write_text("entry_point: idx.md\ntasks: tasks/\nname: Fancy Name\n", encoding="utf-8")
    (root / "idx.md").write_text("---\nname: Front Name\n---\n# body\n", encoding="utf-8")
    with pytest.raises(PackageError):
        read_manifest(root)
    with pytest.raises(PackageError):
        assemble(root, "k")
    src = _src(root, None)
    assert catalog_status(src)["status"] == "unusable"
    assert catalog_status(src)["package_id"] is None        # never inferred from name/front-matter/folder
    assert load_source(src)["health"] == "unloadable"


@pytest.mark.parametrize("pid,ok", [
    ("larkspur", True), ("claims-adjudication", True), ("a1", True), ("x2-y3", True),
    ("Bad", False), ("has_underscore", False), ("trailing-", False), ("-leading", False),
    ("sp ace", False), ("dots.here", False), ("", False),
])
def test_package_id_syntax_is_enforced(tmp_path, pid, ok):
    root = tmp_path / "p"
    root.mkdir(exist_ok=True)
    (root / MANIFEST_NAME).write_text(f"package_id: '{pid}'\nentry_point: idx.md\ntasks: tasks/\n", encoding="utf-8")
    (root / "idx.md").write_text("# x\n", encoding="utf-8")
    if ok:
        assert read_manifest(root).package_id == pid
    else:
        with pytest.raises(PackageError):
            read_manifest(root)


def test_identity_mismatch_is_unhealthy_not_silently_adopted(tmp_path):
    """Registered as one id, manifest now declares another → unusable/unloadable with
    a clear reason. The live id is never silently adopted."""
    root = _pkg(tmp_path / "p", pid="alpha")
    src = _src(root, "beta")                                  # registered beta, manifest says alpha
    cat = catalog_status(src)
    assert cat["status"] == "unusable" and "changed since registration" in cat["detail"]
    det = load_source(src)
    assert det["health"] == "unloadable" and "changed since registration" in det["detail"]


def test_package_id_and_fingerprint_are_independent(tmp_path):
    # Same id, different content → different fingerprint, same identity.
    a = _pkg(tmp_path / "a", pid="same-id", body="# One\n")
    b = _pkg(tmp_path / "b", pid="same-id", body="# Two\n")
    aa, bb = assemble(a, "sa"), assemble(b, "sb")
    assert aa.package_id == bb.package_id == "same-id"
    assert aa.package.fingerprint != bb.package.fingerprint


def test_snapshot_is_content_addressed_not_bound_to_package_id(tmp_path, monkeypatch):
    """Two DIFFERENT package identities with IDENTICAL knowledge share one
    content-addressed snapshot — the snapshot store is neutral to package_id."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "db.db"))
    db.init()
    a = assemble(_pkg(tmp_path / "a", pid="alpha", body="# Same knowledge\n"), "sa")
    b = assemble(_pkg(tmp_path / "b", pid="beta", body="# Same knowledge\n"), "sb")
    assert a.package.fingerprint == b.package.fingerprint     # identical content
    assert db.save_snapshot(a) is True                        # stored once...
    assert db.save_snapshot(b) is False                       # ...reused, not re-bound to 'beta'

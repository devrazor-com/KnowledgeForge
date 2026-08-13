"""Package registry, minimal manifest, health, and fingerprint semantics (Step 3C-1).

In-process (no HTTP): the manifest format, source-id determinism, health states for
broken roots, fingerprint independence from the absolute root, exclusion of the
manifest from the assembled knowledge, and that the two structurally-different
example packages both load through the same loader.
"""

import shutil
from pathlib import Path

import pytest

from workbench import config, db
from workbench.packages import (
    MANIFEST_NAME, PackageError, assemble, catalog_status, load_source,
    normalize_root, read_manifest, source_id,
)
from workbench.tasks import load_tasks

LARKSPUR = config.PACKAGES_DIR / "larkspur"
CLAIMS = config.PACKAGES_DIR / "claims"


def _src(root: Path) -> dict:
    return {"id": source_id(str(root)), "root_path": str(root), "added_at": "2026-08-13T00:00:00Z"}


def _pkg(root: Path, entry: str = "index.md", tasks: str = "tasks/", body: str = "# Root\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(f"entry_point: {entry}\ntasks: {tasks}\n", encoding="utf-8")
    (root / entry).write_text(body, encoding="utf-8")
    return root


# --- manifest -----------------------------------------------------------------

def test_manifest_requires_entry_point_and_tasks(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("tasks: tasks/\n", encoding="utf-8")
    with pytest.raises(PackageError):
        read_manifest(tmp_path)
    (tmp_path / MANIFEST_NAME).write_text("entry_point: x.md\n", encoding="utf-8")
    with pytest.raises(PackageError):
        read_manifest(tmp_path)


def test_manifest_optional_identity_and_valid(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(
        "entry_point: idx.md\ntasks: t/\nname: Demo\nversion: '3'\n", encoding="utf-8")
    m = read_manifest(tmp_path)
    assert m.entry_point == "idx.md" and m.tasks == "t/" and m.name == "Demo" and m.version == "3"


def test_missing_manifest_raises(tmp_path):
    (tmp_path / "index.md").write_text("# x\n", encoding="utf-8")
    with pytest.raises(PackageError):
        read_manifest(tmp_path)


# --- source id / normalisation ------------------------------------------------

def test_source_id_is_deterministic_and_url_safe(tmp_path):
    a = source_id(str(tmp_path))
    b = source_id(str(tmp_path) + "/")     # normalisation collapses the trailing slash
    assert a == b
    assert all(c.isalnum() or c == "-" for c in a)


def test_normalize_root_resolves(tmp_path):
    sub = tmp_path / "a" / ".." / "a"
    (tmp_path / "a").mkdir()
    assert normalize_root(str(sub)) == str((tmp_path / "a").resolve())


# --- health -------------------------------------------------------------------

def test_health_healthy_for_both_examples():
    for root in (LARKSPUR, CLAIMS):
        v = load_source(_src(root))
        assert v["health"] == "healthy", (root, v["detail"])
        assert v["file_count"] >= 3 and v["task_count"] >= 1 and v["fingerprint"]


def test_health_unloadable_states(tmp_path):
    missing = tmp_path / "gone"
    assert load_source({"id": "x", "root_path": str(missing), "added_at": "t"})["health"] == "unloadable"

    afile = tmp_path / "afile"; afile.write_text("x", encoding="utf-8")
    assert load_source({"id": "x", "root_path": str(afile), "added_at": "t"})["health"] == "unloadable"

    nomani = tmp_path / "nomani"; nomani.mkdir(); (nomani / "index.md").write_text("# x\n", encoding="utf-8")
    v = load_source(_src(nomani))
    assert v["health"] == "unloadable" and MANIFEST_NAME in v["detail"]

    badentry = _pkg(tmp_path / "badentry", entry="index.md")
    (badentry / MANIFEST_NAME).write_text("entry_point: nope.md\ntasks: tasks/\n", encoding="utf-8")
    assert load_source(_src(badentry))["health"] == "unloadable"


def test_health_problems_when_link_broken(tmp_path):
    root = _pkg(tmp_path / "p", body="# Root\n[missing](gone.md)\n")
    v = load_source(_src(root))
    assert v["health"] == "problems" and v["problem_count"] == 1
    assert v["assembly"] is not None          # still assembled and visible


# --- fingerprint semantics ----------------------------------------------------

def test_fingerprint_independent_of_absolute_root(tmp_path):
    a = tmp_path / "one" / "larkspur"
    b = tmp_path / "two" / "deeper" / "larkspur"
    shutil.copytree(LARKSPUR, a)
    shutil.copytree(LARKSPUR, b)
    fa = assemble(a, source_id(str(a))).package.fingerprint
    fb = assemble(b, source_id(str(b))).package.fingerprint
    assert fa == fb == "sha256:ab62f181e48dcb0d1cff0a3cdeb606ed33308dd6ef08aa5b5312fcdb62ea6ac9"


def test_manifest_excluded_from_assembled_files():
    for root in (LARKSPUR, CLAIMS):
        a = assemble(root, source_id(str(root)))
        assert all(f.path != MANIFEST_NAME for f in a.package.files)
        assert all(not f.path.endswith(MANIFEST_NAME) for f in a.package.files)


def test_adding_manifest_does_not_change_fingerprint(tmp_path):
    """A package with the same knowledge files at the same relative paths hashes the
    same whether or not a manifest sits beside them (manifest is excluded)."""
    root = tmp_path / "pkg"; root.mkdir()
    (root / "idx.md").write_text("# Root\n[c](child.md)\n", encoding="utf-8")
    (root / "child.md").write_text("# Child\n", encoding="utf-8")
    (root / MANIFEST_NAME).write_text("entry_point: idx.md\ntasks: tasks/\n", encoding="utf-8")
    fp1 = assemble(root, "k").package.fingerprint
    # Add an unrelated extra key to the manifest; knowledge is unchanged.
    (root / MANIFEST_NAME).write_text("entry_point: idx.md\ntasks: other/\nname: X\n", encoding="utf-8")
    fp2 = assemble(root, "k").package.fingerprint
    assert fp1 == fp2


# --- generalization: two different shapes, one loader -------------------------

def test_two_packages_load_through_the_same_loader():
    la = assemble(LARKSPUR, source_id(str(LARKSPUR)))
    cl = assemble(CLAIMS, source_id(str(CLAIMS)))
    # Structurally different: entry filename and tasks directory differ.
    assert la.entry_point == "larkspur-index.md" and la.tasks_rel == "tasks"
    assert cl.entry_point == "claims-overview.md" and cl.tasks_rel == "validation"
    # Both assemble cleanly and load tasks, with distinct fingerprints.
    assert not la.problems and not cl.problems
    assert la.package.fingerprint != cl.package.fingerprint
    assert load_tasks(LARKSPUR / la.tasks_rel) and load_tasks(CLAIMS / cl.tasks_rel)


# --- cheap catalog status vs full detail assembly ----------------------------

def test_catalog_status_is_cheap_and_does_not_assemble(tmp_path):
    """A package whose entry links to a MISSING file is still catalog-'ok' (its root/
    manifest/entry resolve). The catalog does NOT traverse links, assemble, or
    fingerprint — full assembly on the detail path is what surfaces the broken link."""
    root = _pkg(tmp_path / "p", body="# Root\n[missing](gone.md)\n")
    cat = catalog_status(_src(root))
    assert cat["status"] == "ok"
    assert "fingerprint" not in cat            # no assembly happened at catalog time
    full = load_source(_src(root))
    assert full["health"] == "problems"        # detail traversal found the broken link


def test_catalog_status_unusable_reasons(tmp_path):
    assert catalog_status({"id": "x", "root_path": str(tmp_path / "nope"), "added_at": "t"})["status"] == "unusable"
    nomani = tmp_path / "nm"; nomani.mkdir(); (nomani / "i.md").write_text("# x\n", encoding="utf-8")
    assert catalog_status(_src(nomani))["status"] == "unusable"
    badentry = _pkg(tmp_path / "be")
    (badentry / MANIFEST_NAME).write_text("entry_point: no.md\ntasks: t/\n", encoding="utf-8")
    assert catalog_status(_src(badentry))["status"] == "unusable"


def test_catalog_status_matches_examples():
    for root in (LARKSPUR, CLAIMS):
        c = catalog_status(_src(root))
        assert c["status"] == "ok" and c["name"] in ("Larkspur", "Claims Adjudication")


# --- registry is a real collection: scales, accumulates, removes cleanly ------

def test_registry_scales_and_removes_without_tiny_catalog_assumptions(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "reg.db"))
    db.init()
    ids = [f"pkg-{i:03d}" for i in range(50)]
    for i, sid in enumerate(ids):
        assert db.add_package_source(sid, str(tmp_path / f"root{i}"))     # each is a distinct row
    rows = db.list_package_sources()
    assert len(rows) == 50 and {r["id"] for r in rows} == set(ids)         # all accumulate, none replaced
    assert db.add_package_source("pkg-000", str(tmp_path / "root0")) is False   # idempotent on path
    assert db.remove_package_source("pkg-025") is True
    rows2 = db.list_package_sources()
    assert len(rows2) == 49 and "pkg-025" not in {r["id"] for r in rows2}
    assert db.get_package_source("pkg-000") and db.get_package_source("pkg-049")  # neighbours intact
    assert db.remove_package_source("pkg-025") is False                    # already gone


def test_claims_uses_relative_links_not_front_matter_deps():
    """The claims package deliberately declares no front-matter `dependencies`; all
    dependents are reached by ordinary relative links (recursively, deduped)."""
    from workbench.packages import parse_front_matter
    meta, _ = parse_front_matter((CLAIMS / "claims-overview.md").read_text(encoding="utf-8"))
    assert "dependencies" not in meta
    a = assemble(CLAIMS, source_id(str(CLAIMS)))
    assert {f.path for f in a.package.files} == {
        "claims-overview.md", "domain/claim-model.md",
        "architecture/adjudication-pipeline.md", "procedures/adjudication-rules.md"}

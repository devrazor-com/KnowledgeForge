"""Package discovery edge cases — PKG-3/4/5/6.

Missing links and cycles are reported (not skipped); paths outside the root are
refused; the ordered file list is deterministic; front-matter dependencies are
followed as well as relative links.
"""

from pathlib import Path

from workbench import config
from workbench.packages import assemble

LARKSPUR = config.PACKAGES_DIR / "larkspur"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _manifest(root: Path, entry: str = "index.md") -> None:
    """Minimal package.yaml for an inline temp package (tasks dir need not exist)."""
    _write(root, "package.yaml", f"package_id: temp-pkg\nentry_point: {entry}\ntasks: tasks/\n")


def test_order_is_main_first_then_sorted():
    a = assemble(LARKSPUR, "larkspur")
    assert a.ordered_paths == [
        "larkspur-index.md",
        "data/account-model.md",
        "data/billing-ledger.md",
        "rules/plan-rules.md",
    ]


def test_front_matter_dependency_is_discovered():
    # plan-rules.md is reached via the front-matter `dependencies:` list.
    a = assemble(LARKSPUR, "larkspur")
    assert "rules/plan-rules.md" in a.ordered_paths
    assert not a.problems


def test_reading_metadata_from_front_matter():
    a = assemble(LARKSPUR, "larkspur")
    assert a.package.name == "Larkspur"
    assert a.package.version == "1.4"
    assert a.package.metadata.get("domain") == "subscription-billing"


def test_missing_link_is_reported_and_assembly_continues(tmp_path):
    _manifest(tmp_path)
    _write(tmp_path, "index.md", "# Root\n[missing](gone.md)\n[ok](child.md)\n")
    _write(tmp_path, "child.md", "# Child\n")
    a = assemble(tmp_path, "temp")
    kinds = {p.kind for p in a.problems}
    assert "missing" in kinds
    assert "child.md" in a.ordered_paths  # continued despite the broken link


def test_circular_reference_is_reported(tmp_path):
    _manifest(tmp_path)
    _write(tmp_path, "index.md", "# A\n[b](b.md)\n")
    _write(tmp_path, "b.md", "# B\n[back](index.md)\n")
    a = assemble(tmp_path, "temp")
    assert any(p.kind == "cycle" for p in a.problems)
    assert a.ordered_paths == ["index.md", "b.md"]  # both included exactly once


def test_path_outside_root_is_refused(tmp_path):
    pkg = tmp_path / "pkg"
    _manifest(pkg)
    _write(pkg, "index.md", "# Root\n[escape](../secret.md)\n")
    _write(tmp_path, "secret.md", "# Secret\n")
    a = assemble(pkg, "pkg")
    assert any(p.kind == "outside_root" for p in a.problems)
    assert a.ordered_paths == ["index.md"]

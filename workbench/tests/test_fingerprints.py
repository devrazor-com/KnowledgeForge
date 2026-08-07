"""Fingerprint determinism — the load-bearing property (PKG-7, TSK-4, ground rule 4).

Identical input must produce an identical value regardless of working directory,
absolute location, or line-ending style.
"""

import os
import shutil
from pathlib import Path

from workbench import config
from workbench.fingerprints import (
    normalize_content,
    package_fingerprint,
    task_fingerprint,
)
from workbench.packages import assemble

LARKSPUR = config.PACKAGES_DIR / "larkspur"


def test_package_fingerprint_is_independent_of_cwd(tmp_path):
    a = assemble(LARKSPUR, "larkspur").package.fingerprint
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        b = assemble(LARKSPUR, "larkspur").package.fingerprint
    finally:
        os.chdir(old)
    assert a == b


def test_package_fingerprint_is_independent_of_absolute_path(tmp_path):
    original = assemble(LARKSPUR, "larkspur").package.fingerprint
    moved_root = tmp_path / "elsewhere" / "larkspur"
    shutil.copytree(LARKSPUR, moved_root)
    moved = assemble(moved_root, "larkspur").package.fingerprint
    assert original == moved


def test_line_endings_do_not_change_the_fingerprint():
    lf = [{"path": "a.md", "content": "line1\nline2\n"}]
    crlf = [{"path": "a.md", "content": normalize_content("line1\r\nline2\r\n")}]
    assert package_fingerprint(lf) == package_fingerprint(crlf)


def test_content_change_changes_the_fingerprint():
    base = [{"path": "a.md", "content": "hello\n"}]
    changed = [{"path": "a.md", "content": "hello!\n"}]
    assert package_fingerprint(base) != package_fingerprint(changed)


def test_file_set_change_changes_the_fingerprint():
    one = [{"path": "a.md", "content": "x\n"}]
    two = [{"path": "a.md", "content": "x\n"}, {"path": "b.md", "content": "y\n"}]
    assert package_fingerprint(one) != package_fingerprint(two)


def test_task_fingerprint_ignores_cosmetic_fields_but_tracks_substance():
    base = {"id": "T1", "title": "One", "description": "do it",
            "acceptance_criteria": "done", "checks": [{"id": "C", "description": "d", "command": "cmd"}]}
    cosmetic = dict(base, title="A different title", business_area="X", difficulty="hard")
    assert task_fingerprint(base) == task_fingerprint(cosmetic)

    changed_desc = dict(base, description="do something else")
    assert task_fingerprint(base) != task_fingerprint(changed_desc)

    changed_check = dict(base, checks=[{"id": "C", "description": "d", "command": "other"}])
    assert task_fingerprint(base) != task_fingerprint(changed_check)

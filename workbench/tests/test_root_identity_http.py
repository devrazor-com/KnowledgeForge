"""Physical package-root identity, end to end over real HTTP (Windows-readiness).

The invariant these tests assert: one physical package directory backs at most ONE
registration, even if the operator reaches it through two different textual filesystem
paths — a case variant on a case-insensitive filesystem (default macOS APFS, Windows),
or a symlink/junction. Registration must not create a second row, and Change root must
refuse repointing onto a directory already registered as a different source.

Which internal guard enforces this is environment-dependent, and the tests deliberately
do NOT pin it down. Two guards sit in front of registration: an exact resolved-string
match, and `same_physical_dir` (os.path.samefile). Whether a case variant reaches the
physical guard depends on the filesystem: `Path.resolve()` does NOT canonicalise case on
macOS APFS (the two spellings stay distinct strings, so only samefile catches them), but
on the Windows machine we tested it already resolved both spellings to one on-disk casing
(the exact-string guard catches them first). Empirical finding, recorded narrowly — not a
universal claim about every Windows filesystem or path representation. samefile is kept as
defense-in-depth because case is only one aliasing mechanism; symlinks/junctions remain.

Case-variant tests skip on a genuinely case-sensitive filesystem. Nothing here touches
package-relative paths or fingerprints — this concerns only `package_source.root_path`.
"""

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

import pytest

from workbench.packages import normalize_root

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = REPO_ROOT / "workbench" / "examples" / "larkspur"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _env(dbpath):
    e = os.environ.copy()
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
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


def _register(wb_port, root_path):
    status, loc, _ = _post(wb_port, "/packages", [("root_path", str(root_path))])
    assert status == 303, (status, root_path)
    return loc.rsplit("/", 1)[-1]


def _fs_is_case_insensitive(tmp_path):
    d = tmp_path / "CaseProbe"; d.mkdir()
    ci = (tmp_path / "caseprobe").is_dir()
    d.rmdir()
    return ci


def _write_manifest(root: Path, package_id: str):
    (root / "package.yaml").write_text(
        f"package_id: {package_id}\nentry_point: larkspur-index.md\ntasks: tasks/\n", encoding="utf-8")


def test_add_via_case_variant_opens_existing_registration(tmp_path):
    if not _fs_is_case_insensitive(tmp_path):
        pytest.skip("case-sensitive filesystem: a case variant is a genuinely different directory")
    wb_port = _free_port()
    root = tmp_path / "pkgdir"
    shutil.copytree(LARKSPUR, root)                      # package_id 'larkspur'
    wb = _start("workbench.app:app", wb_port, _env(tmp_path / "wb.db"))
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port, root)
        # Register the SAME physical directory via an upper-cased spelling. The invariant
        # is that this opens the EXISTING registration (same source id) — no second row —
        # regardless of whether resolve() collapsed the case (exact-string guard) or left
        # the strings distinct (samefile physical guard). We do NOT assert which.
        variant = str(root).replace("pkgdir", "PKGDIR")
        status, loc, _ = _post(wb_port, "/packages", [("root_path", variant)])
        assert status == 303
        assert loc.rsplit("/", 1)[-1] == sid
    finally:
        _stop(wb)


def test_add_via_symlink_opens_existing_registration(tmp_path):
    wb_port = _free_port()
    root = tmp_path / "real"
    shutil.copytree(LARKSPUR, root)
    link = tmp_path / "link"
    try:
        os.symlink(root, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    wb = _start("workbench.app:app", wb_port, _env(tmp_path / "wb.db"))
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port, root)
        status, loc, _ = _post(wb_port, "/packages", [("root_path", str(link))])
        assert status == 303 and loc.rsplit("/", 1)[-1] == sid
    finally:
        _stop(wb)


def test_change_root_refuses_physical_dup_via_case_variant(tmp_path):
    """Invariant: Change root refuses repointing onto a directory already registered as
    a different source, even reached through a case variant, and names the collision.

    Setup: register S_B at .../shared (manifest id 'pkg-b'), and S_A at .../a
    (id 'pkg-a'). Then rewrite .../shared's manifest to 'pkg-a' so the identity-match
    check will pass, and change-root S_A to .../SHARED (upper case). It must be refused
    (409) and name S_B. Which guard refuses is filesystem-dependent and NOT asserted: on
    macOS APFS resolve() keeps .../SHARED distinct from S_B's stored .../shared so the
    samefile physical guard catches it; on a filesystem where resolve() collapses case,
    the exact-string guard catches it first. Either way the repoint is refused."""
    if not _fs_is_case_insensitive(tmp_path):
        pytest.skip("case-sensitive filesystem: a case variant is a genuinely different directory")
    wb_port = _free_port()
    shared = tmp_path / "shared"
    shutil.copytree(LARKSPUR, shared)
    _write_manifest(shared, "pkg-b")
    a = tmp_path / "a"
    shutil.copytree(LARKSPUR, a)
    _write_manifest(a, "pkg-a")
    wb = _start("workbench.app:app", wb_port, _env(tmp_path / "wb.db"))
    try:
        assert _wait_ready(wb_port)
        sid_b = _register(wb_port, shared)              # occupies the physical dir, id 'pkg-b'
        sid_a = _register(wb_port, a)                   # id 'pkg-a'
        # Drift: .../shared now declares pkg-a (S_B becomes unhealthy but stays registered here).
        _write_manifest(shared, "pkg-a")
        variant = str(shared).replace("shared", "SHARED")

        status, _, body = _post(wb_port, f"/packages/{sid_a}/change-root", [("root_path", variant)])
        assert status == 409, (status, body)
        assert sid_b in body                            # named the colliding registration
        # S_A is unchanged — still pointing at its own root.
        assert str(a.resolve()) in _get(wb_port, f"/packages/{sid_a}")
    finally:
        _stop(wb)

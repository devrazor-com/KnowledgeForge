"""Physical package-root identity, end to end over real HTTP (Windows-readiness).

The invariant: one physical package directory backs at most ONE registration, even
if the operator reaches it through two different textual filesystem paths — a
case-variant on a case-insensitive filesystem (default macOS APFS, Windows), or a
symlink/junction. `os.path.samefile` establishes this; a plain resolved-path string
cannot, because `realpath` does not canonicalise case.

These tests isolate the *physical* guard from the exact-string and identity-match
guards that sit in front of it:

* Add package via a case variant of an already-registered root opens the EXISTING
  registration instead of creating a second one (the resolved strings differ, so only
  the physical check can catch it).
* Change root refuses repointing onto a directory already registered as a different
  source, reached through a case variant (again, strings differ; the manifest is set
  up so the identity-match check passes, proving the physical check is what fires).

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

REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = REPO_ROOT / "workbench" / "examples" / "larkspur"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_ready(port, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _start(module, port, env):
    return subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
                            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p is None:
            continue
        try:
            p.terminate(); p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


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
        # Register the SAME physical directory via an upper-cased spelling.
        variant = str(root).replace("pkgdir", "PKGDIR")
        assert normalize_root(variant) != normalize_root(str(root))   # strings really differ...
        status, loc, _ = _post(wb_port, "/packages", [("root_path", variant)])
        assert status == 303
        # ...yet it opens the EXISTING registration (same source id) — no second row.
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
    """Isolate the physical guard from the identity-match and exact-string guards.

    Setup: register S_B at .../shared (manifest id 'pkg-b'), and S_A at .../a
    (id 'pkg-a'). Then rewrite .../shared's manifest to 'pkg-a'. Now change-root S_A
    to .../SHARED (upper case): the manifest there declares 'pkg-a' so the identity
    check PASSES and the resolved string (.../SHARED) does not exact-match S_B's stored
    .../shared — so only the samefile() physical check can refuse it. It must."""
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

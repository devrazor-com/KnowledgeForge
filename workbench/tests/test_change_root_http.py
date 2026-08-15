"""Change root — repoint a registration at a new location without changing identity.

The operator repair for a package whose files moved (a folder transferred from
another machine, a relocated checkout). Two guarantees, end to end over real HTTP:

* a successful repoint to a new root that declares the SAME package_id keeps the
  source id, the durable package_id, the validation profile and prior run evidence,
  and — because the knowledge bytes are identical — does NOT make anything stale; and
* a new root that declares a DIFFERENT package_id is refused, and the existing
  registration is left pointing at its original root.
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

from _regutil import start_server as _start, wait_ready as _wait_ready, stop_server as _stop
from workbench.packages import normalize_root
REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR = str(REPO_ROOT / "workbench" / "examples" / "larkspur")
CLAIMS = str(REPO_ROOT / "workbench" / "examples" / "claims")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _env(mock_port, dbpath):
    e = os.environ.copy()
    e["MOD3_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    e["WORKBENCH_DB"] = str(dbpath)
    e["WORKBENCH_DEV_MOCK"] = "1"
    e["WORKBENCH_TIMEOUT_SECONDS"] = "60"
    e["WORKBENCH_TIMEOUT_GUARD_SECONDS"] = "1"
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
    status, loc, _ = _post(wb_port, "/packages", [("root_path", root_path)])
    assert status == 303, (status, root_path)
    return loc.rsplit("/", 1)[-1]


def _copy_pkg(src, dst):
    shutil.copytree(src, dst)
    return str(dst)


def test_change_root_repoints_same_identity_and_preserves_state(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    old_root = _copy_pkg(LARKSPUR, tmp_path / "old" / "larkspur")
    new_root = _copy_pkg(LARKSPUR, tmp_path / "new" / "larkspur")   # identical content, new location
    mock = _start("tools.mock_gateway.app:app", mock_port, env)
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(mock_port) and _wait_ready(wb_port)
        sid = _register(wb_port, old_root)
        _post(wb_port, f"/packages/{sid}/profile",
              [("environment", "larkspur-sandbox"), ("capabilities", "filesystem"), ("configured_by", "op")])

        before = _get(wb_port, f"/packages/{sid}")
        # Assert the app's real guarantee: the resolved/stored root path is displayed
        # verbatim (its normal filesystem casing) — NOT a platform-normalised string.
        assert normalize_root(old_root) in before
        # fingerprint of the current knowledge, to prove the move does not disturb it
        fp_line = [l for l in before.splitlines() if "sha256:" in l]
        assert fp_line, "expected a package fingerprint on the detail page"

        # Repoint to the new root (same package_id 'larkspur').
        status, loc, _ = _post(wb_port, f"/packages/{sid}/change-root", [("root_path", new_root)])
        assert status == 303 and loc.rsplit("/", 1)[-1] == sid       # same source id preserved

        after = _get(wb_port, f"/packages/{sid}")
        assert normalize_root(new_root) in after                     # root now points at the new location
        assert normalize_root(old_root) not in after
        assert "larkspur" in after                                   # durable package_id preserved
        assert "Healthy" in after
        # Profile survived the move: the package is NOT back to "profile not configured".
        assert "profile not configured" not in after.lower()
        # Identical knowledge → identical fingerprint → nothing made stale by the move.
        for l in fp_line:
            assert l.strip() in after
    finally:
        _stop(wb, mock)


def test_change_root_refuses_package_id_mismatch(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    old_root = _copy_pkg(LARKSPUR, tmp_path / "old" / "larkspur")
    other_root = _copy_pkg(CLAIMS, tmp_path / "other" / "claims")   # declares package_id 'claims'
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        sid = _register(wb_port, old_root)                            # registered as 'larkspur'

        status, _, body = _post(wb_port, f"/packages/{sid}/change-root", [("root_path", other_root)])
        assert status == 400
        assert "Identity mismatch" in body and "larkspur" in body and "claims" in body

        # The registration is unchanged — still pointing at the original root.
        detail = _get(wb_port, f"/packages/{sid}")
        assert normalize_root(old_root) in detail                    # still the original resolved root
        assert normalize_root(other_root) not in detail
    finally:
        _stop(wb)


def test_change_root_refuses_a_path_already_registered_elsewhere(tmp_path):
    mock_port, wb_port = _free_port(), _free_port()
    env = _env(mock_port, tmp_path / "wb.db")
    root_a = _copy_pkg(LARKSPUR, tmp_path / "a" / "larkspur")
    root_b = _copy_pkg(CLAIMS, tmp_path / "b" / "claims")
    wb = _start("workbench.app:app", wb_port, env)
    try:
        assert _wait_ready(wb_port)
        _register(wb_port, root_a)                                    # A occupies its path
        sid_b = _register(wb_port, root_b)
        # Try to repoint B onto A's already-registered path (also an identity mismatch,
        # but the collision check must refuse it regardless).
        status, _, body = _post(wb_port, f"/packages/{sid_b}/change-root", [("root_path", root_a)])
        assert status in (400, 409)
    finally:
        _stop(wb)

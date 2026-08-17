"""Shared test helpers.

Two concerns live here so every HTTP test speaks to the app the same way on macOS
and Windows:

* Registration through the REAL endpoint (POST /packages) — using this instead of a
  startup auto-seed keeps tests on the product's own path (the operator registers a
  root; nothing self-populates the registry). It is idempotent.
* Subprocess server lifecycle — `start_server` / `wait_ready` / `stop_server`. These
  used to be copy-pasted into every HTTP test file with a fragile readiness check
  (a single bounded poll that returned False for BOTH "still starting" and "crashed
  on startup") and a stop that did not reliably wait for the child to exit. Windows
  is ~2x slower to start a child and locks open files, which turned both weaknesses
  into failures. Centralising them here fixes every test at once and stops the
  behaviour drifting between files. Call sites keep their original signatures
  (`_start(module, port, env)`, `_wait_ready(port)`, `_stop(*procs)`) by importing
  these under those names.
"""

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR_ROOT = str(REPO_ROOT / "workbench" / "examples" / "larkspur")

# --------------------------------------------------------------------------
# Subprocess server lifecycle (shared, Windows-robust)
# --------------------------------------------------------------------------

# port -> (Popen, captured-output path). Lets wait_ready(port) find the child so it
# can tell "slow" from "crashed" and surface the child's output — without changing
# the historical wait_ready(port) call signature.
_SERVERS: dict[int, tuple[subprocess.Popen, str]] = {}

# Generous but bounded: Windows child startup is materially slower than macOS.
READY_TIMEOUT = 45.0
STOP_TIMEOUT = 10.0


def start_server(module: str, port: int, env: dict, cwd: str | None = None) -> subprocess.Popen:
    """Launch a uvicorn app as a child process, capturing its stdout+stderr to a temp
    file so a startup crash can be reported. Registers it by port for wait_ready."""
    log = tempfile.NamedTemporaryFile(
        prefix=f"kf-server-{port}-", suffix=".log", delete=False)
    log_path = log.name
    # Serve harness child servers on the SELECTOR loop on every platform, via the same
    # production factory the run_workbench launcher uses — one cross-platform launch path.
    # On Windows this is the mitigation for the Proactor accept-loop failure; on
    # macOS/Linux it is the loop uvicorn already picks. No platform branch (see
    # REQUIREMENTS_CLARIFICATIONS.md, "Windows event loop").
    args = [sys.executable, "-m", "uvicorn", module, "--port", str(port),
            "--loop", "workbench.eventloop:selector_loop_factory", "--log-level", "warning"]
    proc = subprocess.Popen(
        args, cwd=str(cwd or REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    _SERVERS[port] = (proc, log_path)
    return proc


def _captured_output(port: int) -> str:
    entry = _SERVERS.get(port)
    if entry is None:
        return ""
    try:
        with open(entry[1], "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def wait_ready(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """Poll until the server answers 200 on '/', the child exits, or the deadline
    passes. Returns True when ready; otherwise raises AssertionError, distinguishing:

      * the child already exited before becoming ready — fail immediately, don't wait
        out the deadline, and surface its return code + captured output (a genuine
        startup crash, not slow startup);
      * the deadline passed while the child is still alive — a readiness timeout,
        reported distinctly (slow/hung startup), with whatever it logged.
    """
    proc = _SERVERS.get(port, (None, None))[0]
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(
                f"server on port {port} exited before becoming ready "
                f"(returncode={proc.returncode}) — startup crash, not slow startup.\n"
                f"--- captured child output ---\n{_captured_output(port)}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    if proc is not None and proc.poll() is not None:
        raise AssertionError(
            f"server on port {port} exited before becoming ready "
            f"(returncode={proc.returncode}) — startup crash, not slow startup.\n"
            f"--- captured child output ---\n{_captured_output(port)}")
    raise AssertionError(
        f"readiness timeout: server on port {port} still alive after {timeout:.0f}s "
        f"but never answered on '/'.\n--- captured child output ---\n{_captured_output(port)}")


def stop_server(*procs: subprocess.Popen | None) -> None:
    """Terminate each child and WAIT for it to actually exit (bounded), escalating to
    kill+wait if terminate is not enough. Only after confirmed exit is it safe for a
    test to touch the child's SQLite DB — on Windows the file stays locked until the
    process is truly gone. Cleans up the captured-output temp files last."""
    for p in procs:
        if p is None:
            continue
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=STOP_TIMEOUT)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=STOP_TIMEOUT)
            except Exception:
                pass
    for port in list(_SERVERS):
        proc, log_path = _SERVERS[port]
        if proc in procs:
            try:
                os.unlink(log_path)
            except OSError:
                pass
            del _SERVERS[port]
CLAIMS_ROOT = str(REPO_ROOT / "workbench" / "examples" / "claims")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def register(wb_port: int, root_path: str) -> str:
    """POST /packages and return the resulting source id (from the 303 Location)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{wb_port}/packages",
        data=urllib.parse.urlencode([("root_path", root_path)]).encode(), method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req) as r:
            loc = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
    assert loc, "registration did not redirect to the package detail"
    return loc.rsplit("/", 1)[-1]


def configure_profile(wb_port: int, source_id: str, environment: str, capabilities, by: str = "test") -> None:
    """Configure a package's validation profile via the real endpoint (3C-3: no
    profile, no run). Idempotent."""
    fields = [("environment", environment), ("configured_by", by)] + [("capabilities", c) for c in capabilities]
    try:
        with urllib.request.build_opener(_NoRedirect).open(urllib.request.Request(
                f"http://127.0.0.1:{wb_port}/packages/{source_id}/profile",
                data=urllib.parse.urlencode(fields).encode(), method="POST")):
            pass
    except urllib.error.HTTPError:
        pass


def ensure_larkspur(wb_port: int) -> str:
    """Register Larkspur AND configure a default profile so runs can start."""
    sid = register(wb_port, LARKSPUR_ROOT)
    configure_profile(wb_port, sid, "larkspur-sandbox", ["filesystem"])
    return sid


def ensure_claims(wb_port: int) -> str:
    sid = register(wb_port, CLAIMS_ROOT)
    configure_profile(wb_port, sid, "claims-sandbox", ["filesystem"])
    return sid

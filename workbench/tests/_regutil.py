"""Shared test helper: register a package root through the REAL registration
endpoint (POST /packages) and return its source id.

Using this instead of a startup auto-seed keeps tests on the same path the product
uses — the operator registers a root; nothing self-populates the registry. It is
idempotent (an already-registered path resolves to its existing id), so it is safe
to call before every run.
"""

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LARKSPUR_ROOT = str(REPO_ROOT / "workbench" / "examples" / "larkspur")
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

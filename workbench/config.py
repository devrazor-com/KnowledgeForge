"""Workbench configuration — plain module-level values plus a few env-driven
accessors. The env-driven ones are functions (not constants) so a test or an
integration run can override them via the environment without import-time capture.

The only Module 3 setting is the base URL. When the real Gateway arrives, that
one value changes and nothing else in Module 1 does.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

# Synthetic example packages (NFR-5). Each is its own folder with a Markdown
# index and a tasks/ subfolder.
PACKAGES_DIR = BASE_DIR / "examples"

# Local state, excluded from the repository (see .gitignore).
DATA_DIR = BASE_DIR / "data"

DEFAULT_MOD3_BASE_URL = "http://127.0.0.1:8003"

# Permitted capabilities are Module 1's OWN vocabulary (not owned by Module 3), so they
# stay a fixed in-code list. Target environments are different: Module 3 owns and
# publishes the accepted logical environment names, so they are deployment configuration
# (see environments() below), never a baked-in product list.
CAPABILITIES = ["filesystem", "shell", "database-read", "web-search"]

CONTRACT_VERSION = "0.1"
DEFAULT_TIMEOUT_SECONDS = 1800

# New tasks are active by default.
DEFAULT_TASK_ACTIVE = True

_TRUE = {"1", "true", "yes", "on"}


def mod3_base_url() -> str:
    """Where Module 3 lives. The one setting that changes for the real Gateway."""
    return os.environ.get("MOD3_BASE_URL", DEFAULT_MOD3_BASE_URL).rstrip("/")


class EnvironmentsConfigError(Exception):
    """The selectable Module 3 target environments are unconfigured or invalid. `kind`
    is one of 'unset' | 'unreadable' | 'empty' | 'duplicate'. `message` is operator-facing
    (no filesystem path or internal detail — those go to the server log only)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def environments() -> list[str]:
    """The configured selectable Module 3 target-environment names, read FRESH each call
    (no cache) from the UTF-8 file named by WORKBENCH_ENVIRONMENTS_FILE — so an edit takes
    effect without a restart. Module 3 owns these logical names; Module 1 only presents
    them and sends the selected one verbatim (no mapping/normalisation/discovery).

    Fail-closed: there is NO synthetic fallback. Raises EnvironmentsConfigError when the
    configuration is unset / unreadable / empty / has a duplicate. Parsing is the fixed
    cross-platform contract: utf-8-sig (tolerates a Windows BOM), splitlines (LF/CRLF),
    strip each line (file syntax only — never touches the name's internal characters),
    ignore blank lines and full-line `#` comments, reject duplicates. Names are returned
    verbatim, in file order."""
    path = os.environ.get("WORKBENCH_ENVIRONMENTS_FILE")
    if not path or not path.strip():
        raise EnvironmentsConfigError("unset",
            "No target environments are configured. Set WORKBENCH_ENVIRONMENTS_FILE to a "
            "UTF-8 text file listing the accepted Module 3 environment names, one per line.")
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        raise EnvironmentsConfigError("unreadable",
            "The target-environment configuration file could not be read. Check that "
            "WORKBENCH_ENVIRONMENTS_FILE points to an existing, readable file.") from None
    names: list[str] = []
    first_seen: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):   # PHYSICAL file line number
        line = raw.strip()
        if not line or line.startswith("#"):                    # blank / full-line comment
            continue
        if line in first_seen:
            raise EnvironmentsConfigError("duplicate",
                f"The target-environment configuration file has a duplicate entry "
                f"'{line}' (line {lineno}; first seen at line {first_seen[line]}). "
                f"Each environment name must appear once.")
        first_seen[line] = lineno
        names.append(line)
    if not names:
        raise EnvironmentsConfigError("empty",
            "The target-environment configuration file contains no environment names. "
            "Add one accepted Module 3 environment name per line (blank and #-comment "
            "lines are ignored).")
    return names


def db_path() -> Path:
    return Path(os.environ.get("WORKBENCH_DB", str(DATA_DIR / "workbench.db")))


def dev_mock_mode() -> bool:
    """Development/mock mode. Detected by an EXPLICIT flag, never by inspecting
    MOD3_BASE_URL (the real Gateway may also run on localhost). Defaults to OFF,
    so forgetting to set it hides the dev-only forced-outcome control rather than
    exposing it."""
    return os.environ.get("WORKBENCH_DEV_MOCK", "").strip().lower() in _TRUE


# --------------------------------------------------------------------------
# Termination timeouts (Step 3A). These are deliberately distinct so they can
# never be confused for one another:
#   * run_timeout_seconds() only SEEDS the request's execution_context.timeout_
#     seconds when Module 1 builds a ValidationRequest. Once the request exists,
#     the value IN that request is authoritative for the run's deadline.
#   * timeout_guard_seconds() is Module 1's small backstop margin beyond the
#     Gateway's execution budget.
#   * GATEWAY_HTTP_TIMEOUT bounds a SINGLE network call — never the whole run.
#   * GATEWAY_CANCEL_CLEANUP_TIMEOUT is only for the fire-and-forget cleanup
#     cancel issued AFTER a timeout has already been recorded.
# --------------------------------------------------------------------------

def run_timeout_seconds() -> int:
    """Default execution budget Module 1 puts in a new ValidationRequest."""
    return int(os.environ.get("WORKBENCH_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))


def timeout_guard_seconds() -> int:
    """Module 1's backstop margin beyond the Gateway's execution budget."""
    return int(os.environ.get("WORKBENCH_TIMEOUT_GUARD_SECONDS", "30"))


def gateway_http_timeout() -> float:
    """Ceiling for a single Gateway HTTP call. Subordinate to the run deadline."""
    return float(os.environ.get("WORKBENCH_GATEWAY_HTTP_TIMEOUT", "30"))


# Fixed, small budget for the post-timeout cleanup cancel only. Not the socket
# timeout, not the run deadline.
GATEWAY_CANCEL_CLEANUP_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Result-retrieval allowance (Step 3B-1). Once a run reaches a terminal event,
# execution is over; the contract's `result` op "returns nothing until the run
# reaches a terminal state" but does NOT guarantee the ValidationResult is
# available the instant the terminal event is emitted. This is a bounded,
# AUTHORITATIVE allowance for the Gateway to publish the result — conceptually
# separate from the execution deadline (it is not extra execution time). The same
# policy is used by the normal poller AND by restart recovery.
#   * each result call is bounded by min(GATEWAY_HTTP_TIMEOUT, remaining window);
#   * no new retry begins once the window is exhausted;
#   * on exhaustion Module 1 classifies by what actually failed (2B taxonomy).
# --------------------------------------------------------------------------

def result_retrieval_window_seconds() -> float:
    return float(os.environ.get("WORKBENCH_RESULT_RETRIEVAL_WINDOW_SECONDS", "30"))


def result_retrieval_interval() -> float:
    return float(os.environ.get("WORKBENCH_RESULT_RETRIEVAL_INTERVAL", "0.5"))

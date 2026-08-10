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

# Execution conditions offered when starting a run.
ENVIRONMENTS = ["larkspur-sandbox"]
CAPABILITIES = ["filesystem", "shell", "database-read", "web-search"]

CONTRACT_VERSION = "0.1"
DEFAULT_TIMEOUT_SECONDS = 1800

# New tasks are active by default.
DEFAULT_TASK_ACTIVE = True

_TRUE = {"1", "true", "yes", "on"}


def mod3_base_url() -> str:
    """Where Module 3 lives. The one setting that changes for the real Gateway."""
    return os.environ.get("MOD3_BASE_URL", DEFAULT_MOD3_BASE_URL).rstrip("/")


def db_path() -> Path:
    return Path(os.environ.get("WORKBENCH_DB", str(DATA_DIR / "workbench.db")))


def dev_mock_mode() -> bool:
    """Development/mock mode. Detected by an EXPLICIT flag, never by inspecting
    MOD3_BASE_URL (the real Gateway may also run on localhost). Defaults to OFF,
    so forgetting to set it hides the dev-only forced-outcome control rather than
    exposing it."""
    return os.environ.get("WORKBENCH_DEV_MOCK", "").strip().lower() in _TRUE

"""Workbench configuration — plain module-level constants, no framework.

Everything a single operator needs to point the tool at its data and, later, at
the Gateway. MOD3_BASE_URL is defined here for completeness but is unused until
Step 2 (Gateway interaction). Keeping it in one place is the whole point: when
the real Gateway arrives, this is the only setting that changes.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

# Where the synthetic example packages live. Each package is its own folder with
# a Markdown index and a tasks/ subfolder. (NFR-5: everything here is synthetic.)
PACKAGES_DIR = BASE_DIR / "examples"

# Local state. Excluded from the repository (see .gitignore).
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("WORKBENCH_DB", str(DATA_DIR / "workbench.db")))

# The only Module 3 setting. Unused in Step 1.
MOD3_BASE_URL = os.environ.get("MOD3_BASE_URL", "http://127.0.0.1:8003").rstrip("/")

# Execution conditions offered when starting a run. Used from Step 2 onward; the
# permitted-capability list is part of the validation context (staleness) but is
# treated as a plain list of conditions — no capability classification in V1.
ENVIRONMENTS = ["larkspur-sandbox"]
CAPABILITIES = ["filesystem", "shell", "database-read", "web-search"]

# New tasks are active by default (they count toward approval until turned off).
DEFAULT_TASK_ACTIVE = True

"""Test session setup.

Ensure the repository root is importable as `workbench.*` no matter where pytest is
invoked from (Windows or macOS).

Note: the Workbench serves packages from an operator-registered registry. Tests
register the packages they need EXPLICITLY (via `_regutil`), exercising the real
registration path — the application startup is never changed to auto-populate the
registry under test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

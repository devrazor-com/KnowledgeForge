import sys
from pathlib import Path

# Ensure the repository root is importable as `workbench.*` no matter where
# pytest is invoked from (Windows or macOS).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

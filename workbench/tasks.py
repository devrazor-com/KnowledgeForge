"""Task loading — TSK-1…TSK-4. Tasks are JSON files, one per file, living in the
package folder's tasks/ subdirectory (versioned alongside the package). Each is
fingerprinted; active/inactive state (TSK-5) is operator state and lives in the
database, not in the task file or its fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path

from workbench.fingerprints import task_fingerprint
from workbench.models import Task

_ALLOWED = ["id", "title", "description", "business_area", "difficulty",
            "acceptance_criteria", "checks", "metadata"]


def load_tasks(pkg_dir: Path) -> list[Task]:
    """Load and fingerprint every task JSON in <package>/tasks/, sorted by id.

    The `active` flag defaults to True here; the app layer overlays the persisted
    state before display.
    """
    tasks_dir = pkg_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks: list[Task] = []
    for path in sorted(tasks_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {k: raw[k] for k in _ALLOWED if k in raw}
        fp = task_fingerprint(fields)
        tasks.append(Task(**fields, fingerprint=fp, active=True))
    tasks.sort(key=lambda t: t.id)
    return tasks

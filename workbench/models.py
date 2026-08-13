"""Internal view models. Pydantic, private to Module 1 — not the contract models
(those arrive in Step 2 when messages actually cross the Module 2 boundary)."""

from __future__ import annotations

from pydantic import BaseModel


class KnowledgeFile(BaseModel):
    path: str
    content: str


class Problem(BaseModel):
    """A discovery issue surfaced to the operator, never silently swallowed (PKG-5)."""
    kind: str  # "missing" | "cycle" | "outside_root"
    detail: str


class Manifest(BaseModel):
    """The minimal package manifest (package.yaml). Structural configuration only:
    it tells Module 1 the package's durable logical identity (`package_id`) and where
    the entry point and task definitions live. It is NOT domain knowledge — it never
    crosses Module 2 and never enters any fingerprint. `package_id` is REQUIRED with no
    fallback (a durable identity that survives content/name/root changes); `name`/
    `version` are optional display metadata (also fingerprint-neutral)."""
    package_id: str
    entry_point: str
    tasks: str
    name: str | None = None
    version: str | None = None


class Package(BaseModel):
    name: str
    version: str
    main_file: str
    metadata: dict = {}
    files: list[KnowledgeFile]
    fingerprint: str


class Assembly(BaseModel):
    """Everything the package screen needs: the package plus how it was built.
    `dir_name` is the stable registry key (source id); `tasks_rel` is the manifest's
    declared tasks directory (relative to the root)."""
    dir_name: str
    package: Package
    ordered_paths: list[str]
    problems: list[Problem]
    package_id: str = ""
    entry_point: str = ""
    tasks_rel: str = "tasks/"


class Task(BaseModel):
    id: str
    title: str
    description: str
    business_area: str | None = None
    difficulty: str | None = None
    acceptance_criteria: str | None = None
    checks: list[dict] = []
    metadata: dict = {}
    fingerprint: str
    active: bool = True

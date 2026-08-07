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


class Package(BaseModel):
    name: str
    version: str
    main_file: str
    metadata: dict = {}
    files: list[KnowledgeFile]
    fingerprint: str


class Assembly(BaseModel):
    """Everything the package screen needs: the package plus how it was built."""
    dir_name: str
    package: Package
    ordered_paths: list[str]
    problems: list[Problem]


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

"""Package assembly — the production version of PKG-1…PKG-8.

Given a package folder, read the main file's YAML front matter for name/version/
metadata (PKG-2), discover every dependent document from front-matter declarations
and relative Markdown links followed recursively (PKG-3), refuse anything outside
the package root (PKG-4), report missing files and circular references rather than
skipping them silently (PKG-5), produce a deterministic ordered file list (PKG-6),
and fingerprint the result (PKG-7).

Discovery vs ordering are separate concerns: the recursive walk decides *which*
files belong and finds problems; the final order is always main-file-first then
the rest sorted by path, so the fingerprint never depends on traversal order.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

import yaml

from workbench.fingerprints import normalize_content, package_fingerprint
from workbench.models import Assembly, KnowledgeFile, Package, Problem

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class PackageError(ValueError):
    """A package that cannot be assembled at all (e.g. no identifiable index)."""


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split leading `---` YAML front matter from the body. Returns (meta, body).

    A file with no front matter yields ({}, text). Malformed YAML yields ({}, body)
    rather than raising — a bad front-matter block is the operator's to notice, not
    a crash.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                meta = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, body
    return {}, text


def _find_main_file(pkg_dir: Path) -> str:
    """The package index: a top-level *.md whose name contains 'index', else the
    lone top-level Markdown file."""
    candidates = sorted(
        p.name for p in pkg_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and "index" in p.name.lower()
    )
    if candidates:
        return candidates[0]
    md = sorted(p.name for p in pkg_dir.iterdir() if p.is_file() and p.suffix == ".md")
    if len(md) == 1:
        return md[0]
    raise PackageError(f"Cannot identify an index file in package '{pkg_dir.name}'")


def _md_links(body: str) -> list[str]:
    out = []
    for m in _LINK_RE.finditer(body):
        target = m.group(1).strip()
        target = target.split()[0] if target else target  # drop any "title"
        target = target.split("#", 1)[0]                   # drop any #anchor
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.endswith(".md"):
            out.append(target)
    return out


def _declared_and_linked(pkg_dir: Path, rel: str) -> list[str]:
    """Relative link targets for one file: front-matter `dependencies` + body links,
    in that order (order affects discovery only, not the final fingerprint)."""
    meta, body = parse_front_matter((pkg_dir / rel).read_text(encoding="utf-8"))
    links: list[str] = []
    deps = meta.get("dependencies") or []
    if isinstance(deps, list):
        links.extend(str(d) for d in deps)
    links.extend(_md_links(body))
    return links


def assemble(pkg_dir: Path, dir_name: str) -> Assembly:
    """Assemble one package folder into an immutable, fingerprinted snapshot."""
    pkg_dir = pkg_dir.resolve()
    main_file = _find_main_file(pkg_dir)

    problems: list[Problem] = []
    discovered: list[str] = []
    visited: set[str] = set()

    def visit(rel: str, stack: frozenset[str]) -> None:
        visited.add(rel)
        discovered.append(rel)
        for link in _declared_and_linked(pkg_dir, rel):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
            if target.startswith("..") or target.startswith("/"):
                problems.append(Problem(kind="outside_root",
                    detail=f"{rel} → {link}: resolves outside the package root; refused."))
                continue
            if not (pkg_dir / target).is_file():
                problems.append(Problem(kind="missing",
                    detail=f"{rel} → {link}: referenced file not found."))
                continue
            if target in stack:
                problems.append(Problem(kind="cycle",
                    detail=f"circular reference: {rel} → {target}."))
                continue
            if target in visited:
                continue  # reachable by more than one path; included once
            visit(target, stack | {target})

    visit(main_file, frozenset({main_file}))

    ordered_paths = [main_file] + sorted(p for p in discovered if p != main_file)
    files = [
        KnowledgeFile(path=p, content=normalize_content((pkg_dir / p).read_text(encoding="utf-8")))
        for p in ordered_paths
    ]
    fingerprint = package_fingerprint([f.model_dump() for f in files])

    meta, _ = parse_front_matter((pkg_dir / main_file).read_text(encoding="utf-8"))
    package = Package(
        name=str(meta.get("name") or dir_name),
        version=str(meta.get("version") or "0"),
        main_file=main_file,
        metadata=meta.get("metadata") or {},
        files=files,
        fingerprint=fingerprint,
    )
    return Assembly(dir_name=dir_name, package=package, ordered_paths=ordered_paths, problems=problems)


def list_package_dirs() -> list[str]:
    """Names of example package folders that have an identifiable index."""
    from workbench.config import PACKAGES_DIR
    out = []
    if PACKAGES_DIR.is_dir():
        for d in sorted(PACKAGES_DIR.iterdir()):
            if d.is_dir():
                try:
                    _find_main_file(d)
                    out.append(d.name)
                except PackageError:
                    pass
    return out

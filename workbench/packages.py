"""Package assembly — the production version of PKG-1…PKG-8.

A package is an operator-registered root folder containing a minimal manifest
(`package.yaml`) that declares two structural things only: the `entry_point`
Markdown index and the `tasks:` directory. From the declared entry point Module 1
discovers every dependent document via front-matter `dependencies` and relative
Markdown links, followed recursively (PKG-3), refuses anything outside the root
(PKG-4), reports missing files and circular references rather than skipping them
silently (PKG-5), produces a deterministic ordered file list (PKG-6), and
fingerprints the result (PKG-7).

The manifest is Module 1 structural configuration, NOT domain knowledge: it is
excluded from the assembled KnowledgePackage.files, never crosses Module 2, and
never enters the fingerprint. The fingerprint is derived only from the assembled
(package-relative path, normalised content) sequence, so identical knowledge under
a different absolute root — or on Windows vs macOS — hashes identically.

Discovery vs ordering are separate concerns: the recursive walk decides *which*
files belong and finds problems; the final order is always entry-first then the
rest sorted by path, so the fingerprint never depends on traversal order.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import Path

import yaml

from workbench.fingerprints import normalize_content, package_fingerprint
from workbench.models import Assembly, KnowledgeFile, Manifest, Package, Problem

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
MANIFEST_NAME = "package.yaml"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PackageError(ValueError):
    """A package that cannot be assembled at all (bad/missing manifest, missing
    entry point, unresolvable tasks directory, …)."""


# --------------------------------------------------------------------------
# Registry helpers — the machine-local root path and its stable id
# --------------------------------------------------------------------------

def normalize_root(path_str: str) -> str:
    """Machine-local canonical form of an operator-supplied root path. Expands `~`
    and resolves symlinks/`..`. This normalisation is for the REGISTRY only; it
    never touches package-relative knowledge paths or the fingerprint."""
    return str(Path(path_str).expanduser().resolve())


def source_id(root_path: str) -> str:
    """Stable, URL-safe id for a registered root: the folder basename slug plus a
    short hash of the normalised absolute path (disambiguates same-named folders)."""
    norm = normalize_root(root_path)
    base = _SLUG_RE.sub("-", Path(norm).name.lower()).strip("-") or "package"
    return f"{base}-{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:6]}"


def read_manifest(root: Path) -> Manifest:
    """Read and validate package.yaml. Structural configuration only."""
    mf = root / MANIFEST_NAME
    if not mf.is_file():
        raise PackageError(f"no {MANIFEST_NAME} at the package root")
    try:
        data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PackageError(f"{MANIFEST_NAME} is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise PackageError(f"{MANIFEST_NAME} must be a mapping")
    entry, tasks = data.get("entry_point"), data.get("tasks")
    if not isinstance(entry, str) or not entry.strip():
        raise PackageError(f"{MANIFEST_NAME} must declare a string 'entry_point'")
    if not isinstance(tasks, str) or not tasks.strip():
        raise PackageError(f"{MANIFEST_NAME} must declare a string 'tasks' directory")
    name = data.get("name")
    version = data.get("version")
    return Manifest(entry_point=entry.strip(), tasks=tasks.strip(),
                    name=str(name) if name is not None else None,
                    version=str(version) if version is not None else None)


def _safe_rel(root: Path, rel: str) -> str:
    """Normalise a manifest-declared relative path and refuse escapes from the root."""
    norm = posixpath.normpath(rel.lstrip("/"))
    if norm.startswith("..") or posixpath.isabs(norm):
        raise PackageError(f"'{rel}' resolves outside the package root")
    return norm


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


def assemble(root: Path, key: str) -> Assembly:
    """Assemble one registered package root into an immutable, fingerprinted snapshot.
    `key` is the stable registry id, used only as the routing/label identity — never
    the fingerprint. The manifest is read for the entry point and tasks directory and
    is itself excluded from the assembled knowledge files."""
    root = root.resolve()
    manifest = read_manifest(root)
    main_file = _safe_rel(root, manifest.entry_point)
    if not (root / main_file).is_file():
        raise PackageError(f"entry_point '{manifest.entry_point}' is not a file in the package root")
    tasks_rel = _safe_rel(root, manifest.tasks)

    problems: list[Problem] = []
    discovered: list[str] = []
    visited: set[str] = set()

    def visit(rel: str, stack: frozenset[str]) -> None:
        visited.add(rel)
        discovered.append(rel)
        for link in _declared_and_linked(root, rel):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
            if target.startswith("..") or target.startswith("/"):
                problems.append(Problem(kind="outside_root",
                    detail=f"{rel} → {link}: resolves outside the package root; refused."))
                continue
            if not (root / target).is_file():
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
        KnowledgeFile(path=p, content=normalize_content((root / p).read_text(encoding="utf-8")))
        for p in ordered_paths
    ]
    fingerprint = package_fingerprint([f.model_dump() for f in files])

    meta, _ = parse_front_matter((root / main_file).read_text(encoding="utf-8"))
    package = Package(
        name=str(manifest.name or meta.get("name") or root.name),
        version=str(manifest.version or meta.get("version") or "0"),
        main_file=main_file,
        metadata=meta.get("metadata") or {},
        files=files,
        fingerprint=fingerprint,
    )
    return Assembly(dir_name=key, package=package, ordered_paths=ordered_paths,
                    problems=problems, entry_point=main_file, tasks_rel=tasks_rel)


def catalog_status(source: dict) -> dict:
    """CHEAP structural check for the Packages catalog — safe with 50+ sources.

    It does NOT assemble the package: no dependency traversal, no reading of every
    knowledge file, no fingerprint. It only establishes whether the registered
    source is *basically usable*: the root exists and is a directory, the manifest
    exists and parses, and the declared entry point resolves to a file. Full
    assembly, fingerprinting, and detailed diagnostics happen on the detail path
    (load_source). Status is deliberately narrow — 'ok' means structurally usable,
    NOT the fully-verified 'healthy' the detail page can claim after assembly."""
    root = Path(source["root_path"])
    v = {"id": source["id"], "root_path": source["root_path"],
         "name": root.name, "version": None, "status": "unusable", "detail": None}
    if not root.exists():
        v["detail"] = "Registered root no longer exists."
        return v
    if not root.is_dir():
        v["detail"] = "Registered root is not a directory."
        return v
    try:
        manifest = read_manifest(root)               # reads package.yaml only
        entry = _safe_rel(root, manifest.entry_point)
        _safe_rel(root, manifest.tasks)              # cheap: just refuse an escaping path
    except PackageError as e:
        v["detail"] = str(e)
        return v
    if not (root / entry).is_file():
        v["detail"] = f"declared entry_point '{manifest.entry_point}' is missing"
        return v
    name, version = manifest.name, manifest.version
    if not name or not version:                      # one cheap read of the entry file, no traversal
        try:
            meta, _ = parse_front_matter((root / entry).read_text(encoding="utf-8"))
            name = name or meta.get("name")
            version = version or meta.get("version")
        except OSError:
            pass
    v.update(name=name or root.name, version=version, status="ok")
    return v


def load_source(source: dict) -> dict:
    """Build the Packages/detail view for one registered source, computing health.
    An unhealthy (unloadable) source is NEVER dropped — it is returned with a clear
    reason so the operator can see and fix what broke.

    Health tiers:
      * 'unloadable' — cannot assemble (missing root, not a dir, unreadable, bad or
        missing manifest, missing entry point, unresolvable tasks dir);
      * 'problems'   — assembled, but discovery reported broken links / cycles;
      * 'healthy'    — assembled cleanly, tasks load."""
    view = {"id": source["id"], "root_path": source["root_path"], "added_at": source["added_at"],
            "health": "unloadable", "detail": None, "name": Path(source["root_path"]).name,
            "version": None, "entry_point": None, "tasks_rel": None,
            "file_count": 0, "task_count": 0, "problem_count": 0, "fingerprint": None,
            "assembly": None, "tasks": []}
    root = Path(source["root_path"])
    if not root.exists():
        view["detail"] = "Registered root no longer exists."
        return view
    if not root.is_dir():
        view["detail"] = "Registered root is not a directory."
        return view
    try:
        assembly = assemble(root, source["id"])
    except PackageError as e:
        view["detail"] = str(e)
        return view
    except (OSError, UnicodeDecodeError) as e:
        view["detail"] = f"Cannot read package: {e}"
        return view

    from workbench.tasks import load_tasks
    try:
        tasks = load_tasks(root / assembly.tasks_rel)
    except (OSError, ValueError) as e:
        view["detail"] = f"Cannot load tasks from '{assembly.tasks_rel}': {e}"
        return view

    view.update({
        "health": "problems" if assembly.problems else "healthy",
        "name": assembly.package.name, "version": assembly.package.version,
        "entry_point": assembly.entry_point, "tasks_rel": assembly.tasks_rel,
        "file_count": len(assembly.package.files), "task_count": len(tasks),
        "problem_count": len(assembly.problems), "fingerprint": assembly.package.fingerprint,
        "assembly": assembly, "tasks": tasks,
    })
    if assembly.problems:
        view["detail"] = f"{len(assembly.problems)} discovery problem(s)."
    return view

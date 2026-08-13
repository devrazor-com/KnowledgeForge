# KnowledgeForge package format (Module 1 V1)

What Module 1 genuinely requires from a knowledge package — deliberately minimal,
so authors organise their knowledge however they like. This states the *format*,
not any one example's accidental shape.

## A package is a root folder containing a manifest

Register a package by pointing the Workbench at its **root folder**. The root must
contain a manifest named **`package.yaml`** with three required keys:

```yaml
package_id: claims                # durable logical identity (see below)
entry_point: claims-overview.md   # the index Markdown file, relative to the root
tasks: validation/                # the directory of task JSON files, relative to the root
```

Optional display metadata (fingerprint-neutral):

```yaml
name: Claims Adjudication
version: "2.1"
```

### `package_id` — durable logical identity

`package_id` answers "which logical package is this?" It is **required, with no
fallback** — it is never inferred from `name`, entry-point front-matter, folder
name, or the registration. It is stable across content changes, name changes, root
moves, and unregister/re-register; changing content changes the *fingerprint* but
not the `package_id`, and moving the root changes neither.

- Syntax: lowercase letters, digits and single hyphens (`^[a-z0-9]+(-[a-z0-9]+)*$`)
  — route-safe, no spaces. Invalid or missing values fail package-format validation;
  the package stays visible in the catalog as **Unhealthy** with a clear reason.
- Like the rest of the manifest, it does **not** cross Module 2, is **not** in
  `KnowledgePackage.files`, and does **not** participate in any fingerprint.
- At most **one active registered source** may declare a given `package_id`;
  registering a second active root with the same id is rejected. Changing a
  registered package's `package_id` on disk is an **identity change**: Module 1
  refuses to run against it and asks the operator to unregister/re-register
  deliberately — identity never changes silently under existing evidence.

Identity axes are kept separate: `package_id` (which logical package),
`package_fingerprint` (what exact knowledge bytes a run used), and `source_id`/root
(where the current registration lives).

Nothing else is required or interpreted. There is **no** fixed directory layout,
no reserved folder names, and no Business/Technical/Skills taxonomy — how an author
arranges Markdown into folders is an authoring convention Module 1 does not need to
understand.

## The manifest is structural configuration, not knowledge

`package.yaml` tells Module 1 *how to assemble* the package. It is **not** domain
knowledge:

- it is **excluded** from the assembled `KnowledgePackage.files`;
- it **never crosses Module 2** (Module 3 never receives it);
- it **never participates** in any fingerprint.

Consequence: renaming `tasks: tasks/` to `tasks: validation/` (and moving the task
files) does not change the knowledge fingerprint — tasks were never part of the
assembled knowledge, and the manifest is excluded.

## Knowledge assembly

Starting from `entry_point`, Module 1 discovers every dependent Markdown document
via, in order:

1. front-matter `dependencies:` (a list of relative paths), and
2. ordinary relative Markdown links `[text](path.md)` in the body,

followed **recursively**. A package may use either mechanism or both. References
outside the root are refused; missing files and circular references are **reported
as problems**, never silently skipped or invented. The assembled file order is
entry-point-first, then the remaining paths sorted — so the fingerprint never
depends on traversal order.

## Fingerprints are machine-independent

The **package (knowledge) fingerprint** is a SHA-256 over the assembled sequence of
`(package-relative path, newline-normalised content)`. It therefore does **not**
depend on the absolute registered root, the OS, or the manifest. The same knowledge
checked out at `/Users/…/claims` or `C:\…\claims` produces the same fingerprint.

The **task fingerprint** is separate (task id + description + acceptance criteria +
checks). Later (3C-3) a **validation-context fingerprint** combines the package
fingerprint, task fingerprint, permitted capabilities, and target environment.
These are distinct concepts and are never conflated with structural loader config.

## Health

A registered root that cannot be assembled (missing/invalid manifest, missing entry
point, unresolvable tasks directory, missing/unreadable root) is kept **visible** as
*unhealthy* with a clear reason, rather than disappearing. A root that assembles but
whose discovery reports broken links or cycles is shown as *assembled with problems*.

## Example packages

- `workbench/examples/larkspur` — entry `larkspur-index.md`, tasks `tasks/`, mixes
  front-matter `dependencies` and relative links.
- `workbench/examples/claims` — entry `claims-overview.md`, tasks `validation/`,
  nested `domain/ architecture/ procedures/` folders, discovery by relative links
  only. Deliberately a different physical shape; it uses the same loader with no
  package-specific code.

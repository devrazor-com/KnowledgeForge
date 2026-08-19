# Pre-commit verification checklist

Run this before committing each milestone. It's a practical checklist, not a
process document — the point is that another developer can check out the repo and
get identical behaviour, which local reruns alone don't prove.

Commands assume you're at the repository root. Windows equivalents:
`workbench\.venv\Scripts\...` instead of `./workbench/.venv/bin/...`.

**Supported interpreter: Python 3.12** (`>=3.12,<3.13`, recorded in `.python-version`).
Build the venv with `python3.12` (macOS/Linux) or `py -3.12` (Windows) so Mac and
Windows integration run the same asyncio implementation. For a reproducible environment,
install the fully-pinned closure with `pip install -r workbench/requirements.lock`
instead of `requirements.txt` — the lock marks the Windows-only `colorama` with an
environment marker, so the same file is correct on both OSes.

1. **Tests pass.**
   ```
   ./workbench/.venv/bin/python -m pytest workbench/tests -q
   ```
   The suite must include, where applicable:
   - **A real-HTTP integration test.** For any milestone that talks to an
     external component (e.g. the Gateway), at least one test runs that component
     as a *separate process* on a real local port and drives a complete flow over
     the socket — not only an in-process ASGI client. This proves the same network
     boundary that will exist in production.
   - **A mid-run disconnect/reconnect test** (for milestones with live event
     streaming). Start a run, read some events, disconnect while the run is still
     in flight, reconnect, and assert: persisted events are replayed, live delivery
     continues, and the *effective* (client-deduplicated) event sequence has no loss
     and no duplicates and reaches the correct result and verdict. Assert the
     effective/rendered result, not a specific server resume mechanism — the server
     is allowed to re-send already-seen events. This is now an automated test, not
     a manual step.

2. **Server stops cleanly and the port is actually closed** (if one was running).
   Kill it, then confirm — don't just assume:
   ```
   lsof -i tcp:8010            # expect: no output
   curl -s http://127.0.0.1:8010/   # expect: connection refused
   ```

3. **Clean-clone check — the important one.** Clone the repo as it would be after
   this commit into a temp dir, install fresh, and confirm identical behaviour in
   a fresh process and working directory. This catches the class of bug a local
   rerun cannot (missing-but-untracked files, path assumptions, `.gitignore` that
   excludes real content). Commit locally first (unpushed) so the clone reflects it.
   ```
   TMP=$(mktemp -d); git clone -q . "$TMP/kf"; cd "$TMP/kf"
   python3 -m venv workbench/.venv
   ./workbench/.venv/bin/pip install -q -r workbench/requirements.txt
   ./workbench/.venv/bin/python -m pytest workbench/tests -q
   # and confirm the package fingerprint matches the value seen in the live app
   cd - && rm -rf "$TMP"
   ```
   The fingerprint from the clone must equal the one shown in the running app.

4. **Frozen areas untouched.** `contract/` and `poc/` must have zero diff.
   ```
   git diff --stat HEAD -- contract poc     # expect: empty
   ```

5. **Nothing local is tracked.** No virtualenv, local database/data dir, caches,
   or `.DS_Store` staged.
   ```
   git diff --cached --name-only | grep -E '\.venv/|/data/|__pycache__|\.db$|\.DS_Store' || echo clean
   ```

6. **Real requirement IDs only.** Any `PKG/TSK/EXE/VER/EVD/HST/APR/UI/NFR/GW-<n>`
   in code or docs must be a real ID (ranges: PKG 1-8, TSK 1-5, EXE 1-8, VER 1-7,
   EVD 1-5, HST 1-4, APR 1-4, UI 1-6, NFR 1-7, GW 1-12). Traceability only works
   if the IDs are real.

7. **Review the exact commit contents.** Know precisely what's going in.
   ```
   git diff --cached --name-only
   ```

8. **Help is part of the operator-facing contract.** Any change that adds, removes,
   renames or materially alters an operator-visible field, action, status, error,
   workflow or interpretation **must update the relevant Help content
   (`workbench/templates/help.html`) in the same change set**. A feature that changes
   operator-visible behaviour is incomplete until its Help is updated. The Help ↔ code
   coupling has a cheap mechanical backstop — `workbench/tests/test_help_http.py`
   asserts every machine-defined outcome/error/cancel-delivery token (`workbench/vocab.py`),
   the required section anchors, and the key operator concepts are present, and guards
   against presenting frozen future Gateway behaviour as current. Extend those structural
   assertions when you add operator-visible vocabulary or a Help section; prose quality
   stays owned by human review, not by brittle sentence-level tests.

---

# Transferring the Workbench from macOS to Windows

The Workbench is developed on macOS but runs on a Windows work machine to reach the
real Module 3 Gateway. This is a **folder transfer** (the work machine can't clone),
so a little care avoids carrying machine-specific junk across.

## Should the dev database travel? — start clean (recommended)

**Recommendation: do NOT carry the database. Start clean on Windows.** Bring only the
code and the package folders, create a fresh venv, then register each package root and
configure its profile fresh. The dev DB holds synthetic local runs with no value on the
work machine; leaving it behind avoids transferring SQLite state (and its `-wal`/`-shm`
sidecar files) and sidesteps every "why is this Mac path Unhealthy" question. This is
the primary path, and **Change root does not depend on it** — Change root is the repair
for a package folder that moved, not for a transferred database.

**Secondary — preserving the Mac database.** If you specifically want the existing runs
and approvals to appear on Windows, you *can* copy `workbench/data/`. Two caveats:
- **Stop the Workbench first**, then copy `workbench.db` **together with any
  `workbench.db-wal` and `workbench.db-shm`** sidecar files if present (they hold
  not-yet-checkpointed writes; copying the `.db` alone can lose or corrupt recent
  state). Stopping the server checkpoints and usually removes the sidecars, which is
  why stopping first is the clean way.
- Every package will show **Unhealthy — "Registered root no longer exists"** because the
  stored roots are Mac paths. Repair each with **Change root** (README → *Moving a
  package folder between machines*). The runs/approvals themselves survive the move.

## What to ZIP (from the repo root, on the Mac)

Exclude the virtualenv (Mac-specific binaries — it must be rebuilt on Windows), Python
caches, the local database/data dir (per the recommendation above), and OS cruft. Keep
`.git` **out** too unless you deliberately want history on the work machine — it isn't
needed to run, and dropping it keeps the ZIP small (the work machine won't `git pull`
anyway). Package folders under `workbench/examples/` travel as ordinary content.

```bash
# From the repository root on macOS. Produces ../knowledgeforge-transfer.zip
zip -r ../knowledgeforge-transfer.zip . \
  -x '*/.venv/*' '.venv/*' \
     '*/__pycache__/*' '*.pyc' '*/.pytest_cache/*' \
     '*/data/*' '*.db' '*.db-wal' '*.db-shm' \
     '.git/*' '*/.DS_Store' '.DS_Store' \
     '*/scratchpad/*' '*.zip'
```

(If you *are* preserving the database, drop the `'*/data/*' '*.db' …` excludes and stop
the Workbench before zipping.)

## Ordered Windows checklist (PowerShell)

1. **Extract** the ZIP to a short, stable path — e.g. `C:\KnowledgeForge`. Avoid deep
   or space-heavy paths.
2. **Confirm no Mac venv came across:** `Test-Path workbench\.venv` should be `False`.
   If a `.venv` is present, delete it — it contains Mac binaries and won't run.
3. **Create a fresh venv and install:**
   ```powershell
   cd C:\KnowledgeForge
   py -3 -m venv workbench\.venv
   workbench\.venv\Scripts\python -m pip install -r workbench\requirements.txt
   ```
4. **Point at the Gateway:** `$env:MOD3_BASE_URL = "https://<gateway-host>:<port>"`
   (or the dev mock's URL).
4b. **Configure target environments:** copy `workbench\environments.example.txt` to a
   machine-local file (convention `workbench\local\environments.txt`, git-ignored, separate
   from `workbench\data\`), edit it with the names your Module 3 accepts (one per line), and
   set `$env:WORKBENCH_ENVIRONMENTS_FILE = "<that path>"`. Without a valid file the Workbench
   still starts but shows an actionable message and blocks runs (fail-closed, no synthetic
   fallback); edits are picked up without a restart.
5. **Run the tests** to confirm the transfer is sound — expect all pass with a small
   number **skipped** (the POSIX-signal tests):
   ```powershell
   workbench\.venv\Scripts\python -m pytest workbench\tests -q
   ```
6. **Start the Workbench** (the same launch command on every platform):
   `workbench\.venv\Scripts\python -m workbench.run_workbench`
   Confirm the startup line `[workbench] serving on event loop: …_WindowsSelectorEventLoop`
   (a selector loop; on macOS/Linux it is `…_UnixSelectorEventLoop`). The launcher selects
   the Selector loop internally — on Windows this avoids the Proactor accept-loop failure.
7. **Register packages** (clean start): open `http://127.0.0.1:8010/`, register each
   package root by its Windows path, and configure each profile. *(If instead you
   preserved the Mac DB, use **Change root** on each Unhealthy package instead of
   registering fresh.)*
8. **Verify before trusting:** each package shows **Healthy**, and a fingerprint shown
   on Windows matches the one for the same package on the Mac (they must be identical —
   fingerprints are machine- and line-ending-independent).

## Windows command mapping (quick reference)

| macOS / Linux | Windows |
|---|---|
| `./workbench/.venv/bin/python` | `workbench\.venv\Scripts\python` |
| `python3 -m venv …` | `py -3 -m venv …` |
| `export MOD3_BASE_URL=…` | `$env:MOD3_BASE_URL="…"` (PowerShell) / `set MOD3_BASE_URL=…` (cmd) |
| `source …/bin/activate` | `…\Scripts\Activate.ps1` (PowerShell) / `…\Scripts\activate.bat` (cmd) |

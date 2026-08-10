# Pre-commit verification checklist

Run this before committing each milestone. It's a practical checklist, not a
process document — the point is that another developer can check out the repo and
get identical behaviour, which local reruns alone don't prove.

Commands assume you're at the repository root. Windows equivalents:
`workbench\.venv\Scripts\...` instead of `./workbench/.venv/bin/...`.

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

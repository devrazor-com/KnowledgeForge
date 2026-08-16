"""Supported entry point for the Validation Workbench — use this to start Module 1.

    python -m workbench.run_workbench

It calls `uvicorn.run(...)` (so uvicorn's `Server.run()` owns the asyncio.Runner and the
clean shutdown lifecycle) and, on Windows, selects a Selector event loop via uvicorn's
custom loop-factory mechanism. That makes Module 1's Windows accept-loop mitigation
AUTOMATIC — it does not depend on anyone remembering a `--loop` flag, which is the human
-error path back to the vulnerable Proactor loop. Off Windows it uses uvicorn's default.

The app's startup logs the live event loop (`[workbench] serving on event loop: ...`),
so a Windows launch can be positively confirmed to serve on `_WindowsSelectorEventLoop`.

Host/port default to 127.0.0.1:8010; override with WORKBENCH_HOST / WORKBENCH_PORT.
Point Module 1 at a Gateway with MOD3_BASE_URL as usual — unchanged by this launcher.
"""
import os
import sys

import uvicorn


def main() -> None:
    host = os.environ.get("WORKBENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("WORKBENCH_PORT", "8010"))
    kwargs: dict = {"host": host, "port": port}
    if sys.platform.startswith("win"):
        # Force the Selector loop through uvicorn's own factory mechanism; Server.run()
        # still owns the runner + shutdown lifecycle (verified clean on Ctrl-C).
        kwargs["loop"] = "workbench.winloop:selector_loop_factory"
    uvicorn.run("workbench.app:app", **kwargs)


if __name__ == "__main__":
    main()

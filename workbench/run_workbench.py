"""Supported entry point for the Validation Workbench — use this to start Module 1 on
every supported platform (Windows, macOS, Linux):

    python -m workbench.run_workbench

It calls `uvicorn.run(...)` (so uvicorn's `Server.run()` owns the asyncio.Runner and the
clean shutdown lifecycle) and always selects the Selector event loop via uvicorn's custom
loop-factory mechanism. This is one cross-platform choice, not a Windows workaround: it is
the loop uvicorn already uses on macOS/Linux (uvloop is absent), and on Windows it is the
required mitigation for the Proactor accept-loop failure (see workbench/eventloop.py and
REQUIREMENTS_CLARIFICATIONS.md). No `--loop` flag to remember, no platform branch.

The app's startup logs the live event loop as `<module>.<class>` (e.g.
`asyncio.unix_events._UnixSelectorEventLoop` on macOS, `asyncio.windows_events.
_WindowsSelectorEventLoop` on Windows), so the serving loop can be positively confirmed.

Host/port default to 127.0.0.1:8010; override with WORKBENCH_HOST / WORKBENCH_PORT.
Point Module 1 at a Gateway with MOD3_BASE_URL as usual — unchanged by this launcher.
"""
import os

import uvicorn


def main() -> None:
    host = os.environ.get("WORKBENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("WORKBENCH_PORT", "8010"))
    # Explicit Selector loop on every platform via uvicorn's own factory mechanism;
    # Server.run() still owns the runner + shutdown lifecycle (verified clean on Ctrl-C).
    uvicorn.run("workbench.app:app", host=host, port=port,
                loop="workbench.eventloop:selector_loop_factory")


if __name__ == "__main__":
    main()

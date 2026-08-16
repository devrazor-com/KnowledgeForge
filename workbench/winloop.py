"""Windows event-loop mitigation for Module 1.

On Windows the default asyncio ProactorEventLoop can lose its listening socket for good
if an incoming connection is aborted while `AcceptEx` is completing: `finish_accept`
lets `WinError 64` (ERROR_NETNAME_DELETED) escape, `_start_serving`'s `except OSError`
closes the socket and does NOT re-arm accept (re-arm is only on the success branch), so
the process stays alive but can no longer accept connections. The SelectorEventLoop
keeps its listener registered across per-accept errors (only the resource-exhaustion
path removes and re-schedules the reader), so it is not exposed to that failure. This
was established by source inspection and confirmed by a Windows A/B experiment; see
REQUIREMENTS_CLARIFICATIONS.md ("Windows event loop").

`selector_loop_factory` is a ZERO-ARGUMENT loop factory for uvicorn's custom-loop
mechanism — `Config(loop="workbench.winloop:selector_loop_factory")`, the `--loop`
flag, or `run_workbench.py`. uvicorn's `Server.run()` builds the loop from this factory
inside its own `asyncio.Runner`, so uvicorn keeps ownership of the runner and its clean
shutdown lifecycle (the manually-driven `loop.run_until_complete(server.serve())`
pattern regresses Ctrl-C and is deliberately not used).

`asyncio.SelectorEventLoop` is cross-platform-safe to reference: on Windows it is
`_WindowsSelectorEventLoop`; elsewhere it is the platform's selector loop. This module
therefore imports and runs anywhere, and is only wired in on Windows by the launcher.
"""
import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a fresh Selector event loop (Windows -> _WindowsSelectorEventLoop)."""
    return asyncio.SelectorEventLoop()

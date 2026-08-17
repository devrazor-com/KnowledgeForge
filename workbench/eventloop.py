"""Event-loop selection for Module 1 — one explicit choice on every platform.

Module 1 serves on the asyncio **Selector** event loop everywhere. This is a single
cross-platform decision, not a Windows workaround: `asyncio.SelectorEventLoop()` is what
uvicorn already picks on macOS/Linux today (we install plain `uvicorn`, not
`uvicorn[standard]`, so uvloop is absent and `auto` resolves to the selector loop), and
on Windows it is the required mitigation for the default ProactorEventLoop's accept-loop
failure (an incoming connection aborted while `AcceptEx` completes lets `WinError 64`
escape `finish_accept`; `_start_serving` then closes the listener without re-arming, so
the process stays alive but can no longer accept — see REQUIREMENTS_CLARIFICATIONS.md,
"Windows event loop"). Selecting the selector loop explicitly everywhere gives one launch
path and lets the same loop-selection be verified on any OS.

`selector_loop_factory` is a ZERO-ARGUMENT loop factory for uvicorn's custom-loop
mechanism — `Config(loop="workbench.eventloop:selector_loop_factory")`, the `--loop`
flag, or `run_workbench.py`. uvicorn's `Server.run()` builds the loop from it inside its
own `asyncio.Runner`, keeping uvicorn's runner and clean shutdown lifecycle (the
manually-driven `loop.run_until_complete(server.serve())` pattern regresses Ctrl-C and is
deliberately not used).

`asyncio.SelectorEventLoop` is defined on every platform: Windows
`_WindowsSelectorEventLoop`, macOS/Linux `_UnixSelectorEventLoop` — all subclasses of
`asyncio.selector_events.BaseSelectorEventLoop`. This module therefore imports and runs
anywhere with no platform branch.
"""
import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a fresh Selector event loop for the current platform (a
    BaseSelectorEventLoop subclass — never a ProactorEventLoop)."""
    return asyncio.SelectorEventLoop()

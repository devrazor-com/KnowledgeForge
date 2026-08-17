"""Prove the harness child servers ACTUALLY ADOPT the Selector loop — on every platform.

Module 1 selects the Selector loop on all platforms (one cross-platform launch path), so
this runs on macOS, Linux and Windows — not just Windows. It starts a real Workbench
child through the SAME `_regutil.start_server` the whole HTTP harness uses, waits for
readiness, and reads the child's startup diagnostic, which reports the live serving loop
as `<module>.<class>` from inside the serving process.

The proof is STRUCTURAL across the subprocess boundary: the test resolves that exact class
independently and asserts it derives from `asyncio.selector_events.BaseSelectorEventLoop`.
This proves something about the LIVE serving loop (reported by the running server), not
merely that the factory can construct a Selector loop. Expected concrete classes:
`_WindowsSelectorEventLoop` (Windows), `_UnixSelectorEventLoop` (macOS/Linux) — both
BaseSelectorEventLoop subclasses; a ProactorEventLoop is not.

The external A/B already established Selector resilience vs Proactor; this only proves the
harness adopted the already-validated loop. The mock is launched through the identical
start_server argv, so the Workbench child's positive proof carries the same evidence for
the mock's loop configuration.
"""
import asyncio
import importlib
import os
import re
import socket

from _regutil import _captured_output, start_server, stop_server, wait_ready

_LOOP_LINE = re.compile(r"serving on event loop:\s*([\w.]+)\s*$", re.MULTILINE)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _resolve(qualname: str) -> type:
    module_name, _, class_name = qualname.rpartition(".")
    return getattr(importlib.import_module(module_name), class_name)


def test_harness_child_serves_on_selector_loop(tmp_path):
    port = _free_port()
    env = os.environ.copy()
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    wb = start_server("workbench.app:app", port, env)
    try:
        assert wait_ready(port)
        out = _captured_output(port)
        m = _LOOP_LINE.search(out)
        assert m, f"no serving-loop diagnostic captured.\n--- captured child output ---\n{out}"
        qualname = m.group(1)                       # e.g. asyncio.unix_events._UnixSelectorEventLoop
        loop_cls = _resolve(qualname)               # resolve the live class independently
        # Structural proof: the live serving loop IS a selector loop (never a Proactor loop).
        assert issubclass(loop_cls, asyncio.selector_events.BaseSelectorEventLoop), (
            f"harness child served on {qualname}, which is not a BaseSelectorEventLoop subclass")
    finally:
        stop_server(wb)

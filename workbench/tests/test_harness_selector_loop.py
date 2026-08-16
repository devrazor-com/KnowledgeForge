"""Windows-only: prove the harness child servers ACTUALLY ADOPT the Selector loop.

This starts a real Workbench child through the SAME `_regutil.start_server` the whole
HTTP harness uses, waits for readiness, and asserts its captured startup output reports
`_WindowsSelectorEventLoop`. It exercises the real launch path — a unit test of
`selector_loop_factory` would only prove construction, not adoption by the server the
harness actually spawns.

The external A/B already established the underlying mechanism (Proactor lost its listener
below 500 abortive connects; Selector survived 10,000), so this test does NOT re-prove
resilience — only that the harness adopted the already-validated loop. The Workbench app
prints `[workbench] serving on event loop: …` at startup, captured by start_server; the
mock Gateway is launched through the IDENTICAL start_server argv, so the Workbench child's
positive capture is direct proof and the mock's identical launch args are evidence it
receives the same loop configuration (no separate mock loop log is added).

Skipped off Windows: macOS/Linux never use the Proactor loop, so Selector adoption there
is not a question.
"""
import os
import socket
import sys

import pytest

from _regutil import _captured_output, start_server, stop_server, wait_ready

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows-only: Selector adoption matters only where the default loop is Proactor",
)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_windows_harness_child_serves_on_selector_loop(tmp_path):
    port = _free_port()
    env = os.environ.copy()
    env["WORKBENCH_DB"] = str(tmp_path / "wb.db")
    wb = start_server("workbench.app:app", port, env)
    try:
        assert wait_ready(port)
        out = _captured_output(port)
        assert "_WindowsSelectorEventLoop" in out, (
            "harness child did not report serving on the Selector loop.\n"
            f"--- captured child output ---\n{out}")
    finally:
        stop_server(wb)

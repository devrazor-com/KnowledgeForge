"""Event-loop selection contract: the factory returns a Selector loop, and the launcher
wires that same Selector factory unconditionally on every platform (no OS branch). These
do not verify the real Workbench serving on a Selector loop end to end — that is
test_harness_selector_loop.py (which now runs on every platform).
"""
import asyncio

import workbench.run_workbench as run_workbench
from workbench.eventloop import selector_loop_factory


def test_selector_factory_returns_a_selector_loop():
    loop = selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.selector_events.BaseSelectorEventLoop)
        proactor = getattr(asyncio, "ProactorEventLoop", None)   # exists only on Windows
        if proactor is not None:
            assert not isinstance(loop, proactor)
    finally:
        loop.close()


def test_launcher_uses_selector_loop_unconditionally(monkeypatch):
    """The launcher passes the Selector factory to uvicorn — unconditionally. There is no
    OS branch: main() reads no platform value, so the loop choice is identical on every
    platform. (The end-to-end proof that a child actually serves on a selector loop is
    test_harness_selector_loop.py, which runs on every OS.)"""
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(run_workbench.uvicorn, "run", fake_run)
    run_workbench.main()
    assert captured["app"] == "workbench.app:app"
    assert captured["loop"] == "workbench.eventloop:selector_loop_factory"

"""Narrow tests for the Windows Selector mitigation: the loop factory returns a selector
loop, and the launcher wires it in on Windows only. These do not (and cannot) verify the
real Workbench serving on _WindowsSelectorEventLoop — that is the Windows acceptance run.
"""
import asyncio

import workbench.run_workbench as run_workbench
from workbench.winloop import selector_loop_factory


def test_selector_factory_returns_a_selector_loop():
    loop = selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.selector_events.BaseSelectorEventLoop)
        proactor = getattr(asyncio, "ProactorEventLoop", None)   # exists only on Windows
        if proactor is not None:
            assert not isinstance(loop, proactor)
    finally:
        loop.close()


def test_launcher_forces_selector_loop_on_windows(monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(run_workbench.uvicorn, "run", fake_run)
    monkeypatch.setattr(run_workbench.sys, "platform", "win32")
    run_workbench.main()
    assert captured["app"] == "workbench.app:app"
    assert captured["loop"] == "workbench.winloop:selector_loop_factory"


def test_launcher_uses_default_loop_off_windows(monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(run_workbench.uvicorn, "run", fake_run)
    monkeypatch.setattr(run_workbench.sys, "platform", "darwin")
    run_workbench.main()
    assert "loop" not in captured   # uvicorn's default loop off Windows

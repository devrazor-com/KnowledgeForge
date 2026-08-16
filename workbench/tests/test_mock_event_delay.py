"""Dev-only mock knob: MOCK_EVENT_DELAY_SECONDS overrides the mock Gateway's event
pacing (default 0.6s). Used by the Windows Phase-D recovery acceptance to widen the
active-run window; no production Module 1 behaviour, contract, or timeout depends on it.
"""
from tools.mock_gateway.app import DEFAULT_EVENT_DELAY_SECONDS, _event_delay_seconds


def test_default_delay_is_preserved(monkeypatch):
    monkeypatch.delenv("MOCK_EVENT_DELAY_SECONDS", raising=False)
    assert _event_delay_seconds() == DEFAULT_EVENT_DELAY_SECONDS == 0.6


def test_env_override_is_honored(monkeypatch):
    monkeypatch.setenv("MOCK_EVENT_DELAY_SECONDS", "6")
    assert _event_delay_seconds() == 6.0


def test_bad_or_negative_value_falls_back_to_default(monkeypatch):
    for bad in ("not-a-number", "", "-2"):
        monkeypatch.setenv("MOCK_EVENT_DELAY_SECONDS", bad)
        assert _event_delay_seconds() == 0.6

"""Regression pin for TODAY's start-5xx physical-request behaviour.

Purpose (this is a PIN, not an endorsement that start should keep retrying):
 1. the cancel-specific `retry_5xx` change must not silently alter start retries;
 2. the pending start-semantics decision (awaiting the Gateway owner's answers) must
    DELIBERATELY change a test that records today's physical HTTP behaviour.

start currently retries a persistent 5xx through the shared `_gw_call` wrapper
(`retry_5xx` defaults True), so one logical start transmits RETRY_ATTEMPTS physical
`POST /runs` requests. This drives the REAL `orchestrator.start_run` call site and
counts physical transmissions by patching the function that issues them
(`gateway_client.start`) — each invocation is exactly one `POST /runs`. No HTTP server
or mock process is needed; the count is direct evidence of physical requests.
"""
import asyncio

from workbench import config, db, orchestrator


def _setup_larkspur_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "wb.db"))
    # larkspur-sandbox must be a currently-configured environment so start_run's env gate
    # is satisfied and execution reaches the (patched) start call being measured.
    envfile = tmp_path / "environments.txt"
    envfile.write_text("larkspur-sandbox\n", encoding="utf-8")
    monkeypatch.setenv("WORKBENCH_ENVIRONMENTS_FILE", str(envfile))
    monkeypatch.setattr(orchestrator, "RETRY_DELAY", 0)   # count is what matters; keep it fast
    db.init()
    db.set_validation_profile("larkspur", "larkspur-sandbox", ["filesystem"], "test")
    return config.PACKAGES_DIR / "larkspur"


def test_start_5xx_transmits_exactly_retry_attempts_physical_requests(tmp_path, monkeypatch):
    """The real start path: a persistent 5xx transmits RETRY_ATTEMPTS physical POST /runs
    (today = 3). If start_run ever passes retry_5xx=False, this drops to 1 and fails —
    which is the point: changing start semantics must be a deliberate edit to this pin."""
    root = _setup_larkspur_profile(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_start(request, forced=None, fault=None, timeout=None):
        calls["n"] += 1
        return 503, {"error": "injected internal server error at start"}

    monkeypatch.setattr(orchestrator.gateway_client, "start", fake_start)
    run_id = asyncio.run(orchestrator.start_run(
        root, "larkspur", "larkspur", "LARK-TASK-001", ["filesystem"], "larkspur-sandbox", None))

    assert calls["n"] == orchestrator.RETRY_ATTEMPTS == 3     # three physical POST /runs
    run = db.get_run(run_id)
    assert run["error_kind"] == "gateway_http_error"          # persistent 5xx terminal state


def test_gw_call_retry_boundary_start_three_cancel_one(monkeypatch):
    """Guards the start/cancel boundary at the wrapper directly: a 5xx is retried three
    times under start's configuration (default retry_5xx=True) and exactly once under
    cancel's (retry_5xx=False). If the cancel change ever leaked into the start default,
    the first assertion breaks."""
    monkeypatch.setattr(orchestrator, "RETRY_DELAY", 0)

    def make_counter():
        c = {"n": 0}

        def fn(*args, timeout=None):
            c["n"] += 1
            return 503, {"error": "x"}
        return c, fn

    c_start, fn_start = make_counter()
    asyncio.run(orchestrator._gw_call(fn_start, None))                     # start config
    assert c_start["n"] == orchestrator.RETRY_ATTEMPTS == 3

    c_cancel, fn_cancel = make_counter()
    asyncio.run(orchestrator._gw_call(fn_cancel, None, retry_5xx=False))   # cancel config
    assert c_cancel["n"] == 1

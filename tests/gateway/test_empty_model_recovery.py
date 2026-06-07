"""Regression tests for empty model recovery after interrupted gateway turns."""

import threading

import gateway.run as gateway_run
from run_agent import AIAgent


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._service_tier = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


def _patch_resolution(monkeypatch, *, model_from_config: str, provider: str = "openai-codex"):
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg=None: model_from_config)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": provider,
            "api_key": "x",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_responses",
        },
    )


def test_normal_turn_caches_last_resolved_model(monkeypatch):
    _patch_resolution(monkeypatch, model_from_config="gpt-5.5")
    runner = _make_runner()
    sk = "agent:main:discord:dm:123"

    model, _ = runner._resolve_session_agent_runtime(
        session_key=sk,
        user_config={"model": {"default": "x"}},
    )

    assert model == "gpt-5.5"
    assert runner._last_resolved_model[sk] == "gpt-5.5"
    assert runner._last_resolved_model["*"] == "gpt-5.5"


def test_empty_model_recovers_session_last_good(monkeypatch):
    runner = _make_runner()
    sk = "agent:main:discord:dm:123"

    _patch_resolution(monkeypatch, model_from_config="gpt-5.5")
    runner._resolve_session_agent_runtime(session_key=sk, user_config={"model": {"default": "x"}})

    _patch_resolution(monkeypatch, model_from_config="", provider="")
    model, _ = runner._resolve_session_agent_runtime(session_key=sk, user_config={})

    assert model == "gpt-5.5"


def test_empty_model_new_session_recovers_global_last_good(monkeypatch):
    runner = _make_runner()

    _patch_resolution(monkeypatch, model_from_config="gpt-5.5")
    runner._resolve_session_agent_runtime(
        session_key="agent:main:discord:dm:111",
        user_config={"model": {}},
    )

    _patch_resolution(monkeypatch, model_from_config="", provider="")
    model, _ = runner._resolve_session_agent_runtime(
        session_key="agent:main:discord:dm:999",
        user_config={},
    )

    assert model == "gpt-5.5"


def test_cold_start_empty_model_does_not_crash(monkeypatch):
    _patch_resolution(monkeypatch, model_from_config="", provider="")
    runner = _make_runner()

    model, _ = runner._resolve_session_agent_runtime(
        session_key="agent:main:discord:dm:1",
        user_config={},
    )

    assert model == ""


def test_bare_runner_without_cache_attr_does_not_crash(monkeypatch):
    _patch_resolution(monkeypatch, model_from_config="gpt-5.5")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    runner._service_tier = None

    model, _ = runner._resolve_session_agent_runtime(session_key="x", user_config={"model": {}})

    assert model == "gpt-5.5"


def test_has_pending_fallback_false_without_chain():
    agent = object.__new__(AIAgent)

    assert agent._has_pending_fallback() is False


def test_has_pending_fallback_tracks_chain_index():
    agent = object.__new__(AIAgent)
    agent._fallback_chain = ["openrouter", "anthropic"]
    agent._fallback_index = 1

    assert agent._has_pending_fallback() is True

    agent._fallback_index = 2
    assert agent._has_pending_fallback() is False

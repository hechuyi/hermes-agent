"""Regression tests for gateway max_tokens propagation."""

from unittest.mock import patch


def test_runtime_agent_kwargs_reads_model_max_tokens(monkeypatch):
    from gateway import run as grun

    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)
    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
        }),
        patch("hermes_cli.runtime_provider._get_model_config", return_value={
            "max_tokens": 16384,
        }),
    ):
        kwargs = grun._resolve_runtime_agent_kwargs()

    assert kwargs["max_tokens"] == 16384


def test_runtime_agent_kwargs_env_override_wins(monkeypatch):
    from gateway import run as grun

    monkeypatch.setenv("HERMES_MAX_TOKENS", "2048")
    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
            "max_output_tokens": 12000,
        }),
        patch("hermes_cli.runtime_provider._get_model_config", return_value={
            "max_tokens": 16384,
        }),
    ):
        kwargs = grun._resolve_runtime_agent_kwargs()

    assert kwargs["max_tokens"] == 2048


def test_runtime_agent_kwargs_uses_provider_max_output_fallback(monkeypatch):
    from gateway import run as grun

    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)
    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
            "max_output_tokens": 12000,
        }),
        patch("hermes_cli.runtime_provider._get_model_config", return_value={}),
    ):
        kwargs = grun._resolve_runtime_agent_kwargs()

    assert kwargs["max_tokens"] == 12000


def test_turn_agent_config_carries_max_tokens_in_runtime_and_signature():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._service_tier = None
    route = GatewayRunner._resolve_turn_agent_config(
        runner,
        "hi",
        "gpt-5.4",
        {
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
            "max_tokens": 4096,
        },
    )

    assert route["runtime"]["max_tokens"] == 4096
    assert 4096 in route["signature"]


def test_session_model_override_preserves_max_tokens():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "feishu:chat": {
            "model": "gpt-5.4",
            "provider": "custom",
            "api_key": "override-key",
            "base_url": "https://override.example.com/v1",
            "api_mode": "chat_completions",
            "max_tokens": 8192,
        },
    }

    model, runtime = GatewayRunner._apply_session_model_override(
        runner,
        "feishu:chat",
        "old-model",
        {"provider": "openrouter", "max_tokens": 4096},
    )

    assert model == "gpt-5.4"
    assert runtime["max_tokens"] == 8192

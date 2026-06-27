"""Regression coverage for typed /model routing to configured providers."""

from hermes_cli.model_switch import switch_model


_ACCEPTED = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


def _patch_switch_dependencies(monkeypatch, *, validation=_ACCEPTED):
    monkeypatch.setattr("hermes_cli.model_switch.resolve_alias", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.list_provider_models", lambda *a, **k: [])
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.models.detect_provider_for_model", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.models.validate_requested_model", lambda *a, **k: validation)
    monkeypatch.setattr(
        "hermes_cli.model_switch.normalize_model_for_provider",
        lambda model, provider: model,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": "configured-key",
            "base_url": "https://configured.example/v1",
            "api_mode": "chat_completions",
        },
    )


def test_typed_model_routes_to_user_configured_provider(monkeypatch):
    _patch_switch_dependencies(monkeypatch)

    result = switch_model(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        current_base_url="https://chatgpt.com/backend-api/codex",
        user_providers={
            "local-relay": {
                "base_url": "http://localhost:11434/v1",
                "models": ["qwen3.5-4b"],
            }
        },
    )

    assert result.success is True, result.error_message
    assert result.target_provider == "local-relay"
    assert result.new_model == "qwen3.5-4b"


def test_typed_model_routes_to_custom_provider(monkeypatch):
    _patch_switch_dependencies(monkeypatch)

    result = switch_model(
        raw_input="local-model",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        current_base_url="https://chatgpt.com/backend-api/codex",
        custom_providers=[
            {
                "name": "my-gateway",
                "base_url": "https://gateway.example/v1",
                "models": {"local-model": {"context_length": 32768}},
            }
        ],
    )

    assert result.success is True, result.error_message
    assert result.target_provider == "custom:my-gateway"
    assert result.new_model == "local-model"


def test_ambiguous_configured_model_requires_explicit_provider(monkeypatch):
    _patch_switch_dependencies(monkeypatch)

    result = switch_model(
        raw_input="shared-model",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers={
            "relay-a": {"models": ["shared-model"]},
            "relay-b": {"models": ["shared-model"]},
        },
    )

    assert result.success is False
    assert "--provider" in result.error_message
    assert "relay-a" in result.error_message
    assert "relay-b" in result.error_message

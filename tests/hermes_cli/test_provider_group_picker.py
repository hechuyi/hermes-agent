"""Tests for provider groups in the CLI model picker."""

from __future__ import annotations


def test_select_provider_group_drills_down_to_concrete_provider(monkeypatch):
    from hermes_cli import main as main_mod

    recorded: dict[str, str] = {}
    prompts: list[list[str]] = []

    monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda *_a, **_k: "openrouter")
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers",
        lambda _config: [],
    )
    monkeypatch.setattr(
        "hermes_cli.providers.resolve_provider_full",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "m", "provider": "openrouter"}},
    )
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})

    def fake_prompt(choices, default=0):
        labels = list(choices)
        prompts.append(labels)
        if len(prompts) == 1:
            return next(i for i, label in enumerate(labels) if label.startswith("MiniMax"))
        return next(i for i, label in enumerate(labels) if "China" in label)

    def fake_generic_flow(_config, provider_id, current_model=""):
        recorded["provider_id"] = provider_id
        recorded["current_model"] = current_model

    monkeypatch.setattr(main_mod, "_prompt_provider_choice", fake_prompt)
    monkeypatch.setattr(main_mod, "_model_flow_api_key_provider", fake_generic_flow)
    monkeypatch.setattr(main_mod, "_clear_stale_openai_base_url", lambda: None)

    main_mod.select_provider_and_model()

    assert recorded == {"provider_id": "minimax-cn", "current_model": "m"}
    assert any(label.startswith("MiniMax") and "▸" in label for label in prompts[0])
    assert any("MiniMax China" in label for label in prompts[1])

"""Regression tests for live custom-provider metadata in aux routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _runtime_globals(mod):
    return {
        "provider": mod._RUNTIME_MAIN_PROVIDER,
        "model": mod._RUNTIME_MAIN_MODEL,
        "base_url": mod._RUNTIME_MAIN_BASE_URL,
        "key": mod._RUNTIME_MAIN_API_KEY,
        "api_mode": mod._RUNTIME_MAIN_API_MODE,
    }


def _client_base_url(client):
    for chain in (("base_url",), ("_client", "base_url")):
        obj = client
        try:
            for attr in chain:
                obj = getattr(obj, attr)
            return str(obj).rstrip("/")
        except AttributeError:
            continue
    return ""


class TestSetRuntimeMainCustomProvider:
    def test_stores_custom_provider_runtime_fields(self):
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:router",
                "glm-5.1",
                base_url="https://router.example.com/v1",
                api_key="sk-runtime",
                api_mode="chat_completions",
            )

            assert _runtime_globals(mod) == {
                "provider": "custom:router",
                "model": "glm-5.1",
                "base_url": "https://router.example.com/v1",
                "key": "sk-runtime",
                "api_mode": "chat_completions",
            }
        finally:
            mod.clear_runtime_main()

    def test_clear_runtime_main_resets_custom_fields(self):
        import agent.auxiliary_client as mod

        mod.set_runtime_main(
            "custom:router",
            "glm-5.1",
            base_url="https://router.example.com/v1",
            api_key="sk-runtime",
            api_mode="chat_completions",
        )
        mod.clear_runtime_main()

        assert set(_runtime_globals(mod).values()) == {""}

    def test_resolve_auto_uses_runtime_globals_when_main_runtime_missing(self):
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:router",
                "glm-5.1",
                base_url="https://router.example.com/v1",
                api_key="sk-runtime",
                api_mode="chat_completions",
            )
            with patch.object(mod, "resolve_provider_client") as resolve:
                resolve.return_value = (MagicMock(), "glm-5.1")

                mod._resolve_auto(main_runtime=None)

            kwargs = resolve.call_args.kwargs
            assert resolve.call_args.args[:2] == ("custom", "glm-5.1")
            assert kwargs["explicit_base_url"] == "https://router.example.com/v1"
            assert kwargs["explicit_api_key"] == "sk-runtime"
            assert kwargs["api_mode"] == "chat_completions"
        finally:
            mod.clear_runtime_main()

    def test_explicit_main_runtime_takes_precedence_over_globals(self):
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:global",
                "global-model",
                base_url="https://global.example.com/v1",
                api_key="sk-global",
                api_mode="chat_completions",
            )
            with patch.object(mod, "resolve_provider_client") as resolve:
                resolve.return_value = (MagicMock(), "dict-model")
                mod._resolve_auto(
                    main_runtime={
                        "provider": "custom:dict",
                        "model": "dict-model",
                        "base_url": "https://dict.example.com/v1",
                        "api_key": "sk-dict",
                        "api_mode": "anthropic_messages",
                    }
                )

            kwargs = resolve.call_args.kwargs
            assert resolve.call_args.args[:2] == ("custom", "dict-model")
            assert kwargs["explicit_base_url"] == "https://dict.example.com/v1"
            assert kwargs["explicit_api_key"] == "sk-dict"
            assert kwargs["api_mode"] == "anthropic_messages"
        finally:
            mod.clear_runtime_main()


class TestResolveAutoCustomEndToEnd:
    def test_configless_custom_endpoint_routes_via_runtime_global(
        self,
        tmp_path,
        monkeypatch,
    ):
        import agent.auxiliary_client as mod

        for var in (
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "NOUS_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:ephemeral'\n"
            "  base_url: ''\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:ephemeral",
                "glm-5.1",
                base_url="https://ephemeral.example.com/v1",
                api_key="sk-live",
            )

            client, resolved = mod.resolve_provider_client("auto", None)

            assert client is not None
            assert resolved == "glm-5.1"
            assert _client_base_url(client) == "https://ephemeral.example.com/v1"
        finally:
            mod.clear_runtime_main()

    def test_named_custom_config_entry_still_routes(self, tmp_path, monkeypatch):
        import agent.auxiliary_client as mod

        for var in (
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "NOUS_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:openclaw'\n"
            "custom_providers:\n"
            "  - name: openclaw\n"
            "    base_url: 'https://withcfg.example.com/v1'\n"
            "    model: glm-5.1\n"
            "    api_key: cfg-key\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main("custom:openclaw", "glm-5.1")

            client, resolved = mod.resolve_provider_client("auto", None)

            assert client is not None
            assert resolved == "glm-5.1"
            assert _client_base_url(client) == "https://withcfg.example.com/v1"
        finally:
            mod.clear_runtime_main()

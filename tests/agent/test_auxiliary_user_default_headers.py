"""Tests for user-configured model.default_headers in auxiliary clients."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("model:\n  default: test-model\n")


def _write_config(tmp_path, config_dict):
    import yaml

    (tmp_path / ".hermes" / "config.yaml").write_text(yaml.dump(config_dict))


class TestApplyUserDefaultHeadersHelper:
    def test_user_headers_merged_and_win(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "m",
                "default_headers": {
                    "User-Agent": "curl/8.7.1",
                    "X-Extra": "1",
                },
            },
        })
        from agent.auxiliary_client import _apply_user_default_headers

        merged = _apply_user_default_headers({"User-Agent": "OpenAI/Python 2.24.0"})

        assert merged["User-Agent"] == "curl/8.7.1"
        assert merged["X-Extra"] == "1"

    def test_no_config_is_noop_returns_original(self, tmp_path):
        _write_config(tmp_path, {"model": {"default": "m"}})
        from agent.auxiliary_client import _apply_user_default_headers

        original = {"User-Agent": "OpenAI/Python"}

        assert _apply_user_default_headers(original) == original

    def test_none_headers_with_config_creates_dict(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "m",
                "default_headers": {"User-Agent": "curl/8.7.1"},
            },
        })
        from agent.auxiliary_client import _apply_user_default_headers

        assert _apply_user_default_headers(None) == {"User-Agent": "curl/8.7.1"}

    def test_none_values_skipped(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "m",
                "default_headers": {
                    "User-Agent": "curl/8.7.1",
                    "X-Drop": None,
                },
            },
        })
        from agent.auxiliary_client import _apply_user_default_headers

        merged = _apply_user_default_headers({})

        assert merged == {"User-Agent": "curl/8.7.1"}


class TestAuxClientHonorsUserDefaultHeaders:
    def test_custom_provider_overrides_sdk_user_agent(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "my-custom-model",
                "provider": "custom",
                "base_url": "http://localhost:8080/v1",
                "default_headers": {
                    "User-Agent": "curl/8.7.1",
                    "X-Extra": "1",
                },
            },
        })

        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, _ = resolve_provider_client("main", "my-custom-model")

        assert client is not None
        headers = mock_openai.call_args.kwargs.get("default_headers", {})
        assert headers["User-Agent"] == "curl/8.7.1"
        assert headers["X-Extra"] == "1"

    def test_custom_provider_no_override_sends_no_user_agent(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "my-custom-model",
                "provider": "custom",
                "base_url": "http://localhost:8080/v1",
            },
        })

        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, _ = resolve_provider_client("main", "my-custom-model")

        assert client is not None
        headers = mock_openai.call_args.kwargs.get("default_headers", {}) or {}
        assert "User-Agent" not in headers

    def test_named_custom_provider_honors_override(self, tmp_path):
        _write_config(tmp_path, {
            "model": {
                "default": "test-model",
                "default_headers": {"User-Agent": "curl/8.7.1"},
            },
            "custom_providers": [
                {"name": "my-gw", "base_url": "http://my-gw.local/v1", "api_key": "k"},
            ],
        })

        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, _ = resolve_provider_client("my-gw", "test-model")

        assert client is not None
        headers = mock_openai.call_args.kwargs.get("default_headers", {}) or {}
        assert headers["User-Agent"] == "curl/8.7.1"

"""Regression tests for OpenAI/OpenRouter model-picker bugs."""

from unittest.mock import patch

from hermes_cli import models as M
from hermes_cli.providers import HERMES_OVERLAYS


def test_openrouter_overlay_does_not_list_openai_api_key():
    overlay = HERMES_OVERLAYS["openrouter"]
    assert "OPENAI_API_KEY" not in overlay.extra_env_vars


def test_default_openai_endpoint_filters_to_curated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    curated = M._PROVIDER_MODELS["openai-api"]
    live = list(curated) + [
        "text-embedding-3-large",
        "whisper-1",
        "tts-1",
        "dall-e-3",
        "gpt-3.5-turbo",
        "davinci-002",
        "omni-moderation-latest",
    ]
    with patch.object(M, "fetch_api_models", return_value=live):
        result = M.provider_model_ids("openai-api", force_refresh=True)

    assert result == list(curated)


def test_default_openai_endpoint_intersects_account_access(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    curated = M._PROVIDER_MODELS["openai-api"]
    live = list(curated[:2]) + ["text-embedding-3-large", "whisper-1"]
    with patch.object(M, "fetch_api_models", return_value=live):
        result = M.provider_model_ids("openai-api", force_refresh=True)

    assert result == list(curated[:2])


def test_default_openai_endpoint_falls_back_when_no_curated_access(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    curated = M._PROVIDER_MODELS["openai-api"]
    live = ["text-embedding-3-large", "whisper-1", "tts-1"]
    with patch.object(M, "fetch_api_models", return_value=live):
        result = M.provider_model_ids("openai-api", force_refresh=True)

    assert result == list(curated)


def test_custom_openai_compatible_endpoint_keeps_live_list(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://my-proxy.example.com/v1")

    live = ["custom-model-a", "custom-model-b", "some-embedding-model"]
    with patch.object(M, "fetch_api_models", return_value=live):
        result = M.provider_model_ids("openai-api", force_refresh=True)

    assert result == live

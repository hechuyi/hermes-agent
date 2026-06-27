"""Regression tests for provider live-model fetch base_url overrides."""

import json
from unittest.mock import MagicMock, patch

from providers.base import ProviderProfile


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"data": [{"id": "proxy-model"}]}).encode()


def test_provider_profile_fetch_models_uses_base_url_override():
    profile = ProviderProfile(name="test", base_url="https://default.example/v1")

    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        models = profile.fetch_models(
            api_key="sk-test",
            base_url="https://proxy.example/v1",
        )

    assert models == ["proxy-model"]
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://proxy.example/v1/models"


def test_provider_profile_models_url_still_wins_over_base_url_override():
    profile = ProviderProfile(
        name="test",
        base_url="https://default.example/v1",
        models_url="https://catalog.example/models",
    )

    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        models = profile.fetch_models(
            api_key="sk-test",
            base_url="https://proxy.example/v1",
        )

    assert models == ["proxy-model"]
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://catalog.example/models"


def test_provider_model_ids_passes_resolved_base_url_to_profile_fetch():
    mock_profile = MagicMock()
    mock_profile.auth_type = "api_key"
    mock_profile.base_url = "https://default.example/v1"
    mock_profile.fetch_models.return_value = ["live-model"]
    mock_profile.fallback_models = None

    with (
        patch("providers.get_provider_profile", return_value=mock_profile),
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "api_key": "sk-test",
                "base_url": "https://proxy.example/v1",
            },
        ),
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"test-provider": []}),
    ):
        from hermes_cli.models import provider_model_ids

        assert provider_model_ids("test-provider") == ["live-model"]

    mock_profile.fetch_models.assert_called_once_with(
        api_key="sk-test",
        base_url="https://proxy.example/v1",
    )

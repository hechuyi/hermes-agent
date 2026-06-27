"""Regression tests for merging live model catalogs with curated entries."""

from unittest.mock import MagicMock, patch


def _profile(live_models):
    p = MagicMock()
    p.auth_type = "api_key"
    p.base_url = "https://api.example/v1"
    p.fetch_models.return_value = live_models
    p.fallback_models = None
    return p


def test_generic_provider_catalog_is_curated_first_with_live_extras():
    with (
        patch("providers.get_provider_profile", return_value=_profile(["live-only", "glm-5"])),
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "sk-test", "base_url": ""},
        ),
        patch.dict(
            "hermes_cli.models._PROVIDER_MODELS",
            {"zai": ["glm-5.1", "glm-5", "glm-4.5"]},
        ),
        patch("hermes_cli.models._merge_with_models_dev", side_effect=lambda _p, curated: curated),
    ):
        from hermes_cli.models import provider_model_ids

        result = provider_model_ids("zai")

    assert result == ["glm-5.1", "glm-5", "glm-4.5", "live-only"]


def test_anthropic_provider_catalog_keeps_curated_aliases_when_live_omits_them():
    curated = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    live = ["claude-sonnet-4-6", "claude-live-only"]

    with (
        patch.object(__import__("hermes_cli.models").models, "_fetch_anthropic_models", return_value=live),
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"anthropic": curated}),
    ):
        from hermes_cli.models import provider_model_ids

        result = provider_model_ids("anthropic")

    assert result == curated + ["claude-live-only"]


def test_validate_requested_model_accepts_curated_model_omitted_by_live_api():
    with (
        patch("hermes_cli.models.fetch_api_models", return_value=["glm-5"]),
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": ["glm-5.1", "glm-5"]}),
        patch("hermes_cli.models._merge_with_models_dev", side_effect=lambda _p, curated: curated),
    ):
        from hermes_cli.models import validate_requested_model

        result = validate_requested_model("glm-5.1", "zai", api_key="sk-test")

    assert result["accepted"] is True
    assert result["recognized"] is True
    assert "curated catalog" in (result["message"] or "")


def test_validate_requested_model_rejects_model_from_other_provider_catalog():
    with (
        patch("hermes_cli.models.fetch_api_models", return_value=["glm-5"]),
        patch.dict(
            "hermes_cli.models._PROVIDER_MODELS",
            {"zai": ["glm-5.1", "glm-5"], "openrouter": ["other-provider-model"]},
        ),
        patch("hermes_cli.models._merge_with_models_dev", side_effect=lambda _p, curated: curated),
    ):
        from hermes_cli.models import validate_requested_model

        result = validate_requested_model("other-provider-model", "zai", api_key="sk-test")

    assert result["accepted"] is False

"""Unit tests for the MiniMax provider profile.

MiniMax's direct API, China API, and OAuth profile should each advertise a
stable ``default_aux_model``. The direct profiles should point at M3, while
OAuth stays on plain M2.7 because the Coding Plan tier does not expose M3.
"""

from __future__ import annotations

import pytest


@pytest.fixture(params=["minimax", "minimax-cn", "minimax-oauth"])
def minimax_profile(request):
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile(request.param)
    assert profile is not None, f"{request.param} provider profile must be registered"
    return profile, request.param


class TestMinimaxAuxModelM3:
    @pytest.mark.parametrize(
        "provider_id,expected",
        [
            ("minimax", "MiniMax-M3"),
            ("minimax-cn", "MiniMax-M3"),
            ("minimax-oauth", "MiniMax-M2.7"),
        ],
    )
    def test_profile_advertises_expected_aux_model(self, provider_id, expected):
        import model_tools  # noqa: F401
        import providers

        profile = providers.get_provider_profile(provider_id)
        assert profile is not None
        assert profile.default_aux_model == expected

    def test_consumer_api_returns_non_empty_for_each_provider(self, minimax_profile):
        from agent.auxiliary_client import _get_aux_model_for_provider

        profile, provider_id = minimax_profile
        resolved = _get_aux_model_for_provider(provider_id)
        assert resolved != ""
        assert resolved == profile.default_aux_model


class TestMinimaxAuxModelNotHighspeed:
    @pytest.mark.parametrize("provider_id", ["minimax", "minimax-cn", "minimax-oauth"])
    def test_default_aux_model_is_not_highspeed(self, provider_id):
        import model_tools  # noqa: F401
        import providers

        profile = providers.get_provider_profile(provider_id)
        assert profile is not None
        assert "highspeed" not in profile.default_aux_model.lower()

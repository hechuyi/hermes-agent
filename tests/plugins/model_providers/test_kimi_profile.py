"""Kimi/Moonshot provider reasoning parameter contract."""

from __future__ import annotations

import pytest


@pytest.fixture
def kimi_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("kimi-coding")
    assert profile is not None
    return profile


class TestKimiReasoningWireShape:
    def test_no_config_enables_thinking_without_effort(self, kimi_profile):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(reasoning_config=None)
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_explicit_effort_sends_effort_only(self, kimi_profile, effort):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": effort}

    def test_enabled_without_effort_falls_back_to_thinking(self, kimi_profile):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True}
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    @pytest.mark.parametrize("effort", ["", "garbage", "xhigh", "max"])
    def test_unrecognized_effort_falls_back_to_thinking(self, kimi_profile, effort):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    def test_disabled_sends_thinking_disabled_only(self, kimi_profile):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"}
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}

    @pytest.mark.parametrize(
        "reasoning_config",
        [
            None,
            {"enabled": True},
            {"enabled": True, "effort": "high"},
            {"enabled": True, "effort": "garbage"},
            {"enabled": False},
            {"enabled": False, "effort": "low"},
        ],
    )
    def test_never_emits_both(self, kimi_profile, reasoning_config):
        extra_body, top_level = kimi_profile.build_api_kwargs_extras(
            reasoning_config=reasoning_config
        )
        assert not ("thinking" in extra_body and "reasoning_effort" in top_level)


class TestKimiFullKwargsIntegration:
    def _build(self, kimi_profile, reasoning_config):
        from agent.transports.chat_completions import ChatCompletionsTransport

        return ChatCompletionsTransport().build_kwargs(
            model="kimi-k2-turbo-preview",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=kimi_profile,
            reasoning_config=reasoning_config,
            base_url="https://api.moonshot.ai/v1",
            provider_name="kimi-coding",
        )

    def test_explicit_effort_omits_thinking(self, kimi_profile):
        kwargs = self._build(kimi_profile, {"enabled": True, "effort": "high"})
        assert kwargs["reasoning_effort"] == "high"
        assert "thinking" not in kwargs.get("extra_body", {})

    def test_no_config_omits_effort(self, kimi_profile):
        kwargs = self._build(kimi_profile, None)
        assert "reasoning_effort" not in kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

"""Unit tests for the Ollama Cloud provider profile's reasoning-effort wiring."""

from __future__ import annotations

import pytest


@pytest.fixture
def ollama_cloud_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("ollama-cloud")
    assert profile is not None
    return profile


class TestOllamaCloudReasoningWireShape:
    def test_xhigh_maps_to_max(self, ollama_cloud_profile):
        extra_body, top_level = ollama_cloud_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"}
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "max"}

    def test_disabled_and_none_emit_nothing(self, ollama_cloud_profile):
        for reasoning_config in (
            {"enabled": False},
            {"enabled": True, "effort": "none"},
        ):
            extra_body, top_level = ollama_cloud_profile.build_api_kwargs_extras(
                reasoning_config=reasoning_config
            )
            assert extra_body == {}
            assert top_level == {}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_standard_efforts_pass_through(self, ollama_cloud_profile, effort):
        _, top_level = ollama_cloud_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}
        )
        assert top_level == {"reasoning_effort": effort}


class TestOllamaCloudTransportIntegration:
    def test_full_kwargs_include_reasoning_effort(self, ollama_cloud_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v4-pro:cloud",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=ollama_cloud_profile,
            reasoning_config={"enabled": True, "effort": "xhigh"},
            base_url="https://ollama.com/v1",
            provider_name="ollama-cloud",
        )

        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["model"] == "deepseek-v4-pro:cloud"


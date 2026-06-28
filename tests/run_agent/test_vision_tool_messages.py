"""Tests for proactive vision-tool-message downgrade (#41072)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_agent(provider="openrouter", model="gpt-4o"):
    from run_agent import AIAgent

    agent = MagicMock(spec=AIAgent)
    agent.provider = provider
    agent.model = model
    agent._no_list_tool_content_models = set()
    agent._content_has_image_parts = lambda content: isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
        for part in content
    )
    agent._model_supports_vision = lambda: AIAgent._model_supports_vision(agent)
    agent._provider_supports_vision_tool_messages = (
        lambda: AIAgent._provider_supports_vision_tool_messages(agent)
    )
    agent._tool_result_content_for_active_model = (
        lambda name, result: AIAgent._tool_result_content_for_active_model(agent, name, result)
    )
    return agent


def _multimodal_result(text="screenshot", image_url="data:image/png;base64,AAAA"):
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "text_summary": text,
    }


class TestProviderSupportsVisionToolMessages:
    def test_xiaomi_returns_false(self):
        agent = _make_agent("xiaomi", "mimo-v2.5")
        assert agent._provider_supports_vision_tool_messages() is False

    def test_unknown_provider_defaults_true(self):
        agent = _make_agent("some-unknown-provider", "model-v1")
        assert agent._provider_supports_vision_tool_messages() is True

    def test_openrouter_defaults_true(self):
        agent = _make_agent("openrouter", "gpt-4o")
        assert agent._provider_supports_vision_tool_messages() is True


class TestToolResultContentProactiveDowngrade:
    def test_xiaomi_downgrades_to_text_summary(self):
        agent = _make_agent("xiaomi", "mimo-v2.5")
        result = _multimodal_result(text="screenshot captured")

        with patch.object(agent, "_model_supports_vision", return_value=True):
            content = agent._tool_result_content_for_active_model("browser_screenshot", result)

        assert isinstance(content, str)
        assert "screenshot captured" in content

    def test_openrouter_vision_keeps_list_content(self):
        agent = _make_agent("openrouter", "gpt-4o")
        result = _multimodal_result()

        with patch.object(agent, "_model_supports_vision", return_value=True):
            content = agent._tool_result_content_for_active_model("browser_screenshot", result)

        assert isinstance(content, list)
        assert any(p.get("type") == "image_url" for p in content if isinstance(p, dict))

    def test_non_vision_model_gets_text_summary(self):
        agent = _make_agent("openrouter", "gpt-3.5-turbo")
        result = _multimodal_result(text="screenshot")

        with patch.object(agent, "_model_supports_vision", return_value=False):
            content = agent._tool_result_content_for_active_model("browser_screenshot", result)

        assert isinstance(content, str)
        assert "screenshot" in content

    def test_reactive_cache_still_works(self):
        agent = _make_agent("openrouter", "some-model")
        agent._no_list_tool_content_models = {("openrouter", "some-model")}
        result = _multimodal_result(text="cached downgrade")

        with patch.object(agent, "_model_supports_vision", return_value=True):
            content = agent._tool_result_content_for_active_model("browser_screenshot", result)

        assert isinstance(content, str)
        assert "cached downgrade" in content


class TestProviderProfileField:
    def test_default_is_true(self):
        from providers.base import ProviderProfile
        import dataclasses

        fields = {f.name: f.default for f in dataclasses.fields(ProviderProfile)}
        assert fields.get("supports_vision_tool_messages", True) is True

    def test_xiaomi_profile_has_false(self):
        from providers import get_provider_profile

        profile = get_provider_profile("xiaomi")
        assert profile is not None
        assert profile.supports_vision_tool_messages is False

    def test_xiaomi_alias_mimo_has_false(self):
        from providers import get_provider_profile

        profile = get_provider_profile("mimo")
        assert profile is not None
        assert profile.supports_vision_tool_messages is False

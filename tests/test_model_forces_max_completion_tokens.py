"""Tests for utils.model_forces_max_completion_tokens."""

from utils import model_forces_max_completion_tokens


class TestModelForcesMaxCompletionTokens:
    def test_positive_openai_families(self):
        for model in (
            "gpt-5",
            "gpt-5.4",
            "gpt-5-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o1-preview",
            "o3-mini",
            "o4-mini",
        ):
            assert model_forces_max_completion_tokens(model) is True

    def test_negative_families(self):
        for model in (
            "gpt-3.5-turbo",
            "gpt-4",
            "gpt-4-turbo",
            "claude-sonnet-4-6",
            "llama3",
            "mistral-7b-instruct",
            "qwen2.5-72b",
            "deepseek-chat",
        ):
            assert model_forces_max_completion_tokens(model) is False

    def test_vendor_prefix_and_case_are_normalized(self):
        assert model_forces_max_completion_tokens("openai/GPT-5.4") is True
        assert model_forces_max_completion_tokens("openai/gpt-4o-mini") is True
        assert model_forces_max_completion_tokens("anthropic/claude-3-opus") is False

    def test_empty_and_substring_cases_do_not_match(self):
        assert model_forces_max_completion_tokens(None) is False
        assert model_forces_max_completion_tokens("") is False
        assert model_forces_max_completion_tokens("local-gpt-5-clone") is False
        assert model_forces_max_completion_tokens("omni-chat") is False

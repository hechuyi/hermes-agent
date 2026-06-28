"""Regression tests for stream-empty and output-cap parsing behavior."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.model_metadata import parse_available_output_tokens_from_error


class TestParseOpenRouterOutputCap:
    def test_openrouter_breakdown_format(self):
        msg = (
            "This endpoint's maximum context length is 200000 tokens. "
            "However, you requested about 195000 tokens "
            "(150000 of text input, 40000 of tool input, 5000 in the output)."
        )
        assert parse_available_output_tokens_from_error(msg) == 10000

    def test_anthropic_format_still_works(self):
        msg = (
            "max_tokens: 32768 > context_window: 200000 - "
            "input_tokens: 190000 = available_tokens: 10000"
        )
        assert parse_available_output_tokens_from_error(msg) == 10000

    def test_non_output_cap_error_returns_none(self):
        assert parse_available_output_tokens_from_error("some unrelated 400 error") is None

    def test_breakdown_with_no_room_returns_none(self):
        msg = (
            "maximum context length is 1000 tokens "
            "(900 of text input, 200 of tool input, 0 in the output)"
        )
        assert parse_available_output_tokens_from_error(msg) is None


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


class TestEmptyStreamGuard:
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_empty_stream_raises_runtime_error(self, _mock_close, mock_create):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter([])
        mock_create.return_value = mock_client

        agent = _make_agent()

        with pytest.raises(RuntimeError, match="empty stream"):
            agent._interruptible_streaming_api_call({})

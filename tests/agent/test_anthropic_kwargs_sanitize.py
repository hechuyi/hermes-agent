"""Anthropic Messages dispatch boundary sanitization."""

import logging
from unittest.mock import MagicMock

import pytest


def _fake_anthropic_call(**kwargs):
    allowed = {
        "model",
        "messages",
        "max_tokens",
        "system",
        "tools",
        "tool_choice",
        "extra_body",
        "extra_headers",
        "temperature",
        "top_p",
        "top_k",
        "thinking",
        "timeout",
    }
    bad = set(kwargs) - allowed
    if bad:
        raise TypeError(
            "Messages.create() got an unexpected keyword argument "
            f"{sorted(bad)[0]!r}"
        )
    return "OK"


def test_bare_responses_payload_reproduces_sdk_typeerror():
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        _fake_anthropic_call(model="claude-sonnet-4-6", instructions="sys")


def test_sanitize_anthropic_kwargs_strips_responses_only_keys():
    from agent.anthropic_adapter import sanitize_anthropic_kwargs

    payload = {
        "model": "claude-sonnet-4-6",
        "instructions": "You are Hermes.",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "parallel_tool_calls": True,
    }
    out = sanitize_anthropic_kwargs(payload)
    assert out is payload
    assert payload == {"model": "claude-sonnet-4-6"}
    assert _fake_anthropic_call(**payload) == "OK"


def test_sanitize_anthropic_kwargs_leaves_clean_payload_untouched():
    from agent.anthropic_adapter import sanitize_anthropic_kwargs

    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1024,
    }
    snapshot = dict(payload)
    sanitize_anthropic_kwargs(payload)
    assert payload == snapshot


def test_sanitize_anthropic_kwargs_warns_when_stripping(caplog):
    from agent.anthropic_adapter import sanitize_anthropic_kwargs

    with caplog.at_level(logging.WARNING, logger="agent.anthropic_adapter"):
        sanitize_anthropic_kwargs(
            {"model": "claude-sonnet-4-6", "instructions": "sys"},
            log_prefix="[pfx] ",
        )
    assert any("31673" in r.message and "[pfx] " in r.message for r in caplog.records)


def test_sanitize_anthropic_kwargs_non_dict_noop():
    from agent.anthropic_adapter import sanitize_anthropic_kwargs

    assert sanitize_anthropic_kwargs(None) is None
    assert sanitize_anthropic_kwargs("not a dict") == "not a dict"


def test_anthropic_messages_create_sanitizes_before_sdk_call():
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.api_mode = "anthropic_messages"
    agent.log_prefix = ""
    agent._try_refresh_anthropic_client_credentials = MagicMock()
    agent._anthropic_client = MagicMock()
    agent._anthropic_client.messages.create.side_effect = _fake_anthropic_call

    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1024,
        "instructions": "sys",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "parallel_tool_calls": True,
    }

    assert agent._anthropic_messages_create(payload) == "OK"
    agent._anthropic_client.messages.create.assert_called_once_with(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1024,
    )

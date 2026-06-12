"""Anthropic replay blocks must not leak SDK response-only fields as input."""

from agent.anthropic_adapter import (
    _convert_assistant_message,
    _convert_content_part_to_anthropic,
    _sanitize_replay_block,
)


def _assert_clean(block):
    assert isinstance(block, dict)
    assert "parsed_output" not in block
    assert "caller" not in block
    if "citations" in block:
        assert isinstance(block["citations"], list) and block["citations"]


def test_sanitize_text_block_strips_parsed_output_and_null_citations():
    block = _sanitize_replay_block(
        {"type": "text", "text": "hello", "parsed_output": None, "citations": None},
    )

    _assert_clean(block)
    assert block == {"type": "text", "text": "hello"}


def test_sanitize_tool_use_strips_caller_but_preserves_input():
    block = _sanitize_replay_block(
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read_file",
            "input": {"path": "a.py"},
            "caller": {"type": "agent"},
        },
    )

    _assert_clean(block)
    assert block == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }


def test_sanitize_thinking_preserves_signature_and_redacted_data():
    thinking = _sanitize_replay_block(
        {"type": "thinking", "thinking": "plan", "signature": "sig_1"},
    )
    redacted = _sanitize_replay_block(
        {"type": "redacted_thinking", "data": "opaque"},
    )

    assert thinking == {"type": "thinking", "thinking": "plan", "signature": "sig_1"}
    assert redacted == {"type": "redacted_thinking", "data": "opaque"}


def test_content_text_part_is_whitelisted():
    block = _convert_content_part_to_anthropic(
        {"type": "text", "text": "hello", "parsed_output": None, "citations": None},
    )

    _assert_clean(block)
    assert block == {"type": "text", "text": "hello"}


def test_ordered_replay_sanitizes_output_only_fields_without_reordering():
    message = {
        "role": "assistant",
        "anthropic_content_blocks": [
            {"type": "thinking", "thinking": "plan", "signature": "sig_1"},
            {"type": "text", "text": "working", "parsed_output": None, "citations": None},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "a.py"},
                "caller": {"type": "agent"},
            },
        ],
    }

    blocks = _convert_assistant_message(message)["content"]

    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    for block in blocks:
        _assert_clean(block)

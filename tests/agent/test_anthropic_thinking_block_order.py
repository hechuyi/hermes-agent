"""Anthropic interleaved thinking/tool_use replay regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.anthropic_adapter import convert_messages_to_anthropic
from agent.chat_completion_helpers import build_assistant_message
from agent.transports import get_transport


class _FakeAgent:
    verbose_logging = False
    reasoning_callback = None
    stream_delta_callback = None
    _stream_callback = None

    def _extract_reasoning(self, _message):
        return None

    def _strip_think_blocks(self, content):
        return content

    def _needs_thinking_reasoning_pad(self):
        return False

    def _split_responses_tool_id(self, _raw_id):
        return None, None

    def _derive_responses_function_call_id(self, call_id, response_item_id):
        return response_item_id or call_id


def _thinking(text: str, signature: str):
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def _redacted(data: str):
    return SimpleNamespace(type="redacted_thinking", data=data)


def _tool_use(block_id: str, name: str, payload: dict):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=payload)


def _text(text: str):
    return SimpleNamespace(type="text", text=text, parsed_output=None, citations=None)


def _interleaved_response():
    return SimpleNamespace(
        content=[
            _thinking("Plan first call.", "sig_1"),
            _tool_use("toolu_1", "terminal", {"command": "curl -H 'Authorization: Bearer sk-live'"}),
            _thinking("Plan second call.", "sig_2"),
            _tool_use("toolu_2", "terminal", {"command": "echo done"}),
            _text("Queued both calls."),
        ],
        stop_reason="tool_use",
        usage=None,
    )


def _order(blocks):
    result = []
    for block in blocks:
        if block.get("type") in {"thinking", "redacted_thinking"}:
            result.append((block.get("type"), block.get("signature") or block.get("data")))
        elif block.get("type") == "tool_use":
            result.append(("tool_use", block.get("id")))
    return result


def test_transport_captures_ordered_blocks_for_signed_thinking_tool_turn():
    normalized = get_transport("anthropic_messages").normalize_response(
        _interleaved_response(),
    )

    assert normalized.anthropic_content_blocks is not None
    assert _order(normalized.anthropic_content_blocks) == [
        ("thinking", "sig_1"),
        ("tool_use", "toolu_1"),
        ("thinking", "sig_2"),
        ("tool_use", "toolu_2"),
    ]
    text_block = normalized.anthropic_content_blocks[-1]
    assert text_block == {"type": "text", "text": "Queued both calls."}


def test_transport_captures_redacted_thinking_in_ordered_blocks():
    response = SimpleNamespace(
        content=[
            _redacted("opaque-redacted-payload"),
            _tool_use("toolu_1", "lookup", {"q": "x"}),
        ],
        stop_reason="tool_use",
        usage=None,
    )

    normalized = get_transport("anthropic_messages").normalize_response(response)

    assert normalized.anthropic_content_blocks == [
        {"type": "redacted_thinking", "data": "opaque-redacted-payload"},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
    ]


def test_build_assistant_message_carries_ordered_blocks_in_memory():
    normalized = get_transport("anthropic_messages").normalize_response(
        _interleaved_response(),
    )
    assistant_message = SimpleNamespace(
        content=normalized.content,
        tool_calls=normalized.tool_calls,
        reasoning_details=normalized.reasoning_details,
        anthropic_content_blocks=normalized.anthropic_content_blocks,
    )

    message = build_assistant_message(_FakeAgent(), assistant_message, "tool_calls")

    assert message["anthropic_content_blocks"] == normalized.anthropic_content_blocks


def test_interleaved_order_preserved_and_tool_input_resourced_from_redacted_calls():
    raw_ordered = [
        {"type": "thinking", "thinking": "Plan first call.", "signature": "sig_1"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "terminal",
            "input": {"command": "curl -H 'Authorization: Bearer sk-live'"},
        },
        {"type": "thinking", "thinking": "Plan second call.", "signature": "sig_2"},
        {
            "type": "tool_use",
            "id": "toolu_2",
            "name": "terminal",
            "input": {"command": "echo done"},
        },
    ]
    assistant_msg = {
        "role": "assistant",
        "content": "",
        "reasoning_details": [b for b in raw_ordered if b["type"] == "thinking"],
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps(
                        {"command": "curl -H 'Authorization: Bearer [REDACTED_SECRET]'"}
                    ),
                },
            },
            {
                "id": "toolu_2",
                "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": "echo done"})},
            },
        ],
        "anthropic_content_blocks": raw_ordered,
    }
    messages = [
        {"role": "user", "content": "Call both tools."},
        assistant_msg,
        {"role": "tool", "tool_call_id": "toolu_1", "content": "200 OK"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "done"},
    ]

    _, anthropic_messages = convert_messages_to_anthropic(
        messages,
        base_url=None,
        model="claude-opus-4-8",
    )

    assistant = next(m for m in anthropic_messages if m["role"] == "assistant")
    blocks = assistant["content"]
    assert _order(blocks) == [
        ("thinking", "sig_1"),
        ("tool_use", "toolu_1"),
        ("thinking", "sig_2"),
        ("tool_use", "toolu_2"),
    ]
    tool_1 = next(b for b in blocks if b.get("type") == "tool_use" and b.get("id") == "toolu_1")
    assert "sk-live" not in tool_1["input"]["command"]
    assert "[REDACTED_SECRET]" in tool_1["input"]["command"]


def test_missing_ordered_blocks_falls_back_without_schema_dependency():
    messages = [
        {"role": "user", "content": "Call one tool."},
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {"type": "thinking", "thinking": "Plan.", "signature": "sig_live"},
            ],
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": json.dumps({"q": "x"})},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
    ]

    _, anthropic_messages = convert_messages_to_anthropic(
        messages,
        base_url=None,
        model="claude-opus-4-8",
    )

    assistant = next(m for m in anthropic_messages if m["role"] == "assistant")
    assert [b.get("type") for b in assistant["content"]] == ["thinking", "tool_use"]


def test_ordered_blocks_are_not_state_db_schema(tmp_path):
    import hermes_state

    db = hermes_state.SessionDB(tmp_path / "state.db")
    columns = {
        row[1]
        for row in db._conn.execute("PRAGMA table_info(messages)").fetchall()
    }

    assert "anthropic_content_blocks" not in columns

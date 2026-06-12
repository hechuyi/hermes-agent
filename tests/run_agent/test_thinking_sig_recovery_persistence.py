"""Regression tests for thinking-signature retry recovery state boundaries."""

from agent.conversation_loop import _strip_reasoning_details_for_thinking_signature_retry


def _shallow_copies(messages):
    return [m.copy() for m in messages]


def test_strip_api_messages_leaves_canonical_messages_intact():
    rd_one = [{"type": "thinking", "thinking": "one", "signature": "sig_one"}]
    rd_two = [{"type": "thinking", "thinking": "two", "signature": "sig_two"}]
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning_details": rd_one},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2", "reasoning_details": rd_two},
    ]
    api_messages = _shallow_copies(messages)

    stripped = _strip_reasoning_details_for_thinking_signature_retry(api_messages)

    assert stripped == 2
    assert all("reasoning_details" not in m for m in api_messages)
    assert [
        m.get("reasoning_details") for m in messages if m["role"] == "assistant"
    ] == [rd_one, rd_two]


def test_strip_is_idempotent_when_run_twice():
    api_messages = [
        {"role": "assistant", "content": "a", "reasoning_details": [{"x": 1}]},
        {"role": "user", "content": "q"},
    ]

    first = _strip_reasoning_details_for_thinking_signature_retry(api_messages)
    second = _strip_reasoning_details_for_thinking_signature_retry(api_messages)

    assert first == 1
    assert second == 0
    assert all("reasoning_details" not in m for m in api_messages)


def test_strip_skips_messages_without_reasoning_details():
    api_messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ]
    snapshot = [dict(m) for m in api_messages]

    stripped = _strip_reasoning_details_for_thinking_signature_retry(api_messages)

    assert stripped == 0
    assert api_messages == snapshot

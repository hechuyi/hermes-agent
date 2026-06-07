from types import SimpleNamespace

import pytest

from agent.codex_responses_adapter import (
    _format_responses_error,
    _normalize_codex_response,
)


def test_format_responses_error_prefixes_code_and_message():
    error = {"code": "rate_limit_exceeded", "message": "Slow down"}

    assert _format_responses_error(error, "failed") == "rate_limit_exceeded: Slow down"


def test_format_responses_error_handles_attribute_payload_without_message():
    error = SimpleNamespace(code="internal_error", message="")

    assert _format_responses_error(error, "failed") == "internal_error"


def test_normalize_codex_response_failed_status_preserves_error_code():
    response = SimpleNamespace(
        status="failed",
        error={"code": "rate_limit_exceeded", "message": "Slow down"},
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                status="incomplete",
                content=[],
            )
        ],
    )

    with pytest.raises(RuntimeError, match="rate_limit_exceeded: Slow down"):
        _normalize_codex_response(response)


def test_normalize_codex_response_drops_transient_rs_tmp_reasoning_items():
    response = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="reasoning",
                id="rs_tmp_123",
                encrypted_content="opaque-transient",
                summary=[],
            ),
            SimpleNamespace(
                type="reasoning",
                id="rs_456",
                encrypted_content="opaque-stable",
                summary=[SimpleNamespace(text="stable summary")],
            ),
            SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[SimpleNamespace(type="output_text", text="done")],
            ),
        ],
    )

    assistant_message, finish_reason = _normalize_codex_response(response)

    assert finish_reason == "stop"
    assert assistant_message.content == "done"
    assert assistant_message.codex_reasoning_items == [
        {
            "type": "reasoning",
            "encrypted_content": "opaque-stable",
            "id": "rs_456",
            "summary": [{"type": "summary_text", "text": "stable summary"}],
        }
    ]


def test_normalize_codex_response_treats_summary_only_reasoning_as_incomplete():
    response = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="reasoning",
                id="rs_tmp_789",
                encrypted_content="opaque-transient",
                summary=[SimpleNamespace(text="still thinking")],
            )
        ],
    )

    assistant_message, finish_reason = _normalize_codex_response(response)

    assert finish_reason == "incomplete"
    assert assistant_message.content == ""
    assert assistant_message.reasoning == "still thinking"
    assert assistant_message.codex_reasoning_items is None

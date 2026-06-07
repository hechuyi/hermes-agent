"""Regression coverage for stale compaction handoffs after resume."""

from unittest.mock import MagicMock, patch

from agent.context_compressor import (
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    ContextCompressor,
)


_OLD_CONFLICTING_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Your current task is identified in the '## Active Task' section of the "
    "summary — resume exactly from there. "
    "Respond ONLY to the latest user message "
    "that appears AFTER this summary. The current session state (files, "
    "config, etc.) may reflect work described here — avoid repeating it:"
)


def _response(content: str = "updated summary"):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


def _compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(model="test/model", quiet_mode=True)


def test_historical_conflicting_prefix_is_renormalized():
    stale_handoff = (
        f"{_OLD_CONFLICTING_PREFIX}\n"
        "## Active Task\n"
        "User asked: 'finish task A'\n"
    )

    assert "resume exactly" in stale_handoff.lower()

    renormalized = ContextCompressor._with_summary_prefix(stale_handoff)

    assert renormalized.startswith(SUMMARY_PREFIX)
    assert "finish task A" in renormalized
    assert "resume exactly" not in renormalized.lower()


def test_historical_conflicting_handoff_is_detected_and_stripped():
    messages = [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "content": f"{_OLD_CONFLICTING_PREFIX}\n## Active Task\nUser asked: 'task A'",
        },
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "unrelated task B"},
    ]

    idx, body = ContextCompressor._find_latest_context_summary(messages, 1, len(messages))

    assert idx == 1
    assert "task A" in body
    assert "resume exactly" not in body.lower()
    assert not body.startswith(SUMMARY_PREFIX)


def test_legacy_prefix_still_renormalizes():
    renormalized = ContextCompressor._with_summary_prefix(
        f"{LEGACY_SUMMARY_PREFIX} ## Active Task\nUser asked: 'task A'"
    )

    assert renormalized.startswith(SUMMARY_PREFIX)
    assert LEGACY_SUMMARY_PREFIX not in renormalized
    assert "task A" in renormalized


def test_active_task_prompt_captures_unanswered_questions_and_decisions():
    compressor = _compressor()
    compressor._previous_summary = "## Active Task\nUser asked: 'old task'\n"
    turns = [
        {"role": "user", "content": "Should we use option A or option B?"},
        {"role": "assistant", "content": "I need to inspect the current code first."},
    ]

    with patch("agent.context_compressor.call_llm", return_value=_response()) as mock_call:
        compressor._generate_summary(turns)

    prompt = " ".join(mock_call.call_args.kwargs["messages"][0]["content"].lower().split())
    assert "most recent unfulfilled input" in prompt
    assert "questions awaiting an answer" in prompt
    assert "decisions awaiting input" in prompt
    assert "do not write \"none\" merely" in prompt
    assert "only write \"none\" if the last exchange was fully resolved" in prompt

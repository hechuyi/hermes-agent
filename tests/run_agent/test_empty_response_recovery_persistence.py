"""Regression tests for empty-response recovery transcript persistence."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_agent import AIAgent


def _agent_with_stubbed_persistence():
    agent = AIAgent.__new__(AIAgent)
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._session_db = None
    agent._session_messages = []
    agent.flushed_session_db_messages = []
    agent._flush_messages_to_session_db = lambda messages, conversation_history=None: (
        agent.flushed_session_db_messages.append([m.copy() for m in messages])
    )
    return agent


def test_persist_session_strips_trailing_empty_recovery_scaffolding():
    """After stripping scaffolding, also rewind past orphan trailing tool-result
    messages that the failed iteration left behind. Otherwise the next user
    message lands after a bare ``tool`` and produces a protocol-invalid
    sequence that most providers silently fail on, retriggering the empty-
    retry loop indefinitely.
    """
    agent = _agent_with_stubbed_persistence()
    messages = [
        {"role": "user", "content": "run the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{}", "tool_call_id": "call_1"},
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_recovery_synthetic": True,
        },
        {
            "role": "user",
            "content": (
                "You just executed tool calls but returned an empty response. "
                "Please process the tool results above and continue with the task."
            ),
            "_empty_recovery_synthetic": True,
        },
    ]

    AIAgent._persist_session(agent, messages, conversation_history=[])

    # After strip + rewind, only the original user message remains. The
    # assistant(tool_calls) + tool pair is dropped because its iteration
    # never produced a real response.
    assert messages == [
        {"role": "user", "content": "run the task"},
    ]
    assert agent.flushed_session_db_messages[-1] == messages
    assert all(not msg.get("_empty_recovery_synthetic") for msg in messages)


def test_persist_session_keeps_unmarked_terminal_empty_response():
    agent = _agent_with_stubbed_persistence()
    messages = [
        {"role": "user", "content": "run the task"},
        {"role": "assistant", "content": "(empty)"},
    ]

    AIAgent._persist_session(agent, messages, conversation_history=[])

    assert messages == [
        {"role": "user", "content": "run the task"},
        {"role": "assistant", "content": "(empty)"},
    ]
    assert agent.flushed_session_db_messages[-1] == messages


def test_persist_session_strips_marked_terminal_empty_sentinel():
    agent = _agent_with_stubbed_persistence()
    messages = [
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_terminal_sentinel": True,
        },
    ]

    AIAgent._persist_session(agent, messages, conversation_history=[])

    assert messages == [{"role": "user", "content": "continue"}]
    assert agent.flushed_session_db_messages[-1] == messages
    assert all(not msg.get("_empty_terminal_sentinel") for msg in messages)


def test_run_conversation_empty_exhaustion_does_not_persist_unmarked_empty_final(tmp_path):
    """The real terminal empty path must not turn the private sentinel into
    durable assistant content or a strict-deliverable proof.
    """
    from hermes_state import SessionDB

    class _EmptyCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            reasoning=None,
                            reasoning_content=None,
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
                model="test/model",
            )

    class _FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_EmptyCompletions())

        def close(self):
            return None

    db = SessionDB(db_path=Path(tmp_path) / "state.db")
    with (
        patch("run_agent.OpenAI", lambda **kwargs: _FakeClient()),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.com/v1",
            model="test/model",
            max_iterations=8,
            quiet_mode=True,
            session_db=db,
            session_id="empty-terminal-real-path",
            skip_context_files=True,
            skip_memory=True,
        )
        agent._disable_streaming = True

        result = agent.run_conversation("produce no visible content")
    rows = db.get_messages(agent.session_id)

    assert result["final_response"] == "(empty)"
    assert [row["role"] for row in rows] == ["user"]
    assert [row["content"] for row in rows] == ["produce no visible content"]
    assert all(row["content"] != "(empty)" for row in rows)
    assert all(
        not (
            msg.get("role") == "assistant"
            and msg.get("content") == "(empty)"
            and not msg.get("_empty_terminal_sentinel")
        )
        for msg in result["messages"]
    )
    assert not (
        result["persistence"].get("ok")
        and result["persistence"].get("assistant_message_row_id") is not None
    )

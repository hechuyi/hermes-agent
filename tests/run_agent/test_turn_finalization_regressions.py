from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _mock_response(content="Done.", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(max_iterations: int = 1) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


def test_final_allowed_api_call_with_stop_text_is_completed():
    agent = _make_agent(max_iterations=1)
    agent.client.chat.completions.create.return_value = _mock_response(
        content="Finished normally.",
        finish_reason="stop",
    )

    with (
        patch.object(agent, "_persist_session", return_value={"ok": True}),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("finish on the only allowed call")

    assert result["final_response"] == "Finished normally."
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["api_calls"] == 1
    assert result["completed"] is True


@pytest.mark.parametrize(
    ("failing_stage", "expected_action"),
    [
        ("_save_trajectory", "save_trajectory"),
        ("_cleanup_task_resources", "cleanup_task_resources"),
        ("_persist_session", "persist_session"),
    ],
)
def test_turn_finalization_cleanup_errors_do_not_hide_final_response(
    failing_stage,
    expected_action,
):
    agent = _make_agent(max_iterations=2)
    agent.client.chat.completions.create.return_value = _mock_response(
        content="Final answer.",
        finish_reason="stop",
    )
    calls = []

    def save_trajectory(*args, **kwargs):
        calls.append("save_trajectory")
        if failing_stage == "_save_trajectory":
            raise RuntimeError("trajectory failed with api_key=secret-token-value")

    def cleanup_task_resources(*args, **kwargs):
        calls.append("cleanup_task_resources")
        if failing_stage == "_cleanup_task_resources":
            raise RuntimeError("cleanup failed with api_key=secret-token-value")

    def persist_session(*args, **kwargs):
        calls.append("persist_session")
        if failing_stage == "_persist_session":
            raise RuntimeError("persist failed with api_key=secret-token-value")
        return {"ok": True}

    with (
        patch.object(agent, "_save_trajectory", side_effect=save_trajectory),
        patch.object(agent, "_cleanup_task_resources", side_effect=cleanup_task_resources),
        patch.object(agent, "_persist_session", side_effect=persist_session),
    ):
        result = agent.run_conversation("return final response despite cleanup failure")

    assert result["final_response"] == "Final answer."
    assert result["completed"] is True
    assert calls == ["save_trajectory", "cleanup_task_resources", "persist_session"]
    assert result["cleanup_errors"]
    assert result["cleanup_errors"][0]["stage"] == "turn_finalization"
    assert result["cleanup_errors"][0]["action"] == expected_action
    assert "api_key=***" in result["cleanup_errors"][0]["error"]
    assert "secret-token-value" not in result["cleanup_errors"][0]["error"]
    assert result["failed"] is True
    assert result["cleanup_failed"] is True
    if failing_stage == "_persist_session":
        assert result["persistence"]["ok"] is False

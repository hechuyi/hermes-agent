"""Tests for per-turn context preparation and late MCP refresh."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.turn_context import build_turn_context
from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin


def _make_agent():
    agent = SimpleNamespace()
    agent._restore_primary_runtime = MagicMock(return_value=True)
    agent._skip_mcp_refresh = False
    agent.enabled_toolsets = ["hermes-cli"]
    agent.disabled_toolsets = None
    return agent


def test_refresh_adds_late_tool_when_servers_registered():
    agent = _make_agent()
    captured = {}

    def _refresh(target_agent, *, enabled_override=None, quiet_mode=True):
        captured["agent"] = target_agent
        captured["enabled_override"] = enabled_override
        captured["quiet_mode"] = quiet_mode
        target_agent.tools = [{"function": {"name": "mcp_demo_ping"}}]
        target_agent.valid_tool_names = {"mcp_demo_ping"}
        return {"mcp_demo_ping"}

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch("tools.mcp_tool.refresh_agent_mcp_tools", side_effect=_refresh):
        user_message, persist_user_message = build_turn_context(
            agent,
            "hello",
            persist_user_message="persist",
        )

    assert user_message == "hello"
    assert persist_user_message == "persist"
    assert captured["agent"] is agent
    assert captured["enabled_override"] is None
    assert captured["quiet_mode"] is True
    assert agent.valid_tool_names == {"mcp_demo_ping"}


def test_refresh_skipped_when_no_servers():
    agent = _make_agent()

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=False), \
         patch("tools.mcp_tool.refresh_agent_mcp_tools") as refresh_mock:
        build_turn_context(agent, "hello")

    refresh_mock.assert_not_called()
    agent._restore_primary_runtime.assert_called_once()


def test_refresh_skipped_when_flag_set():
    agent = _make_agent()
    agent._skip_mcp_refresh = True

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch("tools.mcp_tool.refresh_agent_mcp_tools") as refresh_mock:
        build_turn_context(agent, "hello")

    refresh_mock.assert_not_called()


def test_refresh_skipped_for_background_review_origin():
    agent = _make_agent()
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
             patch("tools.mcp_tool.refresh_agent_mcp_tools") as refresh_mock:
            build_turn_context(agent, "hello")
    finally:
        reset_current_write_origin(token)

    refresh_mock.assert_not_called()


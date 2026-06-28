"""Focused tests for MCP refresh snapshot rebuilds."""

from types import SimpleNamespace
from unittest.mock import patch


def _tool(name: str):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


def _agent(tool_names=None):
    names = list(tool_names or [])
    agent = SimpleNamespace()
    agent.tools = [_tool(name) for name in names]
    agent.valid_tool_names = set(names)
    agent.enabled_toolsets = ["hermes-cli"]
    agent.disabled_toolsets = None
    agent._tool_snapshot_generation = 0
    agent._context_engine_tool_names = set()
    agent._memory_manager = None

    class _Compressor:
        def get_tool_schemas(self):
            return []

    agent.context_compressor = _Compressor()
    return agent


def test_stale_generation_refresh_does_not_clobber_newer_snapshot():
    from tools import mcp_tool
    from tools.registry import registry
    import model_tools  # ensure registry discovery side effects happen before the snapshot baseline

    agent = _agent(["read_file", "mcp_new_tool"])
    agent._tool_snapshot_generation = registry._generation + 5

    with patch.object(model_tools, "get_tool_definitions", return_value=[_tool("read_file")]):
        added = mcp_tool.refresh_agent_mcp_tools(agent)

    assert added == set()
    assert agent.valid_tool_names == {"read_file", "mcp_new_tool"}
    assert [tool["function"]["name"] for tool in agent.tools] == ["read_file", "mcp_new_tool"]


def test_refresh_does_not_claim_context_engine_name_already_owned_by_registry_tool():
    from tools import mcp_tool
    import model_tools

    agent = _agent()

    class _Compressor:
        def get_tool_schemas(self):
            return [{"name": "shared", "description": "", "parameters": {"type": "object", "properties": {}}}]

    agent.context_compressor = _Compressor()

    with patch.object(model_tools, "get_tool_definitions", return_value=[_tool("shared")]):
        added = mcp_tool.refresh_agent_mcp_tools(agent)

    assert added == {"shared"}
    assert agent.valid_tool_names == {"shared"}
    assert agent._context_engine_tool_names == set()


def test_has_registered_mcp_tools_reflects_live_server_map():
    from tools import mcp_tool

    with patch.object(mcp_tool, "_servers", {}):
        assert mcp_tool.has_registered_mcp_tools() is False

    with patch.object(mcp_tool, "_servers", {"srv": object()}):
        assert mcp_tool.has_registered_mcp_tools() is True

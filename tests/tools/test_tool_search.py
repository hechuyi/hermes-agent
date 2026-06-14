"""Session-scoped progressive tool disclosure tests.

These tests pin the fork-local contract: tool_search is a scoped bridge for
non-Feishu plugin/MCP tools, never a global registry browser.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


def _schema(name: str, description: str = "temporary test tool") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        },
    }


@pytest.fixture
def temp_registry_tools():
    from tools.registry import registry

    registered: list[str] = []

    def register(name: str, toolset: str, *, description: str | None = None) -> str:
        registry.deregister(name)

        def _handler(args: dict, **_kwargs) -> str:
            return json.dumps({"ok": True, "tool": name, "args": args})

        registry.register(
            name=name,
            toolset=toolset,
            schema=_schema(name, description or f"{name} handles scoped queries"),
            handler=_handler,
        )
        registered.append(name)
        return name

    yield register

    for name in registered:
        registry.deregister(name)
    try:
        import model_tools

        model_tools._clear_tool_defs_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def clear_model_tool_cache():
    try:
        import model_tools

        model_tools._clear_tool_defs_cache()
    except Exception:
        pass
    yield
    try:
        import model_tools

        model_tools._clear_tool_defs_cache()
    except Exception:
        pass


def _names(tool_defs: list[dict[str, Any]]) -> set[str]:
    return {tool["function"]["name"] for tool in tool_defs}


def test_forced_bridge_keeps_core_visible_and_defers_plugin_tools(temp_registry_tools):
    temp_registry_tools("toolsearch_allowed_alpha", "toolsearch-plugin")

    import model_tools
    from tools import tool_search

    raw_defs = model_tools.get_tool_definitions(
        enabled_toolsets=["toolsearch-plugin", "file"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    assembled = tool_search.assemble_tool_defs(
        raw_defs,
        context_length=200_000,
        config=tool_search.ToolSearchConfig.from_raw({"enabled": "on"}),
    )

    names = _names(assembled.tool_defs)
    assert assembled.activated
    assert "read_file" in names
    assert "search_files" in names
    assert "toolsearch_allowed_alpha" not in names
    assert {"tool_search", "tool_describe", "tool_call"} <= names


def test_bridge_tools_and_core_tools_are_not_deferrable():
    from tools import tool_search

    for name in ("tool_search", "tool_describe", "tool_call"):
        assert not tool_search.is_deferrable_tool_name(name)
    for name in ("read_file", "write_file", "patch", "search_files", "terminal"):
        assert not tool_search.is_deferrable_tool_name(name)


def test_scoped_search_only_lists_tools_from_enabled_toolsets(temp_registry_tools):
    for index in range(3):
        temp_registry_tools(
            f"toolsearch_allowed_search_{index}",
            "toolsearch-allowed",
            description="allowed scoped search capability",
        )
    temp_registry_tools(
        "toolsearch_denied_search",
        "toolsearch-denied",
        description="denied scoped search capability",
    )

    import model_tools

    result = json.loads(
        model_tools.handle_function_call(
            "tool_search",
            {"query": "scoped search", "limit": 10},
            enabled_toolsets=["toolsearch-allowed"],
        )
    )

    assert result["total_available"] == 3
    hit_names = {match["name"] for match in result["matches"]}
    assert "toolsearch_denied_search" not in hit_names
    assert {"toolsearch_allowed_search_0", "toolsearch_allowed_search_1"} & hit_names


def test_tool_call_rejects_out_of_scope_tool(temp_registry_tools):
    temp_registry_tools("toolsearch_allowed_call", "toolsearch-allowed")
    temp_registry_tools("toolsearch_denied_call", "toolsearch-denied")

    import model_tools

    rejected = json.loads(
        model_tools.handle_function_call(
            "tool_call",
            {"name": "toolsearch_denied_call", "arguments": {"query": "x"}},
            enabled_toolsets=["toolsearch-allowed"],
        )
    )
    assert "error" in rejected
    assert "not available in this session" in rejected["error"]

    accepted = json.loads(
        model_tools.handle_function_call(
            "tool_call",
            {"name": "toolsearch_allowed_call", "arguments": {"query": "x"}},
            enabled_toolsets=["toolsearch-allowed"],
        )
    )
    assert accepted == {
        "ok": True,
        "tool": "toolsearch_allowed_call",
        "args": {"query": "x"},
    }


def test_tool_describe_rejects_out_of_scope_tool(temp_registry_tools):
    temp_registry_tools("toolsearch_allowed_describe", "toolsearch-allowed")
    temp_registry_tools("toolsearch_denied_describe", "toolsearch-denied")

    import model_tools

    rejected = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": "toolsearch_denied_describe"},
            enabled_toolsets=["toolsearch-allowed"],
        )
    )
    assert "error" in rejected
    assert "not available in this session" in rejected["error"]

    described = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": "toolsearch_allowed_describe"},
            enabled_toolsets=["toolsearch-allowed"],
        )
    )
    assert described["name"] == "toolsearch_allowed_describe"
    assert described["schema"]["function"]["name"] == "toolsearch_allowed_describe"


@pytest.mark.parametrize(
    ("bridge_name", "args"),
    [
        ("tool_search", {"query": "anything"}),
        ("tool_describe", {"name": "anything"}),
        ("tool_call", {"name": "anything", "arguments": {}}),
    ],
)
def test_bridge_dispatch_without_explicit_scope_fails_closed(bridge_name, args):
    import model_tools

    result = json.loads(model_tools.handle_function_call(bridge_name, args))

    assert "error" in result
    assert "scope" in result["error"].lower()


def test_scoped_bridge_dispatch_does_not_pollute_last_resolved_names(temp_registry_tools):
    temp_registry_tools("toolsearch_allowed_pollute", "toolsearch-allowed")
    temp_registry_tools("toolsearch_denied_pollute", "toolsearch-denied")

    import model_tools

    model_tools.get_tool_definitions(
        enabled_toolsets=["toolsearch-allowed"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    before = set(model_tools._last_resolved_tool_names)
    assert "toolsearch_denied_pollute" not in before

    model_tools.handle_function_call(
        "tool_search",
        {"query": "pollute"},
        enabled_toolsets=["toolsearch-allowed"],
    )

    after = set(model_tools._last_resolved_tool_names)
    assert "toolsearch_denied_pollute" not in after


def test_scoped_deferrable_names_uses_supplied_definitions(temp_registry_tools):
    temp_registry_tools("toolsearch_allowed_helper", "toolsearch-allowed")
    temp_registry_tools("toolsearch_denied_helper", "toolsearch-denied")

    import model_tools
    from tools.tool_search import scoped_deferrable_names

    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["toolsearch-allowed"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )

    assert scoped_deferrable_names(defs) == frozenset({"toolsearch_allowed_helper"})


def test_hermes_feishu_package_b_tools_remain_direct_when_bridge_is_forced(monkeypatch):
    import model_tools
    from gateway.feishu_readiness import FEISHU_PACKAGE_B_MODEL_VISIBLE_CAPABILITIES
    from tools import tool_search

    monkeypatch.setattr(
        tool_search,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({"enabled": "on"}),
    )

    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-feishu"],
        quiet_mode=True,
        context_length=1,
    )

    names = _names(defs)
    assert FEISHU_PACKAGE_B_MODEL_VISIBLE_CAPABILITIES <= names
    assert "tool_search" not in names
    assert "tool_describe" not in names
    assert "tool_call" not in names


def test_feishu_family_tools_are_not_deferrable(temp_registry_tools):
    feishu_names = [
        temp_registry_tools("toolsearch_feishu_doc_probe", "feishu_doc"),
        temp_registry_tools("toolsearch_feishu_drive_probe", "feishu_drive"),
        temp_registry_tools("toolsearch_hermes_feishu_probe", "hermes-feishu"),
    ]

    from tools import tool_search

    for name in feishu_names:
        assert not tool_search.is_deferrable_tool_name(name)


def test_tool_describe_rejects_legacy_feishu_tool(temp_registry_tools):
    legacy_name = temp_registry_tools("toolsearch_feishu_doc_legacy", "feishu_doc")

    import model_tools

    result = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": legacy_name},
            enabled_toolsets=["feishu_doc"],
        )
    )

    assert "error" in result
    assert "not a deferrable" in result["error"]

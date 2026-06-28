"""Tests for capability-gated MCP tool discovery and keepalive.

Prompt-only / resource-only MCP servers do not implement the ``tools/*``
request family. Per the MCP spec, ``InitializeResult.capabilities.tools``
is non-None iff the server supports it. Before this fix, Hermes always
called ``tools/list`` during discovery, which raised
``McpError(-32601 Method not found)`` against such servers, so a prompt-only
server could never stay connected. Keepalive now uses ``ping`` first for every
server and falls back to ``tools/list`` only when a tool-capable server does
not implement ping.

Ported from anomalyco/opencode#31271.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tools.mcp_tool import MCPServerTask


class _RpcError(Exception):
    def __init__(self, code=None, message=""):
        super().__init__(message)
        self.error = SimpleNamespace(code=code, message=message)


class _DictRpcError(Exception):
    def __init__(self, code=None, message=""):
        super().__init__({"code": code, "message": message})
        self.error = {"code": code, "message": message}


def _caps(tools=None, prompts=None, resources=None):
    """Build a fake InitializeResult with the given capability sub-objects."""
    return SimpleNamespace(
        capabilities=SimpleNamespace(tools=tools, prompts=prompts, resources=resources)
    )


class TestAdvertisesTools:
    def test_true_when_tools_capability_present(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace(listChanged=True))
        assert task._advertises_tools() is True

    def test_false_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(prompts=SimpleNamespace(listChanged=None))
        assert task._advertises_tools() is False

    def test_false_for_resource_only_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(resources=SimpleNamespace())
        assert task._advertises_tools() is False

    def test_legacy_fallback_no_initialize_result(self):
        """No captured capabilities → preserve old always-list_tools behavior."""
        task = MCPServerTask("test")
        assert task.initialize_result is None
        assert task._advertises_tools() is True

    def test_legacy_fallback_no_capabilities_attr(self):
        task = MCPServerTask("test")
        task.initialize_result = SimpleNamespace()  # no .capabilities
        assert task._advertises_tools() is True


class TestMethodNotFoundDetection:
    def test_detects_json_rpc_method_not_found_code(self):
        from tools.mcp_tool import _is_method_not_found_error

        assert _is_method_not_found_error(_RpcError(-32601, "Method not found"))

    def test_detects_dict_json_rpc_method_not_found_code(self):
        from tools.mcp_tool import _is_method_not_found_error

        assert _is_method_not_found_error(
            _DictRpcError(-32601, "Method not found")
        )

    def test_rejects_other_json_rpc_errors(self):
        from tools.mcp_tool import _is_method_not_found_error

        assert not _is_method_not_found_error(_RpcError(-32602, "Invalid params"))

    def test_unknown_method_phrasing_is_match(self):
        from tools.mcp_tool import _is_method_not_found_error

        assert _is_method_not_found_error(Exception("Unknown method: ping"))


@pytest.mark.asyncio
class TestDiscoverToolsGating:
    async def test_skips_list_tools_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock())
        task._tools = ["stale"]

        await task._discover_tools()

        task.session.list_tools.assert_not_called()
        assert task._tools == []

    async def test_calls_list_tools_for_tool_capable_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        fake_tool = SimpleNamespace(name="echo")
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[fake_tool]))
        )

        await task._discover_tools()

        task.session.list_tools.assert_awaited_once()
        assert task._tools == [fake_tool]

    async def test_legacy_fallback_still_calls_list_tools(self):
        task = MCPServerTask("test")
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[]))
        )

        await task._discover_tools()

        task.session.list_tools.assert_awaited_once()

    async def test_method_not_found_falls_back_to_ping_and_empty_tools(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(side_effect=_RpcError(-32601, "Method not found")),
            send_ping=AsyncMock(),
        )
        task._tools = ["stale"]

        await task._discover_tools()

        task.session.list_tools.assert_awaited_once()
        task.session.send_ping.assert_awaited_once()
        assert task._tools == []

    async def test_other_list_tools_errors_are_not_swallowed(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(side_effect=_RpcError(-32602, "Invalid params")),
            send_ping=AsyncMock(),
        )

        with pytest.raises(_RpcError):
            await task._discover_tools()

        task.session.list_tools.assert_awaited_once()
        task.session.send_ping.assert_not_called()


@pytest.mark.asyncio
class TestRefreshToolsGating:
    async def test_refresh_noop_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock())

        await task._refresh_tools()

        task.session.list_tools.assert_not_called()


@pytest.mark.asyncio
class TestKeepaliveProbe:
    async def _run_one_keepalive_cycle(self, task):
        """Drive _wait_for_lifecycle_event through exactly one keepalive
        timeout, then fire shutdown so it returns."""
        real_wait = asyncio.wait
        cycles = {"n": 0}

        async def fake_wait(tasks, timeout=None, return_when=None):
            cycles["n"] += 1
            if cycles["n"] == 1:
                # Simulate keepalive timeout: nothing completed.
                return set(), set(tasks)
            # Second cycle: let shutdown win.
            task._shutdown_event.set()
            return await real_wait(
                tasks, timeout=0.5, return_when=return_when or asyncio.FIRST_COMPLETED
            )

        import tools.mcp_tool as mcp_mod
        orig = mcp_mod.asyncio.wait
        mcp_mod.asyncio.wait = fake_wait
        try:
            return await task._wait_for_lifecycle_event()
        finally:
            mcp_mod.asyncio.wait = orig

    async def test_keepalive_uses_ping_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(),
            send_ping=AsyncMock(),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "shutdown"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_not_called()

    async def test_keepalive_uses_ping_for_tool_capable_server(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            send_ping=AsyncMock(),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "shutdown"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_not_called()

    async def test_keepalive_uses_ping_legacy_fallback(self):
        task = MCPServerTask("test")
        assert task.initialize_result is None
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            send_ping=AsyncMock(),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "shutdown"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_not_called()

    async def test_keepalive_method_not_found_falls_back_to_list_tools(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            send_ping=AsyncMock(side_effect=_RpcError(-32601, "Method not found")),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "shutdown"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_awaited_once()
        assert task._ping_unsupported is True

    async def test_keepalive_latch_skips_ping_after_method_not_found(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            send_ping=AsyncMock(side_effect=_RpcError(-32601, "Method not found")),
        )

        await task._keepalive_probe()
        await task._keepalive_probe()

        task.session.send_ping.assert_awaited_once()
        assert task.session.list_tools.await_count == 2

    async def test_keepalive_unknown_method_falls_back_to_list_tools(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            send_ping=AsyncMock(side_effect=Exception("Unknown method: ping")),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "shutdown"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_awaited_once()

    async def test_keepalive_other_ping_errors_trigger_reconnect(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(),
            send_ping=AsyncMock(side_effect=_RpcError(-32602, "Invalid params")),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "reconnect"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_not_called()

    async def test_keepalive_no_ping_no_tools_propagates_method_not_found(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(
            list_tools=AsyncMock(),
            send_ping=AsyncMock(side_effect=_RpcError(-32601, "Method not found")),
        )

        reason = await self._run_one_keepalive_cycle(task)

        assert reason == "reconnect"
        task.session.send_ping.assert_awaited_once()
        task.session.list_tools.assert_not_called()

    async def test_discover_resets_ping_fallback_latch(self):
        task = MCPServerTask("test")
        task.initialize_result = _caps(tools=SimpleNamespace())
        task._ping_unsupported = True
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
        )

        await task._discover_tools()

        assert task._ping_unsupported is False


class TestKeepaliveInterval:
    async def _captured_interval(self, config):
        task = MCPServerTask("test")
        task._config = config
        task.session = SimpleNamespace(send_ping=AsyncMock())
        captured = {}
        real_wait = asyncio.wait

        async def fake_wait(tasks, timeout=None, return_when=None):
            captured["timeout"] = timeout
            task._shutdown_event.set()
            return await real_wait(
                tasks, timeout=0.5, return_when=return_when or asyncio.FIRST_COMPLETED
            )

        import tools.mcp_tool as mcp_mod
        orig = mcp_mod.asyncio.wait
        mcp_mod.asyncio.wait = fake_wait
        try:
            await task._wait_for_lifecycle_event()
        finally:
            mcp_mod.asyncio.wait = orig
        return captured["timeout"]

    @pytest.mark.asyncio
    async def test_default_interval_when_unset(self):
        from tools.mcp_tool import _DEFAULT_KEEPALIVE_INTERVAL
        assert await self._captured_interval({}) == _DEFAULT_KEEPALIVE_INTERVAL

    @pytest.mark.asyncio
    async def test_configured_interval_honored(self):
        assert await self._captured_interval({"keepalive_interval": 10}) == 10

    @pytest.mark.asyncio
    async def test_interval_clamped_to_floor(self):
        from tools.mcp_tool import _MIN_KEEPALIVE_INTERVAL
        assert (
            await self._captured_interval({"keepalive_interval": 0.1})
            == _MIN_KEEPALIVE_INTERVAL
        )


class TestShutdown:
    def test_shutdown_mcp_servers_swallows_cancelled_error_from_future_result(self):
        import tools.mcp_tool as mcp_mod
        from tools.mcp_tool import _servers, shutdown_mcp_servers

        class _CancelledFuture:
            def result(self, timeout=None):
                raise asyncio.CancelledError()

        _servers.clear()
        _servers["test"] = SimpleNamespace(name="test", shutdown=AsyncMock())
        original_loop = mcp_mod._mcp_loop
        original_thread = mcp_mod._mcp_thread
        mcp_mod._mcp_loop = SimpleNamespace(
            is_running=lambda: True,
            call_soon_threadsafe=lambda *args, **kwargs: None,
        )
        mcp_mod._mcp_thread = None
        try:
            def _fake_schedule(coro, loop, **kwargs):
                coro.close()
                return _CancelledFuture()

            with patch(
                "agent.async_utils.safe_schedule_threadsafe",
                side_effect=_fake_schedule,
            ), patch.object(mcp_mod, "_stop_mcp_loop", return_value=None):
                shutdown_mcp_servers()
        finally:
            _servers.clear()
            mcp_mod._mcp_loop = original_loop
            mcp_mod._mcp_thread = original_thread

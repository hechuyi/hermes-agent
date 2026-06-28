"""Tests for MCP tool-handler circuit-breaker recovery.

The circuit breaker in ``tools/mcp_tool.py`` is intended to short-circuit
calls to an MCP server that has failed ``_CIRCUIT_BREAKER_THRESHOLD``
consecutive times, then *transition back to a usable state* once the
server has had time to recover (or an explicit reconnect succeeds).

The original implementation only had two states — closed and open — with
no mechanism to transition back to closed, so a tripped breaker stayed
tripped for the lifetime of the process. These tests lock in the
half-open / cooldown / reconnect-resets-breaker behavior that fixes
that.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


pytest.importorskip("mcp.client.auth.oauth2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_stub_server(mcp_tool_module, name: str, call_tool_impl):
    """Install a fake MCP server in the module's registry.

    ``call_tool_impl`` is an async function stored at ``session.call_tool``
    (it's what the tool handler invokes).
    """
    server = MagicMock()
    server.name = name
    session = MagicMock()
    session.call_tool = call_tool_impl
    server.session = session
    server._reconnect_event = MagicMock()
    server._ready = MagicMock()
    server._ready.is_set.return_value = True

    mcp_tool_module._servers[name] = server
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)
    return server


def _cleanup(mcp_tool_module, name: str) -> None:
    mcp_tool_module._servers.pop(name, None)
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_circuit_breaker_half_opens_after_cooldown(monkeypatch, tmp_path):
    """After a tripped breaker's cooldown elapses, the *next* call must
    actually execute against the session (half-open probe). When the
    probe succeeds, the breaker resets to fully closed.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    call_count = {"n": 0}

    async def _call_tool_success(*a, **kw):
        call_count["n"] += 1
        result = MagicMock()
        result.isError = False
        block = MagicMock()
        block.text = "ok"
        result.content = [block]
        result.structuredContent = None
        return result

    _install_stub_server(mcp_tool, "srv", _call_tool_success)
    mcp_tool._ensure_mcp_loop()

    try:
        # Trip the breaker by setting the count at/above threshold and
        # stamping the open-time to "now".
        mcp_tool._server_error_counts["srv"] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        fake_now = [1000.0]

        def _fake_monotonic():
            return fake_now[0]

        monkeypatch.setattr(mcp_tool.time, "monotonic", _fake_monotonic)
        # The breaker-open timestamp dict is introduced by the fix; on
        # a pre-fix build it won't exist, which will cause the test to
        # fail at the .get() inside the gate (correct — the fix is
        # required for this state to be tracked at all).
        if hasattr(mcp_tool, "_server_breaker_opened_at"):
            mcp_tool._server_breaker_opened_at["srv"] = fake_now[0]
        cooldown = getattr(mcp_tool, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 60.0)

        handler = _make_tool_handler("srv", "tool1", 10.0)

        # Before cooldown: must short-circuit (no session call).
        result = handler({})
        parsed = json.loads(result)
        assert "error" in parsed, parsed
        assert "unreachable" in parsed["error"].lower()
        assert call_count["n"] == 0, (
            "breaker should short-circuit before cooldown elapses"
        )

        # Advance past cooldown → next call is a half-open probe that
        # actually hits the session.
        fake_now[0] += cooldown + 1.0

        result = handler({})
        parsed = json.loads(result)
        assert parsed.get("result") == "ok", parsed
        assert call_count["n"] == 1, "half-open probe should invoke session"

        # On probe success the breaker must close (count reset to 0).
        assert mcp_tool._server_error_counts.get("srv", 0) == 0
    finally:
        _cleanup(mcp_tool, "srv")


def test_circuit_breaker_reopens_on_probe_failure(monkeypatch, tmp_path):
    """If the half-open probe fails, the breaker must re-arm the
    cooldown (not let every subsequent call through).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    call_count = {"n": 0}

    async def _call_tool_fails(*a, **kw):
        call_count["n"] += 1
        raise RuntimeError("still broken")

    _install_stub_server(mcp_tool, "srv", _call_tool_fails)
    mcp_tool._ensure_mcp_loop()

    try:
        mcp_tool._server_error_counts["srv"] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        fake_now = [1000.0]

        def _fake_monotonic():
            return fake_now[0]

        monkeypatch.setattr(mcp_tool.time, "monotonic", _fake_monotonic)
        if hasattr(mcp_tool, "_server_breaker_opened_at"):
            mcp_tool._server_breaker_opened_at["srv"] = fake_now[0]
        cooldown = getattr(mcp_tool, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 60.0)

        handler = _make_tool_handler("srv", "tool1", 10.0)

        # Advance past cooldown, run probe, expect failure.
        fake_now[0] += cooldown + 1.0
        result = handler({})
        parsed = json.loads(result)
        assert "error" in parsed
        assert call_count["n"] == 1, "probe should invoke session once"

        # The probe failure must have re-armed the cooldown — another
        # immediate call should short-circuit, not invoke session again.
        result = handler({})
        parsed = json.loads(result)
        assert "unreachable" in parsed.get("error", "").lower()
        assert call_count["n"] == 1, (
            "breaker should re-open and block further calls after probe failure"
        )
    finally:
        _cleanup(mcp_tool, "srv")


def test_half_open_probe_on_dead_session_requests_reconnect(monkeypatch, tmp_path):
    """A half-open probe against a server with no live session must request
    a transport reconnect and return a clean error.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    server = _install_stub_server(mcp_tool, "srv", None)
    server.session = None
    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)

    try:
        mcp_tool._server_error_counts["srv"] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        fake_now = [1000.0]
        monkeypatch.setattr(mcp_tool.time, "monotonic", lambda: fake_now[0])
        mcp_tool._server_breaker_opened_at["srv"] = fake_now[0]
        cooldown = getattr(mcp_tool, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 60.0)
        fake_now[0] += cooldown + 1.0

        handler = _make_tool_handler("srv", "tool1", 10.0)
        parsed = json.loads(handler({}))

        assert "reconnect" in parsed.get("error", "").lower(), parsed
        server._reconnect_event.set.assert_called_once()
    finally:
        _cleanup(mcp_tool, "srv")


def test_half_open_dead_session_recovers_after_reconnect(monkeypatch, tmp_path):
    """Once the transport comes back, the next call must go straight through."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool_success(*a, **kw):
        result = MagicMock()
        result.isError = False
        block = MagicMock()
        block.text = "ok"
        result.content = [block]
        result.structuredContent = None
        return result

    server = _install_stub_server(mcp_tool, "srv", _call_tool_success)
    server.session = None
    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)
    mcp_tool._ensure_mcp_loop()

    try:
        mcp_tool._server_error_counts["srv"] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        fake_now = [1000.0]
        monkeypatch.setattr(mcp_tool.time, "monotonic", lambda: fake_now[0])
        mcp_tool._server_breaker_opened_at["srv"] = fake_now[0]
        cooldown = getattr(mcp_tool, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 60.0)
        fake_now[0] += cooldown + 1.0

        handler = _make_tool_handler("srv", "tool1", 10.0)

        parsed = json.loads(handler({}))
        assert "reconnect" in parsed.get("error", "").lower(), parsed

        live = MagicMock()
        live.call_tool = _call_tool_success
        server.session = live
        mcp_tool._reset_server_error("srv")

        fake_now[0] += cooldown + 1.0

        parsed = json.loads(handler({}))
        assert parsed.get("result") == "ok", parsed
    finally:
        _cleanup(mcp_tool, "srv")


def test_circuit_breaker_cleared_on_reconnect(monkeypatch, tmp_path):
    """When the auth-recovery path successfully reconnects the server,
    the breaker should be cleared so subsequent calls aren't gated on a
    stale failure count — even if the post-reconnect retry itself fails.

    This locks in the fix-#2 contract: a successful reconnect is
    sufficient evidence that the server is viable again. Under the old
    implementation, reset only happened on retry *success*, so a
    reconnect+retry-failure left the counter pinned above threshold
    forever.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_oauth_manager import get_manager, reset_manager_for_tests
    from mcp.client.auth import OAuthFlowError

    reset_manager_for_tests()

    async def _call_tool_unused(*a, **kw):  # pragma: no cover
        raise AssertionError("session.call_tool should not be reached in this test")

    _install_stub_server(mcp_tool, "srv", _call_tool_unused)
    mcp_tool._ensure_mcp_loop()

    # Open the breaker well above threshold, with a recent open-time so
    # it would short-circuit everything without a reset.
    mcp_tool._server_error_counts["srv"] = mcp_tool._CIRCUIT_BREAKER_THRESHOLD + 2
    if hasattr(mcp_tool, "_server_breaker_opened_at"):
        import time as _time
        mcp_tool._server_breaker_opened_at["srv"] = _time.monotonic()

    # Force handle_401 to claim recovery succeeded.
    mgr = get_manager()

    async def _h401(name, token=None):
        return True

    monkeypatch.setattr(mgr, "handle_401", _h401)

    try:
        # Retry fails *after* the successful reconnect. Under the old
        # implementation this bumps an already-tripped counter even
        # higher. Under fix #2 the reset happens on successful
        # reconnect, and the post-retry bump only raises the fresh
        # count to 1 — still below threshold.
        def _retry_call():
            raise OAuthFlowError("still failing post-reconnect")

        result = mcp_tool._handle_auth_error_and_retry(
            "srv",
            OAuthFlowError("initial"),
            _retry_call,
            "tools/call test",
        )
        # The call as a whole still surfaces needs_reauth because the
        # retry itself didn't succeed, but the breaker state must
        # reflect the successful reconnect.
        assert result is not None
        parsed = json.loads(result)
        assert parsed.get("needs_reauth") is True, parsed

        # Post-reconnect count was reset to 0, then the failing retry
        # bumped it to exactly 1 — well below threshold.
        count = mcp_tool._server_error_counts.get("srv", 0)
        assert count < mcp_tool._CIRCUIT_BREAKER_THRESHOLD, (
            f"successful reconnect must reset the breaker below threshold; "
            f"got count={count}, threshold={mcp_tool._CIRCUIT_BREAKER_THRESHOLD}"
        )
    finally:
        _cleanup(mcp_tool, "srv")


def test_run_loop_parks_instead_of_exiting_then_revives(monkeypatch, tmp_path):
    """Parked reconnect must revive the transport and restore tool registry."""
    import asyncio

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask
    from tools.registry import registry

    monkeypatch.setattr(mcp_tool, "_MAX_RECONNECT_RETRIES", 2)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(mcp_tool.asyncio, "sleep", _fast_sleep)

    tool_name = "mcp_srv_tool"
    mcp_tool_listing = SimpleNamespace(
        name="tool",
        description="test tool",
        inputSchema={"type": "object", "properties": {}},
    )
    tools_result = SimpleNamespace(tools=[mcp_tool_listing])
    config = {
        "command": "x",
        "tools": {"resources": False, "prompts": False},
    }

    state = {"transport_calls": 0, "deregistered": 0, "revived": False}

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            def _deregister_tools(self):
                state["deregistered"] += 1
                super()._deregister_tools()

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                if state["transport_calls"] == 1:
                    self.session = object()
                    self._ready.set()
                    self.session = None
                    raise RuntimeError("subprocess died")
                if state["revived"]:
                    self.session = live_session
                    await self._discover_tools()
                    self._ready.set()
                    await self._wait_for_lifecycle_event()
                    return
                raise RuntimeError("still down")

        task = _Task("srv")
        task._config = config
        task._tools = [mcp_tool_listing]
        mcp_tool._servers["srv"] = task
        task._registered_tool_names = mcp_tool._register_server_tools(
            "srv", task, task._config
        )
        assert tool_name in task._registered_tool_names
        assert registry.get_entry(tool_name) is not None

        run_task = asyncio.ensure_future(task.run(config))

        for _ in range(500):
            await real_sleep(0)
            if state["deregistered"] >= 1:
                break
        await real_sleep(0)
        assert not run_task.done(), "run loop exited instead of parking"
        assert state["deregistered"] >= 1, "tools not deregistered on park"
        assert task._registered_tool_names == []
        assert registry.get_entry(tool_name) is None

        state["revived"] = True
        live_session = MagicMock()

        async def _list_tools():
            return tools_result

        live_session.list_tools = _list_tools
        before = state["transport_calls"]
        task._reconnect_event.set()
        for _ in range(500):
            await real_sleep(0)
            if (
                state["transport_calls"] > before
                and registry.get_entry(tool_name) is not None
            ):
                break
        assert state["transport_calls"] > before, (
            "parked task did not re-enter transport on reconnect signal"
        )
        assert task._registered_tool_names == [tool_name]
        entry = registry.get_entry(tool_name)
        assert entry is not None, "parked reconnect did not restore registry entry"
        assert entry.handler is not None

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()
        finally:
            task._deregister_tools()
            mcp_tool._servers.pop("srv", None)

    asyncio.run(_scenario())

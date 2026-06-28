"""Regression tests for loading feedback on slow slash commands."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import HermesCLI


class TestCLILoadingIndicator:
    def _make_cli(self):
        cli_obj = HermesCLI.__new__(HermesCLI)
        cli_obj._app = None
        cli_obj._last_invalidate = 0.0
        cli_obj._command_running = False
        cli_obj._command_status = ""
        return cli_obj

    def test_skills_command_sets_busy_state_and_prints_status(self, capsys):
        cli_obj = self._make_cli()
        seen = {}

        def fake_handle(cmd: str):
            seen["cmd"] = cmd
            seen["running"] = cli_obj._command_running
            seen["status"] = cli_obj._command_status
            print("skills done")

        with patch.object(cli_obj, "_handle_skills_command", side_effect=fake_handle), \
             patch.object(cli_obj, "_invalidate") as invalidate_mock:
            assert cli_obj.process_command("/skills search kubernetes")

        output = capsys.readouterr().out
        assert "⏳ Searching skills..." in output
        assert "skills done" in output
        assert seen == {
            "cmd": "/skills search kubernetes",
            "running": True,
            "status": "Searching skills...",
        }
        assert cli_obj._command_running is False
        assert cli_obj._command_status == ""
        assert invalidate_mock.call_count == 2

    def test_reload_mcp_sets_busy_state_and_prints_status(self, capsys):
        cli_obj = self._make_cli()
        seen = {}

        def fake_reload():
            seen["running"] = cli_obj._command_running
            seen["status"] = cli_obj._command_status
            print("reload done")

        # /reload-mcp now wraps the actual reload in a prompt-cache-invalidation
        # confirmation prompt (commit 4d7fc0f37).  This test exercises the
        # loading-indicator path, not the confirmation UX, so pre-approve the
        # reload via config so the handler goes straight into _reload_mcp().
        fake_cfg = {"approvals": {"mcp_reload_confirm": False}}

        with patch.object(cli_obj, "_reload_mcp", side_effect=fake_reload), \
             patch.object(cli_obj, "_invalidate") as invalidate_mock, \
             patch("cli.load_cli_config", return_value=fake_cfg):
            assert cli_obj.process_command("/reload-mcp")

        output = capsys.readouterr().out
        assert "⏳ Reloading MCP servers..." in output
        assert "reload done" in output
        assert seen == {
            "running": True,
            "status": "Reloading MCP servers...",
        }
        assert cli_obj._command_running is False
        assert cli_obj._command_status == ""
        assert invalidate_mock.call_count == 2

    def test_reload_mcp_merges_connected_servers_into_shared_refresh(self):
        cli_obj = self._make_cli()
        cli_obj.enabled_toolsets = ["web"]
        cli_obj.conversation_history = []
        cli_obj.agent = SimpleNamespace(
            enabled_toolsets=["web"],
            disabled_toolsets=None,
            tools=[],
            valid_tool_names=set(),
            _invalidate_system_prompt=MagicMock(),
            _persist_session=MagicMock(),
        )

        from tools import mcp_tool as mcp_mod

        saved_servers = dict(mcp_mod._servers)
        try:
            captured = {}

            def _discover():
                mcp_mod._servers.clear()
                mcp_mod._servers["mcp_new"] = SimpleNamespace(name="mcp_new")
                return ["mcp_new_tool"]

            def _refresh(agent, *, enabled_override=None, quiet_mode=True):
                captured["agent"] = agent
                captured["enabled_override"] = enabled_override
                captured["quiet_mode"] = quiet_mode
                agent.tools = [{"function": {"name": "mcp_new_tool"}}]
                agent.valid_tool_names = {"mcp_new_tool"}
                return {"mcp_new_tool"}

            with patch("tools.mcp_tool._servers", {}), \
                 patch("tools.mcp_tool.shutdown_mcp_servers", return_value=None), \
                 patch("tools.mcp_tool.discover_mcp_tools", side_effect=_discover), \
                 patch("tools.mcp_tool.refresh_agent_mcp_tools", side_effect=_refresh):
                cli_obj._reload_mcp()

            assert captured["agent"] is cli_obj.agent
            assert captured["enabled_override"] == ["web", "mcp_new"]
            assert captured["quiet_mode"] is True
            assert cli_obj.enabled_toolsets == ["web", "mcp_new"]
            cli_obj.agent._invalidate_system_prompt.assert_called_once()
        finally:
            mcp_mod._servers.clear()
            mcp_mod._servers.update(saved_servers)

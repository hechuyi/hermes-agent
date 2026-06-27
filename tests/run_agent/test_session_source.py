from unittest.mock import MagicMock, patch

from gateway.session_context import clear_session_vars, set_session_vars
from run_agent import AIAgent


def _make_agent(session_db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            platform="platform-param",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
        )


def test_gateway_session_context_source_overrides_platform_and_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "env-source")
    tokens = set_session_vars(platform="gateway-source")
    try:
        session_db = MagicMock()
        agent = _make_agent(session_db)

        agent._ensure_db_session()

        assert session_db.create_session.call_args.kwargs["source"] == "gateway-source"
    finally:
        clear_session_vars(tokens)


def test_platform_source_used_when_gateway_context_has_no_platform(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "env-source")
    tokens = set_session_vars(platform="")
    try:
        session_db = MagicMock()
        agent = _make_agent(session_db)

        agent._ensure_db_session()

        assert session_db.create_session.call_args.kwargs["source"] == "platform-param"
    finally:
        clear_session_vars(tokens)

"""Tests for gateway /fast support and Priority Processing routing."""

import json
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


class _ScopedSearchAgent(_CapturingAgent):
    last_search_result = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = kwargs.get("session_id")
        self._session_db = kwargs.get("session_db")
        self._api_call_count = 17

    def _get_session_db_for_recall(self):
        return self._session_db

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        self.session_id = task_id
        from agent.agent_runtime_helpers import invoke_tool

        type(self).last_search_result = invoke_tool(
            self,
            "session_search",
            {"query": "scoped needle"},
            task_id,
        )
        return super().run_conversation(user_message, conversation_history, task_id, persist_user_message)


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _install_scoped_search_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ScopedSearchAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_session_key_fallback_uses_group_per_user_default_when_store_errors():
    runner = _make_runner()
    runner.config = SimpleNamespace(thread_sessions_per_user=False)
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_group",
        chat_type="group",
        user_id="ou_user",
        user_id_alt="on_union",
    )

    assert runner._session_key_for_source(source) == "agent:main:feishu:group:oc_group:on_union"


def test_session_key_fallback_uses_feishu_effective_chat_shared_default_when_store_errors():
    from gateway.config import GatewayConfig

    runner = _make_runner()
    runner.config = GatewayConfig()
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_group",
        chat_type="group",
        user_id="ou_user",
        user_id_alt="on_union",
    )

    assert runner._session_key_for_source(source) == "agent:main:feishu:group:oc_group"


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}


def test_turn_route_skips_priority_processing_for_unsupported_models():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.3-codex", runtime_kwargs)

    assert route["request_overrides"] == {}


@pytest.mark.asyncio
async def test_handle_fast_command_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await runner._handle_fast_command(_make_event("/fast fast"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"


@pytest.mark.asyncio
async def test_run_agent_passes_priority_processing_to_gateway_agent(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    # ``_load_service_tier`` was refactored to call ``_load_gateway_runtime_config``
    # (which wraps ``_load_gateway_config`` plus env-expansion).  Since the test
    # stubs ``_load_gateway_config`` to ``{}``, also stub the runtime wrapper
    # directly so the priority routing assertions still exercise the live tier.
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None
    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["service_tier"] == "priority"
    assert _CapturingAgent.last_init["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_fresh_gateway_agent_receives_scope_for_current_chat_session_search(monkeypatch, tmp_path):
    _install_scoped_search_agent(monkeypatch)
    runner = _make_runner()

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(
        "current",
        "feishu",
        conversation_scope_id="cs_current",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-current",
        route_partition_key="route-current",
    )
    db.create_session(
        "same-chat",
        "feishu",
        conversation_scope_id="cs_current",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-same",
        route_partition_key="route-same",
    )
    db.append_message("same-chat", "user", "scoped needle")
    db.create_session(
        "other-chat",
        "feishu",
        conversation_scope_id="cs_other",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-other",
        route_partition_key="route-other",
    )
    db.append_message("other-chat", "user", "scoped needle")
    db._conn.commit()

    runner._session_db = db
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_current",
        chat_type="group",
        user_id="ou_user",
        user_id_alt="on_user",
    )
    session_key = "agent:main:feishu:group:oc_current:on_user"
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda _source: SimpleNamespace(
            session_id="current",
            conversation_scope_id="cs_current",
            platform_account_id="feishu_app:test",
            route_partition_key="route-current",
        ),
        load_transcript=lambda _session_id: [],
    )

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"session_search"})

    _ScopedSearchAgent.last_init = None
    _ScopedSearchAgent.last_search_result = None

    result = await runner._run_agent(
        message="recall",
        context_prompt="",
        history=[],
        source=source,
        session_id="current",
        session_key=session_key,
    )

    assert result["final_response"] == "ok"
    assert _ScopedSearchAgent.last_init["gateway_session_key"] == session_key

    payload = json.loads(_ScopedSearchAgent.last_search_result)
    assert payload["success"] is True
    assert [item["session_id"] for item in payload["results"]] == ["same-chat"]

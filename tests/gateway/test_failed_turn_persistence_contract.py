from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="dm",
        user_id="u1",
    )


def _event(text: str = "blocked prompt") -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id="m1")


def _runner_with_mock_store(agent_result: dict):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    session_entry = SessionEntry(
        session_key="agent:main:discord:dm:c1",
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="dm",
    )
    session_store = MagicMock()
    session_store.get_or_create_session.return_value = session_entry
    session_store.load_transcript.return_value = []
    session_store.has_any_sessions.return_value = True
    session_store.update_session = MagicMock()
    session_store.clear_resume_pending = MagicMock()

    runner.session_store = session_store
    runner.adapters = {
        Platform.DISCORD: SimpleNamespace(
            stop_typing=AsyncMock(),
            send=AsyncMock(),
        )
    }
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    runner._session_db = None
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._voice_mode = {}
    runner._pending_model_notes = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_model_overrides = {}
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda *_args, **_kwargs: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._bind_adapter_run_generation = lambda *_args, **_kwargs: None
    runner._is_session_run_current = lambda *_args, **_kwargs: True
    runner._set_session_env = lambda _context: []
    runner._clear_restart_failure_count = lambda _session_key: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._run_agent = AsyncMock(return_value=agent_result)
    return runner, session_entry, session_store


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", [{"attempted": True, "ok": False}, None])
async def test_failed_turn_with_visible_assistant_final_is_gateway_persisted_when_uncovered(
    monkeypatch,
    persistence,
):
    import gateway.run as gateway_run

    messages = [
        {"role": "user", "content": "blocked prompt"},
        {"role": "assistant", "content": "I cannot help with that request."},
    ]
    agent_result = {
        "failed": True,
        "final_response": "I cannot help with that request.",
        "messages": messages,
        "history_offset": 0,
        "error": "content_policy",
        "last_prompt_tokens": 0,
    }
    if persistence is not None:
        agent_result["persistence"] = persistence
    runner, session_entry, session_store = _runner_with_mock_store(agent_result)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(
        _event(),
        _source(),
        session_entry.session_key,
        1,
    )

    assert result == "I cannot help with that request."
    transcript_writes = [
        call
        for call in session_store.append_to_transcript.call_args_list
        if call.args[1].get("role") in {"user", "assistant"}
    ]
    assert [call.args[1]["role"] for call in transcript_writes] == ["user", "assistant"]
    assistant_write = transcript_writes[-1]
    assert assistant_write.args[1]["content"] == "I cannot help with that request."
    assert assistant_write.kwargs.get("skip_db", False) is False


@pytest.mark.asyncio
async def test_failed_turn_does_not_rewrite_proof_covered_assistant_final(monkeypatch):
    import gateway.run as gateway_run

    messages = [
        {"role": "user", "content": "blocked prompt"},
        {"role": "assistant", "content": "I cannot help with that request."},
    ]
    runner, session_entry, session_store = _runner_with_mock_store(
        {
            "failed": True,
            "final_response": "I cannot help with that request.",
            "messages": messages,
            "history_offset": 0,
            "error": "content_policy",
            "last_prompt_tokens": 0,
            "persistence": {
                "attempted": True,
                "ok": True,
                "row_ids_by_message_index": {0: 101, 1: 102},
            },
        }
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(
        _event(),
        _source(),
        session_entry.session_key,
        1,
    )

    assert result == "I cannot help with that request."
    assistant_writes = [
        call
        for call in session_store.append_to_transcript.call_args_list
        if call.args[1].get("role") == "assistant"
    ]
    assert assistant_writes
    assert all(call.kwargs.get("skip_db", False) is True for call in assistant_writes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assistant_error_text",
    [
        "API call failed after 3 retries: 500 Internal Server Error",
        "⚠️ The model provider failed after retries. Check gateway logs.",
    ],
)
async def test_transient_provider_failure_persists_user_but_not_gateway_error_hint(
    monkeypatch,
    assistant_error_text,
):
    import gateway.run as gateway_run

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": assistant_error_text},
    ]
    runner, session_entry, session_store = _runner_with_mock_store(
        {
            "failed": True,
            "final_response": assistant_error_text,
            "messages": messages,
            "history_offset": 0,
            "error": assistant_error_text,
            "last_prompt_tokens": 0,
        }
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(
        _event("hello"),
        _source(),
        session_entry.session_key,
        1,
    )

    assert result == assistant_error_text
    transcript_writes = [
        call
        for call in session_store.append_to_transcript.call_args_list
        if call.args[1].get("role") in {"user", "assistant"}
    ]
    assert [call.args[1]["role"] for call in transcript_writes] == ["user"]
    assert transcript_writes[0].args[1]["content"] == "hello"
    assert transcript_writes[0].kwargs.get("skip_db", False) is False


@pytest.mark.asyncio
async def test_gateway_db_append_failure_returns_explicit_error_instead_of_success_ack(
    monkeypatch,
    tmp_path,
):
    import gateway.run as gateway_run
    import hermes_state
    from gateway.session import SessionStore

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    fake_db = MagicMock()
    fake_db.get_session.return_value = None
    fake_db.get_messages_as_conversation.return_value = []
    fake_db.session_count.return_value = 2
    fake_db.append_message.side_effect = RuntimeError("sqlite append failed")
    store._db = fake_db

    runner, session_entry, _mock_store = _runner_with_mock_store(
        {
            "final_response": "ok",
            "messages": [{"role": "user", "content": "hello"}],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    runner.session_store = store
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(
        _event("hello"),
        _source(),
        session_entry.session_key,
        1,
    )

    assert result != "ok"
    assert "SessionPersistenceError" in result

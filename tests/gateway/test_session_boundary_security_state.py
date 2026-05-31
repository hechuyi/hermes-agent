"""Regression tests for approval-state cleanup on session boundaries."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from tools import approval as approval_mod
from tools import slash_confirm as slash_confirm_mod
from tools.approval import (
    _ApprovalEntry,
    approve_session,
    enable_session_yolo,
    is_approved,
    is_session_yolo_enabled,
)


@pytest.fixture(autouse=True)
def _clear_approval_state():
    approval_mod._gateway_queues.clear()
    approval_mod._gateway_notify_cbs.clear()
    approval_mod._session_approved.clear()
    approval_mod._session_yolo.clear()
    approval_mod._permanent_approved.clear()
    approval_mod._pending.clear()
    slash_confirm_mod._pending.clear()
    yield
    approval_mod._gateway_queues.clear()
    approval_mod._gateway_notify_cbs.clear()
    approval_mod._session_approved.clear()
    approval_mod._session_yolo.clear()
    approval_mod._permanent_approved.clear()
    approval_mod._pending.clear()
    slash_confirm_mod._pending.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_entry(session_id: str, source: SessionSource | None = None) -> SessionEntry:
    source = source or _make_source()
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
    )


def _make_resume_runner():
    from gateway.run import GatewayRunner

    source = _make_source()
    session_key = build_session_key(source)
    current_entry = _make_entry("current-session", source)
    resumed_entry = _make_entry("resumed-session", source)

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = current_entry
    runner.session_store.switch_session.return_value = resumed_entry
    runner.session_store.load_transcript.return_value = []
    runner._session_db = MagicMock()
    runner._session_db.resolve_session_by_title.return_value = "resumed-session"
    runner._session_db.get_session_title.return_value = "Resumed Work"
    return runner, session_key


def _make_branch_runner():
    from gateway.run import GatewayRunner

    source = _make_source()
    session_key = build_session_key(source)
    current_entry = _make_entry("current-session", source)
    branched_entry = _make_entry("branched-session", source)

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = current_entry
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    runner.session_store.switch_session.return_value = branched_entry
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = "Current Work"
    runner._session_db.get_next_title_in_lineage.return_value = "Current Work #2"
    return runner, session_key


@pytest.mark.asyncio
async def test_resume_clears_session_scoped_approval_and_yolo_state():
    runner, session_key = _make_resume_runner()
    other_key = "agent:main:telegram:dm:other-chat"

    runner._pending_skills_reload_notes = {
        session_key: "[USER INITIATED SKILLS RELOAD: target]",
        other_key: "[USER INITIATED SKILLS RELOAD: other]",
    }
    approve_session(session_key, "recursive delete")
    approve_session(other_key, "recursive delete")
    enable_session_yolo(session_key)
    enable_session_yolo(other_key)
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp/demo"}
    runner._pending_approvals[other_key] = {"command": "rm -rf /tmp/other"}
    runner._update_prompt_pending[session_key] = True
    runner._update_prompt_pending[other_key] = True

    result = await runner._handle_resume_command(_make_event("/resume Resumed Work"))

    assert "Resumed session" in result
    assert is_approved(session_key, "recursive delete") is False
    assert is_session_yolo_enabled(session_key) is False
    assert session_key not in runner._pending_approvals
    assert session_key not in runner._update_prompt_pending
    assert session_key not in runner._pending_skills_reload_notes
    assert is_approved(other_key, "recursive delete") is True
    assert is_session_yolo_enabled(other_key) is True
    assert other_key in runner._pending_approvals
    assert other_key in runner._update_prompt_pending
    assert other_key in runner._pending_skills_reload_notes


@pytest.mark.asyncio
async def test_branch_clears_session_scoped_approval_and_yolo_state():
    runner, session_key = _make_branch_runner()
    other_key = "agent:main:telegram:dm:other-chat"

    runner._pending_skills_reload_notes = {
        session_key: "[USER INITIATED SKILLS RELOAD: target]",
        other_key: "[USER INITIATED SKILLS RELOAD: other]",
    }
    approve_session(session_key, "recursive delete")
    approve_session(other_key, "recursive delete")
    enable_session_yolo(session_key)
    enable_session_yolo(other_key)
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp/demo"}
    runner._pending_approvals[other_key] = {"command": "rm -rf /tmp/other"}
    runner._update_prompt_pending[session_key] = True
    runner._update_prompt_pending[other_key] = True

    result = await runner._handle_branch_command(_make_event("/branch"))

    assert "Branched to" in result
    assert is_approved(session_key, "recursive delete") is False
    assert is_session_yolo_enabled(session_key) is False
    assert session_key not in runner._pending_approvals
    assert session_key not in runner._update_prompt_pending
    assert session_key not in runner._pending_skills_reload_notes
    assert is_approved(other_key, "recursive delete") is True
    assert is_session_yolo_enabled(other_key) is True
    assert other_key in runner._pending_approvals
    assert other_key in runner._update_prompt_pending
    assert other_key in runner._pending_skills_reload_notes


@pytest.mark.asyncio
async def test_branch_preserves_persisted_assistant_metadata():
    runner, _session_key = _make_branch_runner()
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "world",
            "finish_reason": "stop",
            "reasoning": "thinking",
            "reasoning_content": "provider scratchpad",
            "reasoning_details": [{"type": "summary", "text": "step"}],
            "codex_reasoning_items": [{"id": "r1", "type": "reasoning"}],
            "codex_message_items": [{"id": "m1", "type": "message"}],
        },
    ]

    result = await runner._handle_branch_command(_make_event("/branch"))

    assert "Branched to" in result
    append_calls = runner._session_db.append_message.call_args_list
    assert len(append_calls) == 2
    assistant_kwargs = append_calls[1].kwargs
    assert assistant_kwargs["role"] == "assistant"
    assert assistant_kwargs["finish_reason"] == "stop"
    assert assistant_kwargs["reasoning"] == "thinking"
    assert assistant_kwargs["reasoning_content"] == "provider scratchpad"
    assert assistant_kwargs["reasoning_details"] == [{"type": "summary", "text": "step"}]
    assert assistant_kwargs["codex_reasoning_items"] == [{"id": "r1", "type": "reasoning"}]
    assert assistant_kwargs["codex_message_items"] == [{"id": "m1", "type": "message"}]


@pytest.mark.asyncio
async def test_branch_command_copies_parent_scope_and_switch_entry_keeps_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from gateway.config import GatewayConfig, PlatformConfig, SessionResetPolicy
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB

    source = SessionSource(
        platform=Platform.FEISHU,
        user_id="ou_user",
        user_id_alt="on_union",
        chat_id="oc_group",
        chat_type="group",
        user_name="tester",
    )
    event = MessageEvent(text="/branch scoped-copy", source=source, message_id="m1")
    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_branch_scope"},
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = db
    current = store.get_or_create_session(source)
    store.append_to_transcript(current.session_id, {"role": "user", "content": "hello"})

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = config
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner._background_tasks = set()
    runner.session_store = store
    runner._session_db = db

    result = await runner._handle_branch_command(event)
    branched = store._entries[current.session_key]
    row = db.get_session(branched.session_id)

    assert "Branched to" in result
    assert row["parent_session_id"] == current.session_id
    assert row["scope_assignment_status"] == "scoped"
    assert row["conversation_scope_id"] == current.conversation_scope_id
    assert row["route_partition_key"] == current.route_partition_key
    assert branched.conversation_scope_id == current.conversation_scope_id
    assert branched.platform_account_id == current.platform_account_id
    assert branched.route_partition_key == current.route_partition_key


@pytest.mark.asyncio
async def test_branch_command_rejects_current_entry_scope_when_parent_db_row_is_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from gateway.config import GatewayConfig, PlatformConfig, SessionResetPolicy
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB

    source = SessionSource(
        platform=Platform.FEISHU,
        user_id="ou_user",
        user_id_alt="on_union",
        chat_id="oc_group",
        chat_type="group",
        user_name="tester",
    )
    event = MessageEvent(text="/branch fallback-scope", source=source, message_id="m1")
    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_branch_scope_fallback"},
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = db
    current = store.get_or_create_session(source)
    db._conn.execute(
        "UPDATE sessions SET conversation_scope_id=NULL, scope_assignment_status='legacy_unscoped', "
        "route_session_key_snapshot=NULL, route_partition_key=NULL WHERE id=?",
        (current.session_id,),
    )
    db._conn.commit()
    store.append_to_transcript(current.session_id, {"role": "user", "content": "hello"})

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = config
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner._background_tasks = set()
    runner.session_store = store
    runner._session_db = db

    result = await runner._handle_branch_command(event)
    branched = store._entries[current.session_key]

    assert result == "No conversation to branch — send a message first."
    assert branched.session_id != current.session_id
    assert branched.conversation_scope_id == current.conversation_scope_id
    assert branched.route_partition_key == current.route_partition_key


@pytest.mark.asyncio
async def test_branch_command_rejects_current_entry_scope_when_parent_db_row_is_legacy_even_if_store_returns_entry(tmp_path):
    from datetime import datetime
    from unittest.mock import MagicMock
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB

    source = SessionSource(
        platform=Platform.FEISHU,
        user_id="ou_user",
        user_id_alt="on_union",
        chat_id="oc_group",
        chat_type="group",
        user_name="tester",
    )
    event = MessageEvent(text="/branch fallback-scope", source=source, message_id="m1")
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_session_id = "legacy-parent-row"
    db.create_session(parent_session_id, "feishu")

    current_entry = SessionEntry(
        session_key=build_session_key(source),
        session_id=parent_session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        conversation_scope_id="cs_entry",
        platform_account_id="feishu_app:test",
        route_partition_key="route-entry",
    )

    switched_entry = SessionEntry(
        session_key=current_entry.session_key,
        session_id="unused",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        conversation_scope_id="cs_entry",
        platform_account_id="feishu_app:test",
        route_partition_key="route-entry",
    )
    store = MagicMock()
    store.get_or_create_session.return_value = current_entry
    store.load_transcript.return_value = [{"role": "user", "content": "hello"}]
    store.switch_session.return_value = switched_entry

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner._background_tasks = set()
    runner.session_store = store
    runner._session_db = db

    result = await runner._handle_branch_command(event)

    assert result == "No conversation to branch — send a message first."
    store.switch_session.assert_not_called()


def test_clear_session_boundary_security_state_is_scoped():
    """The helper must wipe only the target session's approval/yolo state.

    Also exercises the /new reset path indirectly: /new calls this helper,
    so if the helper is scoped correctly, /new's clearing is correct too.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._pending_skills_reload_notes = {}

    source = _make_source()
    session_key = build_session_key(source)
    other_key = "agent:main:telegram:dm:other-chat"

    approve_session(session_key, "recursive delete")
    approve_session(other_key, "recursive delete")
    enable_session_yolo(session_key)
    enable_session_yolo(other_key)
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp/demo"}
    runner._pending_approvals[other_key] = {"command": "rm -rf /tmp/other"}
    runner._update_prompt_pending[session_key] = True
    runner._update_prompt_pending[other_key] = True
    runner._pending_skills_reload_notes[session_key] = (
        "[USER INITIATED SKILLS RELOAD: target]"
    )
    runner._pending_skills_reload_notes[other_key] = (
        "[USER INITIATED SKILLS RELOAD: other]"
    )

    async def _target_handler(choice):
        return f"target:{choice}"

    async def _other_handler(choice):
        return f"other:{choice}"

    slash_confirm_mod.register(session_key, "confirm-target", "reload-mcp", _target_handler)
    slash_confirm_mod.register(other_key, "confirm-other", "reload-mcp", _other_handler)

    runner._clear_session_boundary_security_state(session_key)

    # Target session cleared
    assert is_approved(session_key, "recursive delete") is False
    assert is_session_yolo_enabled(session_key) is False
    assert session_key not in runner._pending_approvals
    assert session_key not in runner._update_prompt_pending
    assert session_key not in runner._pending_skills_reload_notes
    assert slash_confirm_mod.get_pending(session_key) is None
    # Other session untouched
    assert is_approved(other_key, "recursive delete") is True
    assert is_session_yolo_enabled(other_key) is True
    assert other_key in runner._pending_approvals
    assert other_key in runner._update_prompt_pending
    assert other_key in runner._pending_skills_reload_notes
    assert slash_confirm_mod.get_pending(other_key) is not None

    # Empty session_key is a no-op
    runner._clear_session_boundary_security_state("")
    assert is_approved(other_key, "recursive delete") is True
    assert other_key in runner._update_prompt_pending
    assert other_key in runner._pending_skills_reload_notes
    assert slash_confirm_mod.get_pending(other_key) is not None


def test_clear_session_boundary_security_state_wakes_blocked_approvals():
    """Boundary cleanup must cancel blocked approval waiters immediately."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}

    source = _make_source()
    session_key = build_session_key(source)
    other_key = "agent:main:telegram:dm:other-chat"

    target_entry = _ApprovalEntry({"command": "rm -rf /tmp/demo"})
    other_entry = _ApprovalEntry({"command": "rm -rf /tmp/other"})
    approval_mod._gateway_queues[session_key] = [target_entry]
    approval_mod._gateway_queues[other_key] = [other_entry]

    runner._clear_session_boundary_security_state(session_key)

    assert target_entry.event.is_set()
    assert target_entry.result == "deny"
    assert other_entry.event.is_set() is False
    assert other_entry.result is None
    assert session_key not in approval_mod._gateway_queues
    assert other_key in approval_mod._gateway_queues

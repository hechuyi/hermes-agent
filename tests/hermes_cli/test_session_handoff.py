"""Tests for session handoff (CLI to gateway platform).

The handoff state machine lives on the ``sessions`` table:

    None  → "pending" → "running" → ("completed" | "failed")

CLI side calls ``request_handoff`` and poll-waits on ``get_handoff_state``.
Gateway side iterates ``list_pending_handoffs``, calls ``claim_handoff`` to
flip pending → running, and finishes with ``complete_handoff`` or
``fail_handoff``.
"""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB


class TestHandoffStateDB:
    """Test the handoff schema + helper methods on SessionDB."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        return SessionDB(db_path=home / "state.db")

    def _make_session(self, db, session_id, source="cli", title=None):
        """Insert a session row directly for testing."""
        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, source, title, started_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, source, title, time.time()),
            )
        db._execute_write(_do)

    def test_columns_exist(self, db):
        db._conn.execute(
            "SELECT handoff_state, handoff_platform, handoff_error "
            "FROM sessions LIMIT 0"
        )

    def test_request_handoff_marks_pending(self, db):
        sid = "sess-1"
        self._make_session(db, sid)

        assert db.request_handoff(sid, "telegram") is True

        state = db.get_handoff_state(sid)
        assert state == {
            "state": "pending",
            "platform": "telegram",
            "error": None,
        }

    def test_request_handoff_rejects_in_flight(self, db):
        sid = "sess-2"
        self._make_session(db, sid)

        assert db.request_handoff(sid, "telegram") is True
        # Still pending → reject re-request
        assert db.request_handoff(sid, "discord") is False

        # And after gateway claims it (running) → still rejected
        assert db.claim_handoff(sid) is True
        assert db.request_handoff(sid, "discord") is False

    def test_request_handoff_after_terminal_state_resets_error(self, db):
        sid = "sess-3"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "earlier failure")

        # User retries — should be allowed and clear the prior error.
        assert db.request_handoff(sid, "discord") is True
        state = db.get_handoff_state(sid)
        assert state["state"] == "pending"
        assert state["platform"] == "discord"
        assert state["error"] is None

    def test_list_pending_handoffs_excludes_running_and_terminal(self, db):
        a, b, c, d = "sess-a", "sess-b", "sess-c", "sess-d"
        for sid in (a, b, c, d):
            self._make_session(db, sid)

        db.request_handoff(a, "telegram")
        db.request_handoff(b, "discord")
        db.request_handoff(c, "telegram")
        db.claim_handoff(c)  # c is now running, not pending
        db.request_handoff(d, "slack")
        db.claim_handoff(d)
        db.complete_handoff(d)  # d is terminal

        pending = db.list_pending_handoffs()
        ids = [r["id"] for r in pending]
        assert set(ids) == {a, b}

    def test_claim_handoff_is_atomic(self, db):
        sid = "sess-claim"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")

        # First claim wins
        assert db.claim_handoff(sid) is True
        # Second claim is a no-op (state is now "running", not "pending")
        assert db.claim_handoff(sid) is False
        assert db.get_handoff_state(sid)["state"] == "running"

    def test_complete_handoff_clears_error(self, db):
        sid = "sess-complete"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "transient")
        # User retries; mock the watcher path
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.complete_handoff(sid)

        state = db.get_handoff_state(sid)
        assert state["state"] == "completed"
        assert state["error"] is None

    def test_fail_handoff_records_reason(self, db):
        sid = "sess-fail"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "no home channel for telegram")

        state = db.get_handoff_state(sid)
        assert state["state"] == "failed"
        assert state["error"] == "no home channel for telegram"

    def test_fail_handoff_truncates_long_reasons(self, db):
        sid = "sess-fail-long"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)

        # 1000-character error string
        big_err = "x" * 1000
        db.fail_handoff(sid, big_err)

        state = db.get_handoff_state(sid)
        assert len(state["error"]) <= 500

    def test_get_handoff_state_for_unknown_session(self, db):
        assert db.get_handoff_state("does-not-exist") is None

    def test_full_pending_to_completed_flow(self, db):
        """End-to-end sequence the CLI + gateway watcher follow."""
        sid = "sess-flow"
        self._make_session(db, sid, title="my session")
        db.append_message(sid, "user", "Hello")
        db.append_message(sid, "assistant", "Hi there!")

        # CLI: request handoff
        assert db.request_handoff(sid, "telegram") is True
        assert db.get_handoff_state(sid)["state"] == "pending"

        # Gateway watcher: discover + claim
        pending = db.list_pending_handoffs()
        assert len(pending) == 1
        assert pending[0]["id"] == sid
        assert db.claim_handoff(sid) is True
        assert db.get_handoff_state(sid)["state"] == "running"

        # Gateway uses get_messages to load the transcript (real flow uses
        # session_store.switch_session which reads the same table).
        messages = db.get_messages(sid)
        assert [m["role"] for m in messages] == ["user", "assistant"]

        # Gateway: mark completed
        db.complete_handoff(sid)
        assert db.get_handoff_state(sid)["state"] == "completed"
        assert db.list_pending_handoffs() == []


class TestHandoffCommandRegistration:
    """Slash-command surface checks."""

    def test_command_registered(self):
        from hermes_cli.commands import resolve_command
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.name == "handoff"
        assert cmd.category == "Session"

    def test_command_is_cli_only(self):
        """`/handoff` is initiated from the CLI; gateway shouldn't expose it."""
        from hermes_cli.commands import resolve_command, GATEWAY_KNOWN_COMMANDS
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.cli_only is True
        assert "handoff" not in GATEWAY_KNOWN_COMMANDS


def test_cli_handoff_empty_session_uses_ensure_session_not_title_update(monkeypatch, tmp_path):
    """An empty CLI handoff must explicitly create the row it marks pending."""
    from cli import HermesCLI
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig

    home = tmp_path / ".hermes"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")

    gw_config = GatewayConfig()
    gw_config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test-token",
        home_channel=HomeChannel(platform=Platform.TELEGRAM, chat_id="123", name="home"),
    )

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: gw_config)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    polls = {"count": 0}
    original_get_handoff_state = db.get_handoff_state

    def complete_after_request(session_id):
        state = original_get_handoff_state(session_id)
        polls["count"] += 1
        if polls["count"] >= 2:
            db.claim_handoff(session_id)
            db.complete_handoff(session_id)
            state = original_get_handoff_state(session_id)
        return state

    db.get_handoff_state = complete_after_request

    def fail_if_title_insert_is_used(*_args, **_kwargs):
        raise AssertionError("empty-session handoff must not rely on set_session_title()")

    db.set_session_title = fail_if_title_insert_is_used

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "empty-cli-session"
    cli._session_db = db
    cli._agent_running = False
    cli._should_exit = False

    assert HermesCLI._handle_handoff_command(cli, "/handoff telegram") is False

    row = db.get_session("empty-cli-session")
    assert row is not None
    assert row["source"] == "cli"
    assert row["scope_assignment_status"] in {None, "legacy_unscoped"}


@pytest.mark.asyncio
async def test_gateway_handoff_rejects_legacy_cli_session_to_feishu_destination_scope(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore, build_session_key

    home = tmp_path / ".hermes"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.create_session("cli-session", "cli")
    db.request_handoff("cli-session", "feishu")

    config = GatewayConfig()
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        home_channel=HomeChannel(
            platform=Platform.FEISHU,
            chat_id="oc_home",
            name="Feishu Home",
        ),
        extra={"app_id": "cli_handoff_feishu"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = db

    class Adapter:
        async def create_handoff_thread(self, chat_id, thread_name):
            return None

        async def send(self, chat_id, content, metadata=None):
            return True

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {Platform.FEISHU: Adapter()}
    runner.session_store = store
    runner._session_db = db
    runner._evict_cached_agent = lambda session_key: None
    runner._release_running_agent_state = lambda session_key: None

    seen = {}

    async def fake_handle_message(event):
        entry = store.get_or_create_session(event.source)
        seen["entry"] = entry
        return "handoff accepted"

    runner._handle_message = fake_handle_message
    row = db.list_pending_handoffs()[0]

    dest_source = None
    with pytest.raises(RuntimeError, match="scope|handoff|switch"):
        await GatewayRunner._process_handoff(runner, row)

    if seen:
        dest_source = seen["entry"].origin
    else:
        from gateway.session import SessionSource

        dest_source = SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_home",
            chat_name="Feishu Home",
            chat_type="dm",
            user_id="system:handoff",
            user_name="Handoff",
        )
    dest_key = build_session_key(
        dest_source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    cli_row = db.get_session("cli-session")
    assert cli_row["scope_assignment_status"] in {None, "legacy_unscoped"}
    assert cli_row["conversation_scope_id"] is None
    assert cli_row["route_partition_key"] is None
    assert dest_key not in store._entries or store._entries[dest_key].session_id != "cli-session"


@pytest.mark.asyncio
async def test_gateway_handoff_rejects_feishu_cli_session_with_conflicting_scope(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore

    home = tmp_path / ".hermes"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.create_session(
        "cli-session",
        "cli",
        conversation_scope_id="cs_other",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-other",
        route_partition_key="route-other",
    )
    db.request_handoff("cli-session", "feishu")

    config = GatewayConfig()
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        home_channel=HomeChannel(
            platform=Platform.FEISHU,
            chat_id="oc_home",
            name="Feishu Home",
        ),
        extra={"app_id": "cli_handoff_feishu"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = db

    class Adapter:
        async def create_handoff_thread(self, chat_id, thread_name):
            return None

        async def send(self, chat_id, content, metadata=None):
            raise AssertionError("handoff must fail before sending")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {Platform.FEISHU: Adapter()}
    runner.session_store = store
    runner._session_db = db
    runner._evict_cached_agent = lambda session_key: None
    runner._release_running_agent_state = lambda session_key: None

    async def fail_if_dispatched(event):
        raise AssertionError("handoff must fail before dispatch")

    runner._handle_message = fail_if_dispatched
    row = db.list_pending_handoffs()[0]

    with pytest.raises(RuntimeError, match="scope|switch"):
        await GatewayRunner._process_handoff(runner, row)

    cli_row = db.get_session("cli-session")
    assert cli_row["conversation_scope_id"] == "cs_other"
    assert cli_row["route_partition_key"] == "route-other"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("top_level_key", "top_level_value", "extra_value"),
    [
        ("group_sessions_per_user", True, False),
        ("thread_sessions_per_user", True, False),
    ],
)
async def test_gateway_handoff_rejects_feishu_route_key_config_divergence(
    monkeypatch,
    tmp_path,
    top_level_key,
    top_level_value,
    extra_value,
):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
    from gateway.conversation_scope import (
        conversation_identity,
        feishu_platform_account_id,
        route_partition_key,
    )
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore

    home = tmp_path / ".hermes"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_home",
        chat_name="Feishu Home",
        chat_type="thread" if top_level_key == "thread_sessions_per_user" else "group",
        user_id="system:handoff",
        user_name="Handoff",
        thread_id="omt_home" if top_level_key == "thread_sessions_per_user" else None,
    )

    config = GatewayConfig()
    setattr(config, top_level_key, top_level_value)
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        home_channel=HomeChannel(
            platform=Platform.FEISHU,
            chat_id="oc_home",
            name="Feishu Home",
            thread_id=source.thread_id,
        ),
        extra={
            "app_id": "cli_handoff_feishu_divergence",
            top_level_key: extra_value,
        },
    )
    account_id = feishu_platform_account_id(config=config.platforms[Platform.FEISHU])
    scope = conversation_identity(source, platform_account_id=account_id)
    top_level_route = route_partition_key(
        source,
        group_sessions_per_user=config.group_sessions_per_user,
        thread_sessions_per_user=config.thread_sessions_per_user,
    )
    extra_route = route_partition_key(
        source,
        group_sessions_per_user=config.platforms[Platform.FEISHU].extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=config.platforms[Platform.FEISHU].extra.get("thread_sessions_per_user", False),
    )
    assert extra_route != top_level_route

    db.create_session(
        "cli-session",
        "cli",
        conversation_scope_id=scope.id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=top_level_route,
        route_partition_key=top_level_route,
    )
    db.upsert_conversation_scope(scope)
    db.request_handoff("cli-session", "feishu")

    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = db

    class Adapter:
        async def create_handoff_thread(self, chat_id, thread_name):
            return source.thread_id

        async def send(self, chat_id, content, metadata=None):
            raise AssertionError("handoff must fail before sending")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {Platform.FEISHU: Adapter()}
    runner.session_store = store
    runner._session_db = db
    runner._evict_cached_agent = lambda session_key: None
    runner._release_running_agent_state = lambda session_key: None

    async def fail_if_dispatched(event):
        raise AssertionError("handoff must fail before dispatch")

    runner._handle_message = fail_if_dispatched
    row = db.list_pending_handoffs()[0]

    with pytest.raises(RuntimeError, match="route|scope|switch"):
        await GatewayRunner._process_handoff(runner, row)

    assert extra_route not in store._entries or store._entries[extra_route].session_id != "cli-session"
    assert top_level_route not in store._entries or store._entries[top_level_route].session_id != "cli-session"

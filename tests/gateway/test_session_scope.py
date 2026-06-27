import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig, SessionResetPolicy, load_gateway_config
from gateway.session import (
    InvalidLiveSessionSource,
    SessionSource,
    SessionStore,
    build_session_context,
    build_session_key,
)


def _feishu_source(**overrides) -> SessionSource:
    data = {
        "platform": Platform.FEISHU,
        "chat_id": "oc_group",
        "chat_type": "group",
        "user_id": "ou_user",
        "user_id_alt": "on_union",
    }
    data.update(overrides)
    return SessionSource(**data)


def test_conversation_scope_identity_feishu_group_scope_id_is_deterministic_and_account_scoped():
    from gateway.conversation_scope import conversation_identity

    source = _feishu_source()

    ident = conversation_identity(
        source,
        platform_account_id="feishu_app:abc",
        group_sessions_per_user=True,
        group_conversation_scope_per_user=False,
    )

    assert ident.id == conversation_identity(
        source,
        platform_account_id="feishu_app:abc",
        group_sessions_per_user=True,
        group_conversation_scope_per_user=False,
    ).id
    assert ident.platform_account_id == "feishu_app:abc"
    assert ident.participant_mode == "shared"
    assert ident.participant_id is None
    assert ident.id == "cs_" + hashlib.sha256(ident.canonical_key.encode("utf-8")).hexdigest()[:32]


def test_conversation_scope_identity_participant_mode_matrix():
    from gateway.conversation_scope import conversation_identity

    source = _feishu_source()
    group_shared = conversation_identity(
        source,
        platform_account_id="feishu_app:acct",
        group_sessions_per_user=True,
        group_conversation_scope_per_user=False,
    )
    group_per_user = conversation_identity(
        source,
        platform_account_id="feishu_app:acct",
        group_sessions_per_user=True,
        group_conversation_scope_per_user=True,
    )
    dm = conversation_identity(
        _feishu_source(chat_type="dm", chat_id="oc_dm", thread_id=None),
        platform_account_id="feishu_app:acct",
    )
    topic_shared = conversation_identity(
        _feishu_source(thread_id="omt_topic"),
        platform_account_id="feishu_app:acct",
        thread_sessions_per_user=True,
        thread_conversation_scope_per_user=False,
    )
    topic_per_user = conversation_identity(
        _feishu_source(thread_id="omt_topic"),
        platform_account_id="feishu_app:acct",
        thread_sessions_per_user=True,
        thread_conversation_scope_per_user=True,
    )
    other_account = conversation_identity(
        source,
        platform_account_id="feishu_app:other",
        group_sessions_per_user=True,
        group_conversation_scope_per_user=False,
    )

    assert group_shared.participant_mode == "shared"
    assert group_shared.participant_id is None
    assert group_per_user.participant_mode == "per_user"
    assert group_per_user.participant_id == "on_union"
    assert group_per_user.id != group_shared.id
    assert dm.chat_type == "dm"
    assert dm.participant_mode == "shared"
    assert topic_shared.participant_mode == "shared"
    assert topic_shared.thread_id == "omt_topic"
    assert topic_per_user.participant_mode == "per_user"
    assert topic_per_user.participant_id == "on_union"
    assert topic_per_user.id != topic_shared.id
    assert other_account.id != group_shared.id

    canonical = json.loads(group_shared.canonical_key)
    assert canonical["version"] == 1
    assert canonical["platform"] == "feishu"
    assert canonical["platform_account_id"] == "feishu_app:acct"
    assert canonical["chat_id"] == "oc_group"
    assert canonical["participant_mode"] == "shared"


def test_conversation_scope_group_per_user_requires_participant_evidence():
    from gateway.conversation_scope import conversation_identity

    ident = conversation_identity(
        _feishu_source(user_id=None, user_id_alt=None),
        platform_account_id="feishu_app:acct",
        group_conversation_scope_per_user=True,
    )

    assert ident is None


def test_conversation_scope_feishu_group_requires_chat_id_evidence():
    from gateway.conversation_scope import conversation_identity

    ident = conversation_identity(
        _feishu_source(chat_id=""),
        platform_account_id="feishu_app:acct",
    )

    assert ident is None


def test_conversation_scope_feishu_dm_requires_chat_id_evidence():
    from gateway.conversation_scope import conversation_identity

    ident = conversation_identity(
        _feishu_source(chat_type="dm", chat_id="", thread_id=None),
        platform_account_id="feishu_app:acct",
    )

    assert ident is None


def test_build_session_key_rejects_live_feishu_forum_without_chat_identity():
    source = _feishu_source(chat_type="forum", chat_id="", thread_id=None)

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        build_session_key(source, require_conversation_identity=True)

    assert build_session_key(source, group_sessions_per_user=False) == "agent:main:feishu:forum"


def test_conversation_scope_feishu_platform_account_id_hashes_app_id_and_requires_evidence():
    from gateway.conversation_scope import feishu_platform_account_id

    expected = "feishu_app:" + hashlib.sha256(b"cli_a7f5").hexdigest()[:16]

    assert feishu_platform_account_id(app_id="cli_a7f5") == expected
    assert feishu_platform_account_id(platform_account_id="feishu_app:test") == "feishu_app:test"
    assert feishu_platform_account_id(app_id="") is None
    assert feishu_platform_account_id(app_id=None) is None


def test_route_partition_key_mirrors_build_session_key_matrix():
    from gateway.conversation_scope import route_partition_key
    from gateway.session import build_session_key

    cases = [
        (_feishu_source(chat_type="dm", chat_id="oc_dm", thread_id=None), True, False),
        (_feishu_source(chat_type="group", chat_id="oc_group", thread_id=None), True, False),
        (_feishu_source(chat_type="group", chat_id="oc_group", thread_id=None), False, False),
        (_feishu_source(chat_type="group", chat_id="oc_group", thread_id="omt_topic"), True, False),
        (_feishu_source(chat_type="group", chat_id="oc_group", thread_id="omt_topic"), True, True),
    ]

    for source, group_per_user, thread_per_user in cases:
        assert route_partition_key(
            source,
            group_sessions_per_user=group_per_user,
            thread_sessions_per_user=thread_per_user,
        ) == build_session_key(
            source,
            group_sessions_per_user=group_per_user,
            thread_sessions_per_user=thread_per_user,
        )




def test_feishu_effective_session_isolation_defaults_to_chat_shared_without_changing_global_default():
    config = GatewayConfig.from_dict(
        {
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {"app_id": "cli_pr2a_default"},
                }
            }
        }
    )

    group_alice = _feishu_source(user_id_alt="on_alice")
    group_bob = _feishu_source(user_id_alt="on_bob")
    topic_alice = _feishu_source(thread_id="omt_topic", user_id_alt="on_alice")
    topic_bob = _feishu_source(thread_id="omt_topic", user_id_alt="on_bob")
    dm_alice = _feishu_source(chat_type="dm", chat_id="oc_dm_alice", thread_id=None, user_id_alt="on_alice")
    dm_bob = _feishu_source(chat_type="dm", chat_id="oc_dm_bob", thread_id=None, user_id_alt="on_bob")

    assert config.group_sessions_per_user is True
    assert config.thread_sessions_per_user is False
    assert config.effective_session_isolation(Platform.FEISHU) == {
        "group_sessions_per_user": False,
        "thread_sessions_per_user": False,
    }
    assert config.effective_session_isolation(Platform.TELEGRAM) == {
        "group_sessions_per_user": True,
        "thread_sessions_per_user": False,
    }
    assert config.session_key_for_source(group_alice) == "agent:main:feishu:group:oc_group"
    assert config.session_key_for_source(group_bob) == config.session_key_for_source(group_alice)
    assert config.route_partition_key_for_source(topic_alice) == "agent:main:feishu:group:oc_group:omt_topic"
    assert config.route_partition_key_for_source(topic_bob) == config.route_partition_key_for_source(topic_alice)
    assert config.session_key_for_source(dm_alice) == "agent:main:feishu:dm:oc_dm_alice"
    assert config.session_key_for_source(dm_bob) == "agent:main:feishu:dm:oc_dm_bob"


def test_feishu_session_isolation_to_dict_round_trip_preserves_chat_shared_default():
    config = GatewayConfig.from_dict(
        {
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {"app_id": "cli_pr2a_roundtrip"},
                }
            }
        }
    )

    restored = GatewayConfig.from_dict(config.to_dict())

    assert restored.group_sessions_per_user is True
    assert restored.thread_sessions_per_user is False
    assert restored.effective_session_isolation(Platform.FEISHU) == {
        "group_sessions_per_user": False,
        "thread_sessions_per_user": False,
    }
    assert restored.session_key_for_source(
        _feishu_source(user_id_alt="on_alice")
    ) == restored.session_key_for_source(_feishu_source(user_id_alt="on_bob"))


def test_feishu_session_isolation_yaml_top_level_explicit_config_overrides_chat_shared_default(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "group_sessions_per_user: true\n"
        "thread_sessions_per_user: true\n"
        "feishu:\n"
        "  app_id: cli_pr2a_yaml_override\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    config = load_gateway_config()

    assert config.group_sessions_per_user is True
    assert config.thread_sessions_per_user is True
    assert config.effective_session_isolation(Platform.FEISHU) == {
        "group_sessions_per_user": True,
        "thread_sessions_per_user": True,
    }
    assert config.session_key_for_source(
        _feishu_source(thread_id="omt_topic", user_id="ou_alice", user_id_alt="on_alice")
    ) != config.session_key_for_source(
        _feishu_source(thread_id="omt_topic", user_id="ou_bob", user_id_alt="on_bob")
    )


def test_feishu_session_isolation_top_level_override_requires_explicit_marker():
    config = GatewayConfig.from_dict(
        {
            "session_isolation_overrides": {
                "group_sessions_per_user": True,
                "thread_sessions_per_user": True,
            },
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {"app_id": "cli_pr2a_override"},
                }
            },
        }
    )

    assert config.effective_session_isolation(Platform.FEISHU) == {
        "group_sessions_per_user": True,
        "thread_sessions_per_user": True,
    }


def test_feishu_session_isolation_override_map_value_beats_platform_extra():
    config = GatewayConfig.from_dict(
        {
            "group_sessions_per_user": True,
            "thread_sessions_per_user": True,
            "session_isolation_overrides": {
                "group_sessions_per_user": False,
                "thread_sessions_per_user": False,
            },
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {
                        "app_id": "cli_pr2a_override_false",
                        "group_sessions_per_user": True,
                        "thread_sessions_per_user": True,
                    },
                }
            },
        }
    )

    assert config.effective_session_isolation(Platform.FEISHU) == {
        "group_sessions_per_user": False,
        "thread_sessions_per_user": False,
    }


def test_base_adapter_guard_key_uses_injected_config_instead_of_platform_extra():
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class _Adapter(BasePlatformAdapter):
        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    adapter = _Adapter(
        PlatformConfig(
            extra={
                "group_sessions_per_user": True,
                "thread_sessions_per_user": True,
            }
        ),
        Platform.FEISHU,
        session_isolation_config=GatewayConfig.from_dict(
            {
                "group_sessions_per_user": False,
                "thread_sessions_per_user": False,
            }
        ),
    )
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="group-1",
        chat_type="group",
        thread_id="topic-1",
        user_id="user-1",
    )

    assert adapter._session_guard_key(source) == "agent:main:feishu:group:group-1:topic-1"


def test_gateway_runner_injects_session_isolation_config_into_base_adapter_without_signature_support(monkeypatch):
    import gateway.run as gateway_run
    import gateway.platforms.feishu as feishu_mod

    config = GatewayConfig.from_dict(
        {
            "group_sessions_per_user": False,
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {
                        "app_id": "cli_test",
                        "app_secret": "secret",
                        "group_sessions_per_user": True,
                    },
                }
            },
        }
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = config
    monkeypatch.setattr(feishu_mod, "check_feishu_requirements", lambda: True)

    adapter = gateway_run.GatewayRunner._create_adapter(
        runner,
        Platform.FEISHU,
        config.platforms[Platform.FEISHU],
    )
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="channel-1",
        chat_type="group",
        user_id="user-1",
    )

    assert adapter is not None
    assert adapter._session_isolation_config is config
    assert adapter._session_guard_key(source) == "agent:main:feishu:group:channel-1:user-1"

@pytest.mark.asyncio
async def test_feishu_default_group_scope_is_chat_shared_across_store_guard_and_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.platforms.feishu import FeishuAdapter
    from gateway.platforms.base import MessageEvent, MessageType

    config = GatewayConfig.from_dict(
        {
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {
                        "app_id": "cli_pr2a_default_store",
                        "app_secret": "secret",
                    },
                }
            },
        }
    )
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    alice = _feishu_source(user_id_alt="on_alice", user_name="Alice")
    bob = _feishu_source(user_id_alt="on_bob", user_name="Bob")
    other_group = _feishu_source(chat_id="oc_other", user_id_alt="on_alice")
    topic_alice = _feishu_source(thread_id="omt_topic", user_id_alt="on_alice")
    topic_bob = _feishu_source(thread_id="omt_topic", user_id_alt="on_bob")
    dm_alice = _feishu_source(chat_type="dm", chat_id="oc_dm_alice", thread_id=None, user_id_alt="on_alice")
    dm_bob = _feishu_source(chat_type="dm", chat_id="oc_dm_bob", thread_id=None, user_id_alt="on_bob")

    alice_entry = store.get_or_create_session(alice)
    bob_entry = store.get_or_create_session(bob)
    other_group_entry = store.get_or_create_session(other_group)
    topic_alice_entry = store.get_or_create_session(topic_alice)
    topic_bob_entry = store.get_or_create_session(topic_bob)
    dm_alice_entry = store.get_or_create_session(dm_alice)
    dm_bob_entry = store.get_or_create_session(dm_bob)

    assert alice_entry.session_key == "agent:main:feishu:group:oc_group"
    assert bob_entry.session_key == alice_entry.session_key
    assert bob_entry.session_id == alice_entry.session_id
    assert bob_entry.route_partition_key == alice_entry.route_partition_key
    assert other_group_entry.session_key == "agent:main:feishu:group:oc_other"
    assert other_group_entry.session_id != alice_entry.session_id
    assert topic_alice_entry.session_key == "agent:main:feishu:group:oc_group:omt_topic"
    assert topic_bob_entry.session_key == topic_alice_entry.session_key
    assert topic_bob_entry.session_id == topic_alice_entry.session_id
    assert dm_alice_entry.session_key == "agent:main:feishu:dm:oc_dm_alice"
    assert dm_bob_entry.session_key == "agent:main:feishu:dm:oc_dm_bob"
    assert dm_alice_entry.session_id != dm_bob_entry.session_id

    adapter = FeishuAdapter(config.platforms[Platform.FEISHU], session_isolation_config=config)
    alice_event = MessageEvent(text="hello", source=alice, message_type=MessageType.TEXT)
    bob_event = MessageEvent(text="world", source=bob, message_type=MessageType.TEXT)
    bob_media_event = MessageEvent(
        text="photo",
        source=bob,
        message_type=MessageType.PHOTO,
        media_urls=["m1"],
    )
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(_event):
        handler_started.set()
        await release_handler.wait()
        return None

    async def noop_keep_typing(*_args, **_kwargs):
        await asyncio.Future()

    adapter.set_message_handler(handler)
    monkeypatch.setattr(adapter, "_keep_typing", noop_keep_typing)

    assert adapter._text_batch_key(alice_event) == alice_entry.route_partition_key
    assert adapter._text_batch_key(bob_event) == alice_entry.route_partition_key
    assert adapter._media_batch_key(bob_media_event) == f"{alice_entry.route_partition_key}:media:photo"

    await adapter.handle_message(alice_event)
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    assert list(adapter._active_sessions) == [alice_entry.route_partition_key]

    release_handler.set()
    await adapter.cancel_background_tasks()

def test_route_partition_turn_control_key_combines_scope_and_route_partition():
    from gateway.conversation_scope import turn_control_key

    expected = hashlib.sha256(b"cs_abcagent:main:feishu:group:oc_group:on_union").hexdigest()

    assert turn_control_key("cs_abc", "agent:main:feishu:group:oc_group:on_union") == expected
    assert turn_control_key("cs_abc", "agent:main:feishu:group:oc_group:other") != expected
    assert turn_control_key("cs_other", "agent:main:feishu:group:oc_group:on_union") != expected


def test_scope_lifecycle_get_or_create_and_reset_feishu_sessions_write_scope_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    source = _feishu_source()
    entry = store.get_or_create_session(source)
    row = store._db.get_session(entry.session_id)

    assert row["scope_assignment_status"] == "scoped"
    assert row["conversation_scope_id"].startswith("cs_")
    assert row["route_session_key_snapshot"] == entry.session_key
    assert row["route_partition_key"] == entry.session_key

    reset_entry = store.reset_session(entry.session_key)
    reset_row = store._db.get_session(reset_entry.session_id)

    assert reset_row["scope_assignment_status"] == "scoped"
    assert reset_row["conversation_scope_id"] == row["conversation_scope_id"]
    assert reset_row["route_session_key_snapshot"] == reset_entry.session_key
    assert reset_row["route_partition_key"] == reset_entry.session_key


def test_scope_lifecycle_get_or_create_upserts_conversation_scope_on_normal_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_scope_upsert"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(_feishu_source())

    scope = store._db._conn.execute(
        "SELECT platform_account_id FROM conversation_scopes WHERE id = ?",
        (entry.conversation_scope_id,),
    ).fetchone()
    assert scope is not None
    assert scope["platform_account_id"] == entry.platform_account_id


def test_scope_lifecycle_gateway_config_group_conversation_scope_per_user_affects_session_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig.from_dict(
        {
            "group_conversation_scope_per_user": True,
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {"app_id": "cli_pr1_group_scope_config"},
                }
            },
        }
    )
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    first = store.get_or_create_session(_feishu_source(user_id_alt="on_union_a"))
    second = store.get_or_create_session(_feishu_source(user_id_alt="on_union_b"))

    assert config.group_conversation_scope_per_user is True
    assert first.conversation_scope_id.startswith("cs_")
    assert second.conversation_scope_id.startswith("cs_")
    assert first.conversation_scope_id != second.conversation_scope_id


def test_scope_lifecycle_gateway_config_thread_conversation_scope_per_user_affects_session_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig.from_dict(
        {
            "thread_conversation_scope_per_user": True,
            "thread_sessions_per_user": True,
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "extra": {"app_id": "cli_pr1_thread_scope_config"},
                }
            },
        }
    )
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    first = store.get_or_create_session(_feishu_source(thread_id="omt_topic", user_id_alt="on_union_a"))
    second = store.get_or_create_session(_feishu_source(thread_id="omt_topic", user_id_alt="on_union_b"))

    assert config.thread_conversation_scope_per_user is True
    assert first.conversation_scope_id.startswith("cs_")
    assert second.conversation_scope_id.startswith("cs_")
    assert first.conversation_scope_id != second.conversation_scope_id


def test_scope_lifecycle_group_per_user_without_participant_stays_unscoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.group_conversation_scope_per_user = True
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_missing_participant"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(_feishu_source(user_id=None, user_id_alt=None))
    row = store._db.get_session(entry.session_id)

    assert entry.conversation_scope_id is None
    assert row["conversation_scope_id"] is None
    assert row["scope_assignment_status"] != "scoped"


@pytest.mark.asyncio
async def test_live_feishu_group_without_chat_id_is_rejected_before_dispatch(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from gateway.config import PlatformConfig
    from gateway.platforms.feishu import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    adapter._dispatch_inbound_event = AsyncMock()
    adapter.get_chat_info = AsyncMock()
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "ou_user", "user_name": "Alice", "user_id_alt": "on_union"}
    )
    message = SimpleNamespace(
        chat_id="",
        thread_id=None,
        parent_id=None,
        upper_message_id=None,
        root_id=None,
        message_type="text",
        content='{"text":"hello"}',
        message_id="om_missing_group_chat",
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(event=SimpleNamespace(message=message)),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id="on_union"),
        chat_type="group",
        message_id="om_missing_group_chat",
    )

    adapter._dispatch_inbound_event.assert_not_called()
    adapter.get_chat_info.assert_not_called()
    adapter._resolve_sender_profile.assert_not_called()


@pytest.mark.parametrize(
    ("source", "expected_fallback_key"),
    [
        (_feishu_source(chat_type="group", chat_id="", thread_id=None), "agent:main:feishu:group"),
        (_feishu_source(chat_type="group", chat_id="", thread_id="omt_topic"), "agent:main:feishu:group:omt_topic"),
        (_feishu_source(chat_type="dm", chat_id="", thread_id=None), "agent:main:feishu:dm"),
        (_feishu_source(chat_type="forum", chat_id="", thread_id=None), "agent:main:feishu:forum"),
    ],
)
def test_session_store_rejects_live_feishu_without_chat_identity_but_preserves_legacy_key_semantics(
    tmp_path, monkeypatch, source, expected_fallback_key
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_missing_chat"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    assert config.session_key_for_source(source) == expected_fallback_key

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        store.get_or_create_session(source)
    assert store._entries == {}


def test_gateway_runner_session_key_rejects_live_feishu_without_chat_identity():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(sessions_dir="/tmp/unused", config=runner.config)
    runner.session_store._db = None
    source = _feishu_source(chat_id="")

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        runner._session_key_for_source(source)


def test_gateway_runner_session_key_rejects_live_feishu_forum_without_chat_identity():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(sessions_dir="/tmp/unused", config=runner.config)
    runner.session_store._db = None
    source = _feishu_source(chat_type="forum", chat_id="", thread_id=None)

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        runner._session_key_for_source(source)


def test_gateway_runner_session_key_fallback_rejects_live_feishu_without_chat_identity():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    source = _feishu_source(chat_id="")

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        runner._session_key_for_source(source)


def test_gateway_runner_session_key_fallback_rejects_live_feishu_forum_without_chat_identity():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    source = _feishu_source(chat_type="forum", chat_id="", thread_id=None)

    with pytest.raises(InvalidLiveSessionSource, match="feishu_missing_chat_identity"):
        runner._session_key_for_source(source)


def test_gateway_runner_session_key_accepts_valid_feishu_and_non_feishu_fallbacks():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert runner._session_key_for_source(_feishu_source()) == "agent:main:feishu:group:oc_group"
    assert (
        runner._session_key_for_source(_feishu_source(chat_type="forum", chat_id="oc_forum", thread_id=None))
        == "agent:main:feishu:forum:oc_forum"
    )

    telegram_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="",
        chat_type="dm",
        user_id="tg_user",
    )
    assert runner._session_key_for_source(telegram_source) == "agent:main:telegram:dm"


def test_scope_backfill_feishu_session_without_platform_account_evidence_stays_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(_feishu_source())
    row = store._db.get_session(entry.session_id)

    assert row["scope_assignment_status"] == "ambiguous"
    assert row["conversation_scope_id"] is None
    assert row["route_partition_key"] is None


def test_scope_backfill_existing_legacy_feishu_entry_detaches_instead_of_promoting_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_backfill"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "legacy-json-session"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "display_name": "Legacy group",
                    "platform": "feishu",
                    "chat_type": "group",
                }
            }
        ),
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(session_id=session_id, source="feishu", user_id="ou_user")

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))
    scope_tables = {
        r["id"]
        for r in store._db._conn.execute("SELECT id FROM conversation_scopes").fetchall()
    }

    assert entry.session_id != session_id
    assert entry.platform_account_id.startswith("feishu_app:")
    assert entry.conversation_scope_id.startswith("cs_")
    assert entry.route_partition_key == session_key
    assert row["scope_assignment_status"] in {None, "legacy_unscoped"}
    assert row["conversation_scope_id"] is None
    assert row["route_session_key_snapshot"] is None
    assert row["route_partition_key"] is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_session_key_snapshot"] == session_key
    assert new_row["route_partition_key"] == session_key
    assert entry.conversation_scope_id in scope_tables
    assert stored[session_key]["session_id"] == entry.session_id
    assert stored[session_key]["conversation_scope_id"] == entry.conversation_scope_id
    assert stored[session_key]["platform_account_id"] == entry.platform_account_id
    assert stored[session_key]["route_partition_key"] == session_key


def test_scope_backfill_json_only_legacy_feishu_entry_detaches_instead_of_creating_scoped_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_backfill"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "json-only-session"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                }
            }
        ),
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert entry.session_id != session_id
    assert entry.conversation_scope_id.startswith("cs_")
    assert row is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key
    assert stored[session_key]["session_id"] == entry.session_id


def test_scope_backfill_partial_scoped_db_row_detaches_instead_of_completing_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.conversation_scope import conversation_identity, feishu_platform_account_id
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_partial_scoped_db"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    platform_account_id = feishu_platform_account_id(app_id="cli_pr1_partial_scoped_db")
    expected_scope = conversation_identity(source, platform_account_id=platform_account_id)
    session_id = "partial-scoped-db"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "conversation_scope_id": expected_scope.id,
                    "platform_account_id": platform_account_id,
                    "route_partition_key": session_key,
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(session_id=session_id, source="feishu")
    store._db._conn.execute(
        """
        UPDATE sessions
        SET scope_assignment_status='scoped',
            conversation_scope_id=NULL,
            route_session_key_snapshot=NULL,
            route_partition_key=NULL
        WHERE id=?
        """,
        (session_id,),
    )
    store._db._conn.commit()

    entry = store.get_or_create_session(source)
    old_row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)

    assert entry.session_id != session_id
    assert old_row["scope_assignment_status"] == "scoped"
    assert old_row["conversation_scope_id"] is None
    assert old_row["route_session_key_snapshot"] is None
    assert old_row["route_partition_key"] is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key


def test_scope_backfill_existing_feishu_entry_without_account_evidence_stays_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "legacy-no-account"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)

    assert entry.conversation_scope_id is None
    assert entry.platform_account_id is None
    assert entry.route_partition_key is None
    assert row is None or row["scope_assignment_status"] in {None, "legacy_unscoped"}


def test_scope_backfill_existing_feishu_entry_with_incompatible_scope_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_backfill"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "legacy-conflict"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "conversation_scope_id": "cs_conflicting",
                    "platform_account_id": "feishu_app:old",
                    "route_partition_key": "old-route",
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(
        session_id=session_id,
        source="feishu",
        conversation_scope_id="cs_conflicting",
        scope_assignment_status="scoped",
        route_session_key_snapshot="old-route",
        route_partition_key="old-route",
    )

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert entry.session_id != session_id
    assert entry.conversation_scope_id != "cs_conflicting"
    assert entry.platform_account_id != "feishu_app:old"
    assert entry.route_partition_key == session_key
    assert row["conversation_scope_id"] == "cs_conflicting"
    assert row["route_partition_key"] == "old-route"
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key
    assert stored[session_key]["session_id"] == entry.session_id
    assert stored[session_key]["route_partition_key"] == session_key


def test_scope_backfill_existing_scoped_entry_repairs_missing_db_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.conversation_scope import conversation_identity, feishu_platform_account_id
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_backfill"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    platform_account_id = feishu_platform_account_id(app_id="cli_pr1_backfill")
    expected_scope = conversation_identity(source, platform_account_id=platform_account_id)
    session_id = "scoped-json-missing-db"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "conversation_scope_id": expected_scope.id,
                    "platform_account_id": platform_account_id,
                    "route_partition_key": session_key,
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)

    assert entry.conversation_scope_id == expected_scope.id
    assert row["scope_assignment_status"] == "scoped"
    assert row["conversation_scope_id"] == expected_scope.id
    assert row["route_partition_key"] == session_key


def test_scope_backfill_resume_pending_legacy_entry_detaches_instead_of_repairing_missing_db_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_resume_backfill"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "resume-pending-json"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "resume_pending": True,
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)

    assert entry.session_id != session_id
    assert entry.resume_pending is False
    assert entry.conversation_scope_id.startswith("cs_")
    assert row is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))
    assert stored[session_key]["session_id"] == entry.session_id
    assert stored[session_key]["conversation_scope_id"] == entry.conversation_scope_id
    assert stored[session_key]["platform_account_id"] == entry.platform_account_id
    assert stored[session_key]["route_partition_key"] == session_key


def test_scope_backfill_resume_pending_entry_with_conflicting_db_route_does_not_claim_success(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_resume_conflict"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    session_id = "resume-pending-conflict"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "resume_pending": True,
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(session_id=session_id, source="feishu")
    store._db._conn.execute(
        "UPDATE sessions SET scope_assignment_status='legacy_unscoped', route_partition_key=? WHERE id=?",
        ("different-route", session_id),
    )
    store._db._conn.commit()

    entry = store.get_or_create_session(source)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert row["scope_assignment_status"] == "legacy_unscoped"
    assert row["conversation_scope_id"] is None
    assert row["route_partition_key"] == "different-route"
    assert entry.session_id != session_id
    assert entry.conversation_scope_id is not None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key
    assert stored[session_key]["session_id"] == entry.session_id
    assert stored[session_key]["conversation_scope_id"] == entry.conversation_scope_id


def test_scope_backfill_same_legacy_session_id_from_multiple_routes_is_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_ambiguous"},
    )
    source_a = _feishu_source(chat_id="oc_group_a")
    source_b = _feishu_source(chat_id="oc_group_b")
    session_key_a = config.session_key_for_source(source_a)
    session_key_b = config.session_key_for_source(source_b)
    session_id = "shared-legacy-session"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key_a: {
                    "session_key": session_key_a,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source_a.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                },
                session_key_b: {
                    "session_key": session_key_b,
                    "session_id": session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source_b.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                },
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(session_id=session_id, source="feishu")

    entry = store.get_or_create_session(source_a)
    row = store._db.get_session(session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert entry.session_id != session_id
    assert entry.conversation_scope_id is not None
    assert entry.route_partition_key == session_key_a
    assert row["scope_assignment_status"] == "ambiguous"
    assert row["conversation_scope_id"] is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key_a
    assert stored[session_key_a]["session_id"] == entry.session_id
    assert stored[session_key_a]["route_partition_key"] == session_key_a
    assert "conversation_scope_id" not in stored[session_key_b] or stored[session_key_b]["conversation_scope_id"] is None


def test_scope_backfill_ambiguous_db_row_detaches_current_legacy_json_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_ambiguous_db"},
    )
    source = _feishu_source(chat_id="oc_group_ambiguous")
    session_key = config.session_key_for_source(source)
    legacy_session_id = "ambiguous-db-session"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": legacy_session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(legacy_session_id, "feishu")
    store._db.mark_session_scope_ambiguous(legacy_session_id, source="feishu", user_id="ou_user")

    entry = store.get_or_create_session(source)
    old_row = store._db.get_session(legacy_session_id)
    new_row = store._db.get_session(entry.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert entry.session_id != legacy_session_id
    assert entry.conversation_scope_id is not None
    assert entry.route_partition_key == session_key
    assert old_row["scope_assignment_status"] == "ambiguous"
    assert old_row["conversation_scope_id"] is None
    assert old_row["route_partition_key"] is None
    assert new_row["scope_assignment_status"] == "scoped"
    assert new_row["conversation_scope_id"] == entry.conversation_scope_id
    assert new_row["route_partition_key"] == session_key
    assert stored[session_key]["session_id"] == entry.session_id
    assert stored[session_key]["route_partition_key"] == session_key


def test_scope_backfill_switch_session_preserves_scoped_target_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_switch_scope"},
    )
    source = _feishu_source()
    session_key = config.session_key_for_source(source)
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    current = store.get_or_create_session(source)
    target_session_id = "scoped-target"
    scope_id = current.conversation_scope_id
    route_key = current.route_partition_key
    store._db.create_session(
        session_id=target_session_id,
        source="feishu",
        conversation_scope_id=scope_id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=route_key,
        route_partition_key=route_key,
    )

    switched = store.switch_session(session_key, target_session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert switched.session_id == target_session_id
    assert switched.conversation_scope_id == scope_id
    assert switched.platform_account_id == current.platform_account_id
    assert switched.route_partition_key == route_key
    assert stored[session_key]["conversation_scope_id"] == scope_id
    assert stored[session_key]["platform_account_id"] == current.platform_account_id
    assert stored[session_key]["route_partition_key"] == route_key


def test_scope_backfill_switch_session_rejects_cross_scope_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_switch_cross_scope"},
    )
    source = _feishu_source(chat_id="oc_group_a")
    session_key = config.session_key_for_source(source)
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    current = store.get_or_create_session(source)
    target_session_id = "different-scope-target"
    store._db.create_session(
        session_id=target_session_id,
        source="feishu",
        conversation_scope_id="cs_other",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-other",
        route_partition_key="route-other",
    )

    switched = store.switch_session(session_key, target_session_id)

    assert switched is None
    assert store._entries[session_key].session_id == current.session_id


def test_scope_backfill_switch_session_rejects_unscoped_current_to_scoped_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from datetime import datetime
    from hermes_state import SessionDB
    from gateway.session import SessionEntry, build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    source = _feishu_source(chat_id="oc_group_a")
    session_key = build_session_key(source, group_sessions_per_user=True)
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    current = SessionEntry(
        session_key=session_key,
        session_id="legacy-current",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        conversation_scope_id=None,
        platform_account_id=None,
        route_partition_key=None,
    )
    store._entries[session_key] = current
    store._loaded = True
    store._save()
    store._db.create_session("legacy-current", "feishu")
    store._db.create_session(
        session_id="scoped-target",
        source="feishu",
        conversation_scope_id="cs_target",
        scope_assignment_status="scoped",
        route_session_key_snapshot="route-target",
        route_partition_key="route-target",
    )

    switched = store.switch_session(session_key, "scoped-target")

    assert switched is None
    assert store._entries[session_key].session_id == "legacy-current"


def test_scope_backfill_ambiguous_json_db_conflict_detaches_current_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB
    from gateway.session import build_session_key

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_ambiguous_detach"},
    )
    source_a = _feishu_source(chat_id="oc_group_a")
    source_b = _feishu_source(chat_id="oc_group_b")
    session_key_a = config.session_key_for_source(source_a)
    session_key_b = config.session_key_for_source(source_b)
    shared_session_id = "shared-scoped-session"
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key_a: {
                    "session_key": session_key_a,
                    "session_id": shared_session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source_a.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                    "conversation_scope_id": "cs_a",
                    "platform_account_id": "feishu_app:a",
                    "route_partition_key": session_key_a,
                },
                session_key_b: {
                    "session_key": session_key_b,
                    "session_id": shared_session_id,
                    "created_at": "2026-05-30T01:02:03",
                    "updated_at": "2026-05-30T01:02:03",
                    "origin": source_b.to_dict(),
                    "platform": "feishu",
                    "chat_type": "group",
                },
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._db.create_session(
        shared_session_id,
        "feishu",
        conversation_scope_id="cs_a",
        scope_assignment_status="scoped",
        route_session_key_snapshot=session_key_a,
        route_partition_key=session_key_a,
    )

    entry_b = store.get_or_create_session(source_b)
    row_b = store._db.get_session(entry_b.session_id)
    stored = json.loads((config.sessions_dir / "sessions.json").read_text(encoding="utf-8"))

    assert entry_b.session_id != shared_session_id
    assert entry_b.conversation_scope_id
    assert entry_b.route_partition_key == session_key_b
    assert row_b["conversation_scope_id"] == entry_b.conversation_scope_id
    assert row_b["route_partition_key"] == session_key_b
    assert stored[session_key_b]["session_id"] == entry_b.session_id
    assert stored[session_key_b]["session_id"] != shared_session_id


def test_scope_kwargs_for_feishu_helper_error_does_not_downgrade_to_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="none")
    config.sessions_dir = tmp_path / "sessions"
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True,
        extra={"app_id": "cli_pr1_helper_error"},
    )
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._db = SessionDB(db_path=tmp_path / "state.db")

    def _boom(*args, **kwargs):
        raise RuntimeError("canonicalization failed")

    monkeypatch.setattr("gateway.conversation_scope.route_partition_key", _boom)

    with pytest.raises(RuntimeError, match="canonicalization failed"):
        store.get_or_create_session(_feishu_source())

    assert not store._db.list_sessions_rich(source="feishu")


def test_session_context_scope_vars_are_task_local_and_clear_to_empty():
    from gateway.session_context import clear_session_vars, get_session_env, set_session_vars

    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_group",
        session_key="agent:main:feishu:group:oc_group:on_union",
        conversation_scope_id="cs_123",
        platform_account_id="feishu_app:abc",
        route_partition_key="agent:main:feishu:group:oc_group:on_union",
    )

    assert get_session_env("HERMES_CONVERSATION_SCOPE_ID") == "cs_123"
    assert get_session_env("HERMES_PLATFORM_ACCOUNT_ID") == "feishu_app:abc"
    assert get_session_env("HERMES_ROUTE_PARTITION_KEY") == "agent:main:feishu:group:oc_group:on_union"

    clear_session_vars(tokens)

    assert get_session_env("HERMES_CONVERSATION_SCOPE_ID") == ""
    assert get_session_env("HERMES_PLATFORM_ACCOUNT_ID") == ""
    assert get_session_env("HERMES_ROUTE_PARTITION_KEY") == ""


def test_session_context_scope_gateway_runner_set_session_env_sets_scope_metadata():
    from gateway.run import GatewayRunner
    from gateway.session_context import clear_session_vars, get_session_env

    config = GatewayConfig()
    source = _feishu_source()
    entry = type(
        "Entry",
        (),
        {
            "session_key": "agent:main:feishu:group:oc_group:on_union",
            "session_id": "sid",
            "created_at": None,
            "updated_at": None,
        },
    )()
    context = build_session_context(config=config, source=source, session_entry=entry)
    context.conversation_scope_id = "cs_ctx"
    context.platform_account_id = "feishu_app:ctx"
    context.route_partition_key = "agent:main:feishu:group:oc_group:on_union"

    runner = GatewayRunner.__new__(GatewayRunner)
    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_CONVERSATION_SCOPE_ID") == "cs_ctx"
        assert get_session_env("HERMES_PLATFORM_ACCOUNT_ID") == "feishu_app:ctx"
        assert get_session_env("HERMES_ROUTE_PARTITION_KEY") == "agent:main:feishu:group:oc_group:on_union"
    finally:
        clear_session_vars(tokens)


def test_cached_agent_scope_state_refreshes_on_reuse(tmp_path):
    from gateway.session_context import set_session_vars
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = False
    agent.session_id = "cached-agent-session"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "system"
    agent._parent_session_id = None

    set_session_vars(
        conversation_scope_id="cs_cached",
        platform_account_id="feishu_app:cached",
        route_partition_key="route_cached",
    )

    agent._ensure_db_session()
    row = db.get_session("cached-agent-session")

    assert row["scope_assignment_status"] == "scoped"
    assert row["conversation_scope_id"] == "cs_cached"
    assert row["route_partition_key"] == "route_cached"

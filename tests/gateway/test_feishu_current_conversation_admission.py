from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.conversation_scope import (
    conversation_identity,
    feishu_platform_account_id,
    route_partition_key,
)
from gateway.feishu_contracts import (
    ConversationContract,
    build_feishu_conversation_contract,
    feishu_hashed_ref,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.feishu import FeishuAdapter
from gateway.session import SessionSource, build_session_key


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(
            extra={
                "app_id": "cli_test_app",
                "tenant_partition_key": "tenant:test",
                "app_partition_key": "app:test",
                "gateway_event_state_dir": str(tmp_path),
            }
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._gateway_event_state_dir = None
    return adapter


def _source(
    *,
    chat_id: str = "oc_fake_dm",
    chat_type: str = "dm",
    user_id: str = "ou_actor_fake_001",
    user_id_alt: str = "on_actor_fake_001",
    thread_id: str | None = None,
    message_id: str = "om_fake_001",
) -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id=chat_id,
        chat_name="Feishu Current",
        chat_type=chat_type,
        user_id=user_id,
        user_id_alt=user_id_alt,
        thread_id=thread_id,
        message_id=message_id,
    )


def _contract(source: SessionSource, adapter: FeishuAdapter) -> ConversationContract:
    isolation = adapter._effective_session_isolation()
    identity = conversation_identity(
        source,
        platform_account_id=feishu_platform_account_id(app_id="cli_test_app"),
        **isolation,
    )
    assert identity is not None
    actor = feishu_hashed_ref("feishu_actor", source.user_id_alt or source.user_id)
    authority = feishu_hashed_ref("feishu_authority_subject", source.user_id_alt or source.user_id)
    assert actor is not None and authority is not None
    return build_feishu_conversation_contract(
        scope_identity=identity,
        route_partition_key=route_partition_key(source, **isolation),
        route_session_key_snapshot=build_session_key(
            source,
            **isolation,
            require_conversation_identity=True,
        ),
        scope_assignment_status="scoped",
        actor_ref=actor,
        authority_subject_ref=authority,
        identity_evidence_set=(actor, authority),
        session_id="session:fake-current",
        tenant_partition_key="tenant:test",
        app_partition_key="app:test",
        thread_anchor_ref=feishu_hashed_ref("feishu_thread", source.thread_id),
        root_anchor_ref=feishu_hashed_ref("feishu_reply_anchor", source.message_id),
    )


def _event(
    source: SessionSource,
    *,
    message_id: str | None = "om_fake_001",
    mentions_bot: bool = False,
    reply_to_message_id: str | None = "om_parent_fake_001",
    raw_event_id: str = "evt_fake_001",
    detached_thread: bool = False,
) -> MessageEvent:
    mentions = []
    if mentions_bot:
        mentions = [SimpleNamespace(id=SimpleNamespace(open_id="ou_bot_fake"), key="@_user_1")]
    raw = SimpleNamespace(
        header=SimpleNamespace(event_id=raw_event_id),
        event=SimpleNamespace(
            message=SimpleNamespace(
                chat_id=source.chat_id,
                chat_type="p2p" if source.chat_type == "dm" else "group",
                message_id=message_id,
                mentions=mentions,
                thread_id=None if detached_thread else source.thread_id,
                parent_id=reply_to_message_id,
                upper_message_id=None,
                root_id=reply_to_message_id,
            )
        ),
    )
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
        raw_message=raw,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
        timestamp=datetime.fromtimestamp(1_700_000_000),
    )


def _assert_bound_record(record: dict, contract: ConversationContract, transport_kind: str) -> None:
    assert record["contract_hash"] == contract.contract_hash
    assert record["route_partition_key"] == contract.route_partition_key
    assert record["route_session_key_snapshot"] == contract.route_session_key_snapshot
    assert record["actor_ref"] == contract.actor_ref.value_hash
    assert record["authority_subject_ref"] == contract.authority_subject_ref.value_hash
    assert record["transport_kind"] == transport_kind
    assert record["reply_anchor_ref"].startswith("sha256:")
    assert all("oc_fake" not in str(value) for value in record.values())
    assert all("ou_actor" not in str(value) for value in record.values())
    assert all("om_fake" not in str(value) for value in record.values())


def test_dm_message_admission_creates_current_conversation_record(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_dm", chat_type="dm")
    contract = _contract(source, adapter)

    result = adapter._admit_current_conversation_event(
        _event(source),
        transport_kind="dm",
        expected_contract=contract,
    )

    assert result.ok is True
    _assert_bound_record(result.record, contract, "dm")


@pytest.mark.asyncio
async def test_private_dm_continuity_and_current_reply_binding_failures(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_dm", chat_type="dm")
    contract = _contract(source, adapter)
    result = adapter._admit_current_conversation_event(
        _event(source),
        transport_kind="dm",
        expected_contract=contract,
    )

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(
                    reply=Mock(return_value=SimpleNamespace(success=lambda: True)),
                    create=Mock(return_value=SimpleNamespace(success=lambda: True)),
                )
            )
        )
    )
    adapter._build_reply_message_body = lambda **_kwargs: object()
    adapter._build_reply_message_request = lambda *_args: object()
    await adapter._send_raw_message(
        chat_id=source.chat_id,
        msg_type="text",
        payload='{"text":"ok"}',
        reply_to="om_parent_fake_001",
        metadata={"feishu_current_admission": result.record},
    )
    assert adapter._client.im.v1.message.reply.call_count == 1

    mismatched_actor = _source(chat_id="oc_fake_dm", chat_type="dm", user_id_alt="on_other_fake")
    denied = adapter._admit_current_conversation_event(
        _event(mismatched_actor),
        transport_kind="dm",
        expected_contract=contract,
    )
    assert denied.ok is False
    assert denied.failure_class == "feishu_current_actor_mismatch"

    stale_contract = _contract(source, adapter)
    object.__setattr__(stale_contract, "evidence_state", "stale")
    denied = adapter._admit_current_conversation_event(
        _event(source),
        transport_kind="dm",
        expected_contract=stale_contract,
    )
    assert denied.failure_class == "feishu_current_route_evidence_stale"

    denied = adapter._admit_current_conversation_event(
        _event(source, reply_to_message_id=None),
        transport_kind="dm",
        expected_contract=contract,
    )
    assert denied.failure_class == "feishu_current_reply_anchor_missing"


def test_group_mention_admission_requires_route_actor_mention_anchor_snapshot_and_contract(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_group", chat_type="group")
    contract = _contract(source, adapter)

    result = adapter._admit_current_conversation_event(
        _event(source, mentions_bot=True),
        transport_kind="group",
        expected_contract=contract,
        mention_required=True,
    )

    assert result.ok is True
    _assert_bound_record(result.record, contract, "group")

    mismatched_route = _source(chat_id="oc_other_group", chat_type="group")
    denied = adapter._admit_current_conversation_event(
        _event(mismatched_route, mentions_bot=True),
        transport_kind="group",
        expected_contract=contract,
        mention_required=True,
    )
    assert denied.failure_class == "feishu_current_route_mismatch"

    denied = adapter._admit_current_conversation_event(
        _event(source, mentions_bot=False),
        transport_kind="group",
        expected_contract=contract,
        mention_required=True,
    )
    assert denied.failure_class == "feishu_current_route_evidence_missing"


def test_thread_reply_preserves_thread_reply_to_anchor_and_fails_when_detached_or_ambiguous(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_group", chat_type="group", thread_id="omt_thread_fake")
    contract = _contract(source, adapter)

    result = adapter._admit_current_conversation_event(
        _event(source, mentions_bot=True, reply_to_message_id="om_thread_parent_fake"),
        transport_kind="thread",
        expected_contract=contract,
        mention_required=True,
    )

    assert result.ok is True
    assert result.record["thread_anchor_ref"] == contract.thread_anchor_ref.value_hash
    assert result.record["reply_anchor_ref"].startswith("sha256:")

    detached = adapter._admit_current_conversation_event(
        _event(source, mentions_bot=True, detached_thread=True),
        transport_kind="thread",
        expected_contract=contract,
        mention_required=True,
    )
    assert detached.failure_class == "feishu_current_thread_detached"

    ambiguous = adapter._admit_current_conversation_event(
        _event(source, mentions_bot=True, reply_to_message_id=None),
        transport_kind="thread",
        expected_contract=contract,
        mention_required=True,
    )
    assert ambiguous.failure_class == "feishu_current_reply_anchor_missing"


def test_webhook_and_websocket_normalize_to_equivalent_route_binding(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_dm", chat_type="dm")
    contract = _contract(source, adapter)

    webhook = adapter._admit_current_conversation_event(
        _event(source, raw_event_id="evt_same_fake"),
        transport_kind="webhook",
        expected_contract=contract,
    )
    websocket = adapter._admit_current_conversation_event(
        _event(source, raw_event_id="evt_same_fake"),
        transport_kind="websocket",
        expected_contract=contract,
    )

    assert webhook.ok is True
    assert websocket.ok is True
    comparable_keys = set(webhook.record) - {"transport_kind"}
    assert {key: webhook.record[key] for key in comparable_keys} == {
        key: websocket.record[key] for key in comparable_keys
    }
    assert webhook.record["transport_kind"] == "webhook"
    assert websocket.record["transport_kind"] == "websocket"


@pytest.mark.asyncio
async def test_negative_cases_deny_before_dispatch_and_keep_sanitized_denial_evidence(tmp_path):
    adapter = _adapter(tmp_path)
    source = _source(chat_id="oc_fake_group", chat_type="group")
    contract = _contract(source, adapter)
    event = _event(source, mentions_bot=False)

    await adapter._handle_message_with_guards(
        event,
        current_conversation_contract=contract,
        transport_kind="group",
        mention_required=True,
    )

    adapter.handle_message.assert_not_awaited()
    denial = getattr(event, "feishu_current_conversation_denial")
    assert denial["failure_class"] == "feishu_current_route_evidence_missing"
    assert "oc_fake_group" not in str(denial)
    assert "ou_actor_fake" not in str(denial)
    assert "om_fake" not in str(denial)

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.gateway_event_ledger import LEDGER_FILENAME, apply_gateway_event
from gateway.platforms.base import MessageType
from gateway.platforms.feishu import FeishuAdapter


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
    adapter._is_duplicate = lambda _message_id: False
    adapter._extract_message_content = AsyncMock(
        return_value=("/status", MessageType.TEXT, [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value=None)
    adapter.get_chat_info = AsyncMock(
        return_value={
            "chat_id": "oc_fake_dm",
            "name": "Feishu Current",
            "type": "dm",
            "raw_type": "p2p",
            "reliable": True,
        }
    )
    adapter._resolve_sender_profile = AsyncMock(
        return_value={
            "user_id": "ou_actor_fake_001",
            "user_id_alt": "on_actor_fake_001",
            "user_name": "Current User",
        }
    )
    return adapter


def _raw_message_data(
    *,
    event_id: str = "evt_fake_001",
    message_id: str = "om_fake_001",
    chat_id: str = "oc_fake_dm",
    text: str = "/status",
) -> SimpleNamespace:
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(
                    open_id="ou_actor_fake_001",
                    union_id="on_actor_fake_001",
                ),
                sender_type="user",
            ),
            message=SimpleNamespace(
                chat_id=chat_id,
                chat_type="p2p",
                message_id=message_id,
                content=json.dumps({"text": text}),
                mentions=[],
                parent_id=None,
                upper_message_id=None,
                root_id=None,
                thread_id=None,
            ),
        ),
    )


def _inbound_event(
    *,
    canonical_event_id: str = "evt_fake_001",
    message_id: str = "om_fake_001",
    route_partition_key: str = "feishu:route:fake",
    contract_hash: str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    transport_kind: str = "websocket",
    timestamp: int = 1_700_000_000,
) -> dict:
    return {
        "type": "feishu_inbound",
        "inbound_id": message_id,
        "message_id": message_id,
        "message_type": "text",
        "timestamp": timestamp,
        "canonical_event_ref": f"sha256:{canonical_event_id.encode().hex():0<64}"[:71],
        "route_partition_key": route_partition_key,
        "contract_hash": contract_hash,
        "transport_kind": transport_kind,
    }


def _inbound_event_from_admission(event, *, transport_kind: str) -> dict:
    admission = getattr(event, "feishu_current_conversation_admission")
    return {
        "type": "feishu_inbound",
        "inbound_id": event.message_id,
        "message_id": event.message_id,
        "message_type": event.message_type.value,
        "timestamp": int(event.timestamp.timestamp())
        if isinstance(event.timestamp, datetime)
        else 1_700_000_000,
        "canonical_event_ref": admission["canonical_event_ref"],
        "route_partition_key": admission["route_partition_key"],
        "contract_hash": admission["contract_hash"],
        "transport_kind": transport_kind,
    }


def _ledger_state(tmp_path) -> dict:
    ledger_path = tmp_path / LEDGER_FILENAME
    if not ledger_path.exists():
        return {}
    return json.loads(ledger_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_processing_same_inbound_event_id_twice_dispatches_once(tmp_path):
    adapter = _adapter(tmp_path)
    data = _raw_message_data(event_id="evt_fake_001", message_id="om_fake_001")

    await adapter._handle_message_event_data(data, transport_kind="websocket")
    await adapter._handle_message_event_data(data, transport_kind="websocket")

    assert adapter.handle_message.await_count == 1
    state = _ledger_state(tmp_path)
    assert len(state["inbounds"]) == 1


@pytest.mark.asyncio
async def test_webhook_duplicate_after_websocket_is_classified_duplicate(tmp_path):
    adapter = _adapter(tmp_path)
    data = _raw_message_data(event_id="evt_same_fake", message_id="om_same_fake")

    await adapter._handle_message_event_data(data, transport_kind="websocket")
    await adapter._handle_message_event_data(data, transport_kind="webhook")

    assert adapter.handle_message.await_count == 1
    admitted_event = adapter.handle_message.await_args.args[0]
    duplicate = apply_gateway_event(
        _inbound_event_from_admission(admitted_event, transport_kind="webhook"),
        tmp_path,
    )
    assert duplicate.ok is True
    assert duplicate.action is not None
    assert duplicate.action["decision"] == "feishu_inbound_duplicate"
    assert duplicate.action["duplicate"] is True


@pytest.mark.asyncio
async def test_websocket_duplicate_after_webhook_is_classified_duplicate(tmp_path):
    adapter = _adapter(tmp_path)
    data = _raw_message_data(event_id="evt_same_fake", message_id="om_same_fake")

    await adapter._handle_message_event_data(data, transport_kind="webhook")
    await adapter._handle_message_event_data(data, transport_kind="websocket")

    assert adapter.handle_message.await_count == 1
    admitted_event = adapter.handle_message.await_args.args[0]
    duplicate = apply_gateway_event(
        _inbound_event_from_admission(admitted_event, transport_kind="websocket"),
        tmp_path,
    )
    assert duplicate.ok is True
    assert duplicate.action is not None
    assert duplicate.action["decision"] == "feishu_inbound_duplicate"
    assert duplicate.action["duplicate"] is True


def test_transport_fanout_uses_same_primary_key_without_transport_split(tmp_path):
    first = apply_gateway_event(
        _inbound_event(
            canonical_event_id="evt_fanout_fake",
            message_id="om_fanout_a",
            transport_kind="websocket",
        ),
        tmp_path,
    )
    second = apply_gateway_event(
        _inbound_event(
            canonical_event_id="evt_fanout_fake",
            message_id="om_fanout_b",
            transport_kind="webhook",
        ),
        tmp_path,
    )

    assert first.ok is True
    assert second.ok is True
    assert first.action is not None and second.action is not None
    assert first.action["duplicate"] is False
    assert second.action["decision"] == "feishu_inbound_duplicate"
    assert second.action["duplicate"] is True
    state = _ledger_state(tmp_path)
    assert len(state["inbounds"]) == 1
    assert "webhook" not in next(iter(state["inbounds"]))
    assert "websocket" not in next(iter(state["inbounds"]))


@pytest.mark.asyncio
async def test_restart_replay_from_populated_ledger_does_not_dispatch_again(tmp_path):
    first = _adapter(tmp_path)
    data = _raw_message_data(event_id="evt_replay_fake", message_id="om_replay_fake")
    await first._handle_message_event_data(data, transport_kind="websocket")
    assert first.handle_message.await_count == 1

    restarted = _adapter(tmp_path)
    await restarted._handle_message_event_data(data, transport_kind="webhook")

    assert restarted.handle_message.await_count == 0
    assert len(_ledger_state(tmp_path)["inbounds"]) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_admission_has_one_winner_and_one_duplicate(tmp_path):
    adapter = _adapter(tmp_path)
    data = _raw_message_data(event_id="evt_race_fake", message_id="om_race_fake")

    await asyncio.gather(
        adapter._handle_message_event_data(data, transport_kind="websocket"),
        adapter._handle_message_event_data(data, transport_kind="webhook"),
    )

    assert adapter.handle_message.await_count == 1
    state = _ledger_state(tmp_path)
    assert len(state["inbounds"]) == 1


@pytest.mark.asyncio
async def test_legacy_non_brokered_callback_replay_fails_closed_without_dispatch(tmp_path):
    adapter = _adapter(tmp_path)
    callback = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_callback_replay_fake"),
        event=SimpleNamespace(
            action=SimpleNamespace(value={"legacy": "callback"}),
            operator=SimpleNamespace(open_id="ou_actor_fake_001"),
        ),
    )
    before = _ledger_state(tmp_path)

    await adapter._handle_message_event_data(callback, transport_kind="webhook")

    assert adapter.handle_message.await_count == 0
    assert _ledger_state(tmp_path) == before


def test_idempotency_unknown_fails_closed_without_persistent_success_state(tmp_path):
    event = _inbound_event(canonical_event_id="", message_id="om_unknown_fake")
    event["canonical_event_ref"] = ""
    before = _ledger_state(tmp_path)

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "feishu_inbound_idempotency_unknown"
    assert _ledger_state(tmp_path) == before

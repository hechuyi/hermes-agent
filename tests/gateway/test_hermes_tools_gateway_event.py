import importlib

import pytest

from gateway.gateway_event_contract import GatewayEventResult
from gateway.hermes_tools_gateway_event import (
    HermesToolsGatewayEventResult,
    _validated_feishu_request,
    apply_gateway_event,
    apply_gateway_event_async,
    preflight_gateway_event,
)


def test_facade_exports_compat_result_alias_and_entrypoints():
    module = importlib.import_module("gateway.hermes_tools_gateway_event")

    assert module.GatewayEventResult is GatewayEventResult
    assert HermesToolsGatewayEventResult is GatewayEventResult
    assert module.HermesToolsGatewayEventResult is GatewayEventResult
    assert module.apply_gateway_event is apply_gateway_event
    assert module.apply_gateway_event_async is apply_gateway_event_async
    assert module.preflight_gateway_event is preflight_gateway_event


def test_facade_does_not_expose_external_binary_adapter():
    module = importlib.import_module("gateway.hermes_tools_gateway_event")

    assert not hasattr(module, "subprocess")
    assert not hasattr(module, "DEFAULT_HERMES_TOOLS_BINARY")


def test_apply_gateway_event_ignores_binary_and_uses_internal_ledger(tmp_path):
    event = {
        "type": "feishu_inbound",
        "inbound_id": "raw-inbound-id",
        "message_id": "raw-message-id",
        "message_type": "text",
        "timestamp": 1_700_000_000,
    }

    result = apply_gateway_event(event, tmp_path, binary="definitely-missing")

    assert result.ok is True
    assert result.event_type == "feishu_inbound"
    assert result.failure_class is None
    assert result.reason is None
    assert result.diagnostics == ""
    assert result.action is not None
    assert result.action["type"] == "inbound_admission"
    assert result.action["decision"] == "continue"
    assert result.action["duplicate"] is False
    record = result.action["record"]
    assert record["inbound_id_hash"].startswith("fnv1a64:")
    assert record["message_id_hash"].startswith("fnv1a64:")
    assert record["message_type"] == "text"
    assert record["first_seen_at"] == 1_700_000_000


@pytest.mark.asyncio
async def test_apply_gateway_event_async_uses_same_internal_facade(tmp_path):
    result = await apply_gateway_event_async(
        {
            "type": "feishu_inbound",
            "inbound_id": "inbound-async",
            "message_id": "message-async",
            "message_type": "text",
            "timestamp": 1_700_000_001,
        },
        tmp_path,
        binary="definitely-missing",
    )

    assert result.ok is True
    assert result.event_type == "feishu_inbound"
    assert result.action is not None
    assert result.action["type"] == "inbound_admission"


@pytest.mark.parametrize("event_type", ["task_status", "status_card"])
def test_task_status_and_status_card_events_fail_closed(tmp_path, event_type):
    result = apply_gateway_event(
        {
            "type": event_type,
            "task_id": "task-1",
            "state": "running",
            "timestamp": 1_700_000_002,
        },
        tmp_path,
        binary="definitely-missing",
    )

    assert result.ok is False
    assert result.event_type == event_type
    assert result.action is None
    assert result.failure_class == "unsupported_gateway_event_type"
    assert result.reason == "unsupported gateway event type"


def test_preflight_gateway_event_uses_internal_ledger_without_status_card(tmp_path):
    result = preflight_gateway_event(tmp_path, binary="definitely-missing")

    assert result.ok is True
    assert result.event_type == "preflight"
    assert result.action is not None
    assert result.action["type"] == "preflight"
    names = {check["name"] for check in result.action["checks"]}
    assert names == {
        "state_dir_writable",
        "feishu_inbound",
        "delivery_lifecycle",
        "feishu_ack",
        "stale_pending_scan",
        "session_guard",
    }
    assert "status_card_request_descriptor" not in names


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "operation": "send_interactive_message",
            "method": "POST",
            "path": "/open-apis/im/v1/messages",
            "params": {"receive_id_type": "chat_id"},
            "body": {
                "receive_id": "oc_chat",
                "msg_type": "interactive",
                "content": '{"config":{"wide_screen_mode":true}}',
                "uuid": "descriptor-uuid_1",
            },
        },
        {
            "operation": "patch_interactive_message",
            "method": "PATCH",
            "path": "/open-apis/im/v1/messages/om_card_123",
            "params": {},
            "body": {"content": '{"config":{"wide_screen_mode":true}}'},
        },
    ],
)
def test_descriptor_validator_through_facade_accepts_send_and_patch(descriptor):
    assert _validated_feishu_request(descriptor) == descriptor


def test_descriptor_validator_through_facade_rejects_generic_path():
    descriptor = {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/chats",
        "params": {"receive_id_type": "chat_id"},
        "body": {
            "receive_id": "oc_chat",
            "msg_type": "interactive",
            "content": '{"config":{"wide_screen_mode":true}}',
        },
    }

    assert _validated_feishu_request(descriptor) is None

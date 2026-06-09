from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import PlatformConfig
from gateway.feishu_contracts import feishu_hashed_ref
from gateway.gateway_event_ledger import LEDGER_FILENAME, apply_gateway_event
from gateway.platforms.feishu import FeishuAdapter


def _sha(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakeResponse:
    def __init__(
        self,
        *,
        ok: bool = True,
        message_id: str | None = "om_bot_current_1",
        code: int = 0,
        msg: str = "ok",
    ) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = SimpleNamespace(message_id=message_id) if message_id is not None else None

    def success(self) -> bool:
        return self._ok


def _adapter(tmp_path, *, create_response=None, reply_response=None, update_response=None):
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
    message = SimpleNamespace(
        create=Mock(return_value=create_response or _FakeResponse()),
        reply=Mock(return_value=reply_response or _FakeResponse()),
        update=Mock(return_value=update_response or _FakeResponse(message_id=None)),
    )
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message))
    )
    adapter._build_reply_message_body = lambda **_kwargs: object()
    adapter._build_reply_message_request = lambda *_args: object()
    adapter._build_create_message_body = lambda **_kwargs: object()
    adapter._build_create_message_request = lambda *_args: object()
    adapter._build_update_message_body = lambda **_kwargs: object()
    adapter._build_update_message_request = lambda **_kwargs: object()
    return adapter


def _admission(
    *,
    route: str = "route:current:alpha",
    contract_hash: str | None = None,
    route_snapshot: str = "session-route-current-alpha",
    reply_anchor: str = "om_user_anchor_1",
    evidence_state: str = "current",
) -> dict[str, str]:
    reply_ref = feishu_hashed_ref("feishu_reply_anchor", reply_anchor)
    assert reply_ref is not None
    return {
        "canonical_event_ref": _sha("event-current-alpha"),
        "contract_hash": contract_hash or _sha("contract-current-alpha"),
        "route_partition_key": route,
        "route_session_key_snapshot": route_snapshot,
        "actor_ref": _sha("actor-current-alpha"),
        "authority_subject_ref": _sha("authority-current-alpha"),
        "transport_kind": "dm",
        "reply_anchor_ref": reply_ref.value_hash,
        "evidence_state": evidence_state,
    }


def _metadata(
    *,
    delivery_id: str = "delivery-current-send",
    admission: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "delivery_id": delivery_id,
        "inbound_id": "inbound-current-1",
        "session_id": "session-current-1",
        "correlation_id": "corr-current-1",
        "feishu_current_admission": admission or _admission(),
    }


def _state(tmp_path) -> dict:
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        return json.load(handle)


def _lifecycle_events(tmp_path, event_type: str | None = None) -> list[dict]:
    events = _state(tmp_path).get("feishu_delivery_lifecycle", [])
    if event_type is None:
        return events
    return [event for event in events if event["type"] == event_type]


def _event_types(tmp_path) -> list[str]:
    return [event["type"] for event in _lifecycle_events(tmp_path)]


def _assert_no_raw_platform_context(value) -> None:
    rendered = json.dumps(value, sort_keys=True)
    for raw in (
        "oc_current_chat",
        "om_bot_current",
        "om_user_anchor",
        "ou_actor_raw",
        "on_actor_raw",
        "raw response",
        "hello current",
    ):
        assert raw not in rendered


@pytest.mark.asyncio
async def test_current_reply_send_records_attempt_sent_and_ack_unknown_with_sanitized_refs(tmp_path):
    adapter = _adapter(tmp_path)

    result = await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(),
    )

    assert result.success is True
    assert result.message_id == "om_bot_current_1"
    assert _event_types(tmp_path) == [
        "feishu_delivery_attempted",
        "feishu_delivery_sent",
        "feishu_delivery_ack_unknown",
    ]
    attempted, sent, ack_unknown = _lifecycle_events(tmp_path)
    assert attempted["action"] == "send"
    assert sent["action"] == "send"
    assert ack_unknown["action"] == "send"
    assert attempted["route_partition_hash"].startswith("sha256:")
    assert attempted["contract_hash"] == _metadata()["feishu_current_admission"]["contract_hash"]
    assert sent["message_ref_hash"].startswith("sha256:")
    assert ack_unknown["message_ref_hash"] == sent["message_ref_hash"]
    assert ack_unknown["failure_class"] == "feishu_delivery_ack_unobservable"
    assert "feishu_delivery_acked" not in _event_types(tmp_path)
    _assert_no_raw_platform_context(_lifecycle_events(tmp_path))


@pytest.mark.asyncio
async def test_sdk_failure_records_feishu_delivery_failed_without_success_event(tmp_path):
    adapter = _adapter(
        tmp_path,
        reply_response=_FakeResponse(ok=False, message_id=None, code=190001, msg="denied"),
    )

    result = await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(delivery_id="delivery-sdk-failed"),
    )

    assert result.success is False
    assert _event_types(tmp_path) == [
        "feishu_delivery_attempted",
        "feishu_delivery_failed",
    ]
    failed = _lifecycle_events(tmp_path, "feishu_delivery_failed")[0]
    assert failed["failure_class"] == "feishu_terminal_non_acceptance"
    assert _lifecycle_events(tmp_path, "feishu_delivery_sent") == []
    _assert_no_raw_platform_context(_lifecycle_events(tmp_path))


@pytest.mark.asyncio
async def test_unknown_ack_support_records_ack_unknown_not_ack_success(tmp_path):
    adapter = _adapter(tmp_path)

    result = await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(delivery_id="delivery-ack-unknown"),
    )

    assert result.success is True
    assert "feishu_delivery_ack_unknown" in _event_types(tmp_path)
    assert "feishu_delivery_acked" not in _event_types(tmp_path)
    ack_unknown = _lifecycle_events(tmp_path, "feishu_delivery_ack_unknown")[0]
    assert ack_unknown["failure_class"] == "feishu_delivery_ack_unobservable"
    assert ack_unknown["message_ref_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_sdk_success_without_usable_message_id_records_unknown_state_without_ownership(tmp_path):
    adapter = _adapter(tmp_path, reply_response=_FakeResponse(ok=True, message_id=None))

    result = await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(delivery_id="delivery-missing-message"),
    )

    assert result.success is False
    assert result.error == "Feishu SDK success missing message_id"
    assert _event_types(tmp_path) == ["feishu_delivery_attempted"]
    assert _lifecycle_events(tmp_path, "feishu_delivery_sent") == []
    assert _lifecycle_events(tmp_path, "feishu_delivery_ack_unknown") == []
    delivery_record = next(iter(_state(tmp_path)["deliveries"].values()))
    assert delivery_record["status"] == "unknown"
    assert delivery_record["failure_class"] == "missing_message_id_after_sdk_success"
    _assert_no_raw_platform_context(_lifecycle_events(tmp_path))


@pytest.mark.asyncio
async def test_limited_edit_requires_bot_owned_current_delivery_and_matching_route(tmp_path):
    adapter = _adapter(tmp_path)
    metadata = _metadata(delivery_id="delivery-edit-source")
    send_result = await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=metadata,
    )
    assert send_result.success is True

    edit_result = await adapter.edit_message(
        "oc_current_chat",
        "om_bot_current_1",
        "edited current",
        metadata=_metadata(delivery_id="delivery-edit-1"),
    )

    assert edit_result.success is True
    assert adapter._client.im.v1.message.update.call_count == 1
    sent_events = _lifecycle_events(tmp_path, "feishu_delivery_sent")
    assert [event["action"] for event in sent_events] == ["send", "edit"]
    edit_event = sent_events[-1]
    assert edit_event["original_delivery_hash"] == sent_events[0]["delivery_hash"]
    assert edit_event["bot_ownership_hash"] == sent_events[0]["bot_ownership_hash"]
    _assert_no_raw_platform_context(_lifecycle_events(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "metadata", "expected_failure"),
    [
        (
            "om_user_message_1",
            _metadata(delivery_id="edit-user-message"),
            "feishu_delivery_bot_ownership_missing",
        ),
        (
            "om_unknown_message_1",
            _metadata(delivery_id="edit-unknown-message"),
            "feishu_delivery_bot_ownership_missing",
        ),
        (
            "om_bot_current_1",
            _metadata(
                delivery_id="edit-stale-message",
                admission=_admission(evidence_state="stale"),
            ),
            "feishu_current_route_evidence_stale",
        ),
        (
            "om_bot_current_1",
            _metadata(
                delivery_id="edit-cross-route",
                admission=_admission(route="route:current:beta"),
            ),
            "feishu_current_route_mismatch",
        ),
        (
            "om_bot_current_1",
            {"delivery_id": "edit-non-current"},
            "feishu_current_reply_admission_missing",
        ),
    ],
)
async def test_edit_denials_happen_before_sdk_and_append_only_sanitized_failure_evidence(
    tmp_path, message_id, metadata, expected_failure
):
    adapter = _adapter(tmp_path)
    await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(delivery_id="delivery-edit-denial-source"),
    )
    before_deliveries = dict(_state(tmp_path)["deliveries"])
    adapter._client.im.v1.message.update.reset_mock()

    result = await adapter.edit_message(
        "oc_current_chat",
        message_id,
        "edited current",
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == expected_failure
    adapter._client.im.v1.message.update.assert_not_called()
    after = _state(tmp_path)
    assert after["deliveries"] == before_deliveries
    failed = _lifecycle_events(tmp_path, "feishu_delivery_failed")[-1]
    assert failed["action"] == "edit"
    assert failed["failure_class"] == expected_failure
    _assert_no_raw_platform_context(failed)


@pytest.mark.asyncio
async def test_failed_edit_does_not_rewrite_original_delivery_as_success(tmp_path):
    adapter = _adapter(
        tmp_path,
        update_response=_FakeResponse(ok=False, message_id=None, code=190002, msg="denied"),
    )
    await adapter.send(
        "oc_current_chat",
        "hello current",
        reply_to="om_user_anchor_1",
        metadata=_metadata(delivery_id="delivery-original-success"),
    )
    original_sent = _lifecycle_events(tmp_path, "feishu_delivery_sent")[0]

    result = await adapter.edit_message(
        "oc_current_chat",
        "om_bot_current_1",
        "edited current",
        metadata=_metadata(delivery_id="delivery-edit-fails"),
    )

    assert result.success is False
    sent_events = _lifecycle_events(tmp_path, "feishu_delivery_sent")
    assert sent_events == [original_sent]
    failed = _lifecycle_events(tmp_path, "feishu_delivery_failed")[-1]
    assert failed["action"] == "edit"
    assert failed["original_delivery_hash"] == original_sent["delivery_hash"]
    assert failed["failure_class"] == "feishu_terminal_non_acceptance"
    _assert_no_raw_platform_context(_lifecycle_events(tmp_path))


@pytest.mark.asyncio
async def test_unknown_delivery_state_with_message_ref_does_not_create_edit_ownership(tmp_path):
    assert apply_gateway_event(
        {
            "type": "delivery_pending",
            "delivery_id": "delivery-unknown",
            "operation": "reply",
            "inbound_id": "inbound-current-1",
            "target": "feishu:chat:current-chat-ref",
            "session_id": "session-current-1",
            "correlation_id": "corr-current-1",
            "timestamp": 1,
        },
        tmp_path,
    ).ok
    assert apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-unknown",
            "failure_class": "provider_state_unknown",
            "message_id": "om_bot_current_1",
            "timestamp": 2,
        },
        tmp_path,
    ).ok
    adapter = _adapter(tmp_path)

    edit_result = await adapter.edit_message(
        "oc_current_chat",
        "om_bot_current_1",
        "edited current",
        metadata=_metadata(delivery_id="edit-unknown-state"),
    )
    assert edit_result.success is False
    assert edit_result.error == "feishu_delivery_bot_ownership_missing"
    adapter._client.im.v1.message.update.assert_not_called()

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.feishu_contracts import feishu_hashed_ref
from gateway.feishu_legacy_guard import feishu_broker_context
from gateway.gateway_event_ledger import apply_gateway_event
import gateway.platforms.feishu as feishu_module
from gateway.platforms.feishu import FeishuAdapter


_ROUTE_HASH = "sha256:" + "3" * 64
_CONTRACT_HASH = "sha256:" + "4" * 64
_PLAN_HASH = "sha256:" + "6" * 64
_ROOT_PROOF_HASH = "sha256:" + "7" * 64
_TOOL_ACTION_HASH = "sha256:" + "8" * 64
_GRANT_HANDLE = "broker_grant_handle:sha256:" + "9" * 64
_CURRENT_REPLY_ANCHOR = "om_user_anchor_1"


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakeResponse:
    def __init__(self, *, ok=True, message_id="om_sent", code=0, msg="ok"):
        self.code = code
        self.msg = msg
        self.data = SimpleNamespace(message_id=message_id) if ok else None
        self._ok = ok

    def success(self):
        return self._ok


class _FakeMessageApi:
    def __init__(self):
        self.create_calls = []
        self.reply_calls = []
        self.update_calls = []
        self.create_response = _FakeResponse(message_id="om_created")
        self.reply_response = _FakeResponse(message_id="om_reply")
        self.update_response = _FakeResponse(message_id="om_updated")
        self.create_exception = None
        self.reply_exception = None
        self.update_exception = None

    def create(self, request):
        self.create_calls.append(request)
        if self.create_exception is not None:
            raise self.create_exception
        return self.create_response

    def reply(self, request):
        self.reply_calls.append(request)
        if self.reply_exception is not None:
            raise self.reply_exception
        return self.reply_response

    def update(self, request):
        self.update_calls.append(request)
        if self.update_exception is not None:
            raise self.update_exception
        return self.update_response


class _FakeImageApi:
    def __init__(self):
        self.create_calls = []
        self.create_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(image_key="img_uploaded"),
        )

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response


class _FakeFileApi:
    def __init__(self):
        self.create_calls = []
        self.create_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(file_key="file_uploaded"),
        )

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
    message_api = _FakeMessageApi()
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api))
    )
    return adapter, message_api


def _non_audited_adapter():
    adapter = FeishuAdapter(PlatformConfig(extra={}))
    message_api = _FakeMessageApi()
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api))
    )
    return adapter, message_api


def _metadata(delivery_id="delivery-create"):
    return {
        "delivery_id": delivery_id,
        "inbound_id": "inbound-1",
        "session_id": "session-a",
        "correlation_id": "corr-a",
    }


def _current_admission(
    *,
    route="route:descriptor:current",
    route_snapshot="session-route-descriptor-current",
    reply_anchor=_CURRENT_REPLY_ANCHOR,
):
    reply_ref = feishu_hashed_ref("feishu_reply_anchor", reply_anchor)
    assert reply_ref is not None
    return {
        "canonical_event_ref": _sha("descriptor-current-event"),
        "contract_hash": _sha("descriptor-current-contract"),
        "route_partition_key": route,
        "route_session_key_snapshot": route_snapshot,
        "actor_ref": _sha("descriptor-current-actor"),
        "authority_subject_ref": _sha("descriptor-current-authority"),
        "transport_kind": "dm",
        "reply_anchor_ref": reply_ref.value_hash,
        "evidence_state": "current",
    }


def _metadata_with_current_admission(delivery_id="delivery-create"):
    metadata = _metadata(delivery_id)
    metadata["feishu_current_admission"] = _current_admission()
    return metadata


async def _seed_current_bot_owned_message(
    adapter,
    message_api,
    *,
    chat_id="oc_chat",
    message_id="om_existing",
    delivery_id="delivery-edit-source",
):
    original_reply_response = message_api.reply_response
    message_api.reply_response = _FakeResponse(message_id=message_id)
    result = await adapter.send(
        chat_id,
        "current bot-owned edit target",
        reply_to=_CURRENT_REPLY_ANCHOR,
        metadata=_metadata_with_current_admission(delivery_id),
    )
    assert result.success is True
    assert result.message_id == message_id
    message_api.reply_response = original_reply_response
    message_api.create_calls.clear()
    message_api.reply_calls.clear()
    message_api.update_calls.clear()


def _metadata_with_attachment_provenance(
    tmp_path,
    payload: bytes,
    *,
    delivery_id="delivery-create",
    mime_class="image",
):
    content_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    event = {
        "type": "feishu_attachment_provenance_recorded",
        "provenance_kind": "generated",
        "safe_output_root_proof_hash": _ROOT_PROOF_HASH,
        "producing_tool_action_hash": _TOOL_ACTION_HASH,
        "content_hash": content_hash,
        "declared_mime_class": mime_class,
        "size_class": "small",
        "route_partition_hash": _ROUTE_HASH,
        "route_snapshot_hash": _ROUTE_HASH,
        "contract_hash": _CONTRACT_HASH,
        "delivery_plan_hash": _PLAN_HASH,
        "sensitivity_classification": "internal",
        "sensitivity_state": "current",
        "redaction_state": "redacted",
        "retention_policy": "ephemeral",
        "retention_state": "current",
        "generator_state": "complete",
        "source_grant_handles": [_GRANT_HANDLE],
        "timestamp": 1_718_000_000,
    }
    provenance = apply_gateway_event(event, tmp_path).action["record"]
    metadata = _metadata(delivery_id)
    metadata.update(
        {
            "feishu_attachment_provenance_hash": provenance["provenance_hash"],
            "feishu_attachment_declared_mime_class": mime_class,
            "feishu_attachment_size_class": "small",
            "feishu_attachment_content_hash": content_hash,
            "feishu_attachment_safe_output_root_proof_hash": _ROOT_PROOF_HASH,
            "feishu_attachment_producing_tool_action_hash": _TOOL_ACTION_HASH,
            "feishu_attachment_source_grant_handles": (_GRANT_HANDLE,),
            "feishu_attachment_route_partition_hash": _ROUTE_HASH,
            "feishu_attachment_route_snapshot_hash": _ROUTE_HASH,
            "feishu_attachment_contract_hash": _CONTRACT_HASH,
        }
    )
    return metadata


def _install_event_recorder(adapter, *, fail_event_types=()):
    calls = []
    fail_event_types = set(fail_event_types)

    async def apply(event):
        calls.append(event)
        return event.get("type") not in fail_event_types

    adapter._apply_gateway_event = apply
    return calls


def _broker_context():
    return feishu_broker_context(
        "broker_grant_handle:sha256:" + ("b" * 64),
        action_id="broker_action:sha256:" + ("a" * 64),
        contract_hash="sha256:" + ("c" * 64),
        route_partition_key="route_snapshot:sha256:" + ("d" * 64),
    )


def _event_types(calls):
    return [call.get("type") for call in calls if "type" in call]


def _delivery_audit_event_types(calls):
    return [
        call.get("type")
        for call in calls
        if call.get("type")
        in {
            "delivery_pending",
            "delivery_sent",
            "delivery_failed",
            "unknown_delivery_state",
        }
    ]


def _assert_legacy_descriptor_denied(
    event,
    *,
    surface,
    failure_class="feishu_legacy_descriptor_requires_broker",
):
    assert event["type"] == "feishu_legacy_descriptor_denied"
    assert event["surface"] == surface
    assert event["failure_class"] == failure_class
    assert event["descriptor_hash"].startswith("fnv1a64:")
    assert event["correlation_id"]
    assert isinstance(event["timestamp"], (int, float))


def test_legacy_descriptor_denied_correlation_hashes_regex_safe_external_id():
    adapter = FeishuAdapter(PlatformConfig(enabled=True))
    external_id = "ev_regex_safe_callback_123"

    first = adapter._build_feishu_legacy_descriptor_denied_event(
        surface="feishu.card_action",
        correlation_id=external_id,
        descriptor_seed=external_id,
    )
    second = adapter._build_feishu_legacy_descriptor_denied_event(
        surface="feishu.card_action",
        correlation_id=external_id,
        descriptor_seed=external_id,
    )

    assert first["correlation_id"] != external_id
    assert first["correlation_id"] == second["correlation_id"]
    assert first["correlation_id"].startswith("feishu-legacy-denial:")
    assert first["descriptor_hash"].startswith("fnv1a64:")


def _delivery_events(calls):
    return [call for call in calls if call.get("type") in {"delivery_pending", "delivery_sent"}]


def _assert_pending_event_payload(event, *, operation, delivery_id, target, inbound_id, session_id, correlation_id):
    assert set(event) == {
        "type",
        "delivery_id",
        "operation",
        "inbound_id",
        "target",
        "session_id",
        "correlation_id",
        "timestamp",
    }
    assert event["type"] == "delivery_pending"
    assert event["operation"] == operation
    assert event["delivery_id"] == delivery_id
    assert event["target"] == target
    assert event["inbound_id"] == inbound_id
    assert event["session_id"] == session_id
    assert event["correlation_id"] == correlation_id
    assert isinstance(event["timestamp"], int)


def _assert_sent_event_payload(event, *, operation, delivery_id, message_id):
    assert set(event) == {"type", "delivery_id", "operation", "message_id", "timestamp"}
    assert event["type"] == "delivery_sent"
    assert event["operation"] == operation
    assert event["delivery_id"] == delivery_id
    assert event["message_id"] == message_id
    assert isinstance(event["timestamp"], int)


def _assert_attachment_pending_sanitized(
    adapter,
    event,
    *,
    chat_id,
    reply_to=None,
    metadata,
    delivery_id,
    operation,
    declared_mime_class,
):
    refs = adapter._attachment_upload_reservation_refs(
        chat_id=chat_id,
        reply_to=reply_to,
        metadata=metadata,
        delivery_id=delivery_id,
        declared_mime_class=declared_mime_class,
    )
    _assert_pending_event_payload(
        event,
        operation=operation,
        delivery_id=delivery_id,
        target=refs["target"],
        inbound_id=refs["inbound_id"],
        session_id=refs["session_id"],
        correlation_id=refs["correlation_id"],
    )
    event_json = json.dumps(event, sort_keys=True)
    assert "feishu:chat:" not in event_json
    assert chat_id not in event_json


def _assert_attachment_upload_denied_sanitized(
    event,
    *,
    declared_mime_class,
    forbidden_values=(),
):
    assert event["type"] == "feishu_attachment_upload_denied"
    assert event["failure_class"] == "feishu_arbitrary_local_upload_denied"
    assert event["declared_mime_class"] == declared_mime_class
    event_json = json.dumps(event, sort_keys=True)
    assert "feishu:chat:" not in event_json
    for forbidden in forbidden_values:
        assert forbidden not in event_json


def _assert_sent_matrix_events(
    events,
    *,
    operation,
    delivery_id,
    target,
    inbound_id,
    session_id,
    correlation_id,
    message_id,
):
    pending, sent = _delivery_events(events)
    _assert_pending_event_payload(
        pending,
        operation=operation,
        delivery_id=delivery_id,
        target=target,
        inbound_id=inbound_id,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    _assert_sent_event_payload(
        sent,
        operation=operation,
        delivery_id=delivery_id,
        message_id=message_id,
    )


def _assert_create_request_contract(
    adapter,
    request,
    *,
    delivery_id,
    receive_id,
    receive_id_type="chat_id",
    msg_type,
    content=None,
    idempotency_source_id=None,
):
    assert request.receive_id_type == receive_id_type
    assert request.request_body.receive_id == receive_id
    assert request.request_body.msg_type == msg_type
    expected_idempotency_source_id = (
        delivery_id if idempotency_source_id is None else idempotency_source_id
    )
    assert request.request_body.uuid == adapter._idempotency_key_for_delivery(
        expected_idempotency_source_id
    )
    if content is not None:
        assert request.request_body.content == content


def _assert_reply_request_contract(adapter, request, *, delivery_id, parent_message_id, msg_type):
    assert request.message_id == parent_message_id
    assert request.request_body.msg_type == msg_type
    assert request.request_body.reply_in_thread is False
    assert request.request_body.uuid == adapter._idempotency_key_for_delivery(delivery_id)


def _assert_update_request_contract(request, *, message_id, msg_type, content=None):
    assert request.message_id == message_id
    assert request.request_body.msg_type == msg_type
    if content is not None:
        assert request.request_body.content == content


def _delivery_record_action(
    *,
    delivery_id="delivery-reply",
    inbound_id="inbound-1",
    target="feishu:chat:oc_chat",
    session_id="session-a",
    correlation_id="corr-a",
    status="sent",
    feishu_message_id="om_existing_msg",
):
    return {
        "type": "delivery_record",
        "record": {
            "delivery_id": delivery_id,
            "inbound_id": inbound_id,
            "target": target,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "status": status,
            "created_at": 1,
            "updated_at": 2,
            "feishu_message_id": feishu_message_id,
            "failure_class": "unknown_delivery_state" if status == "unknown" else None,
            "ack_event_id": None,
        },
    }


@pytest.mark.asyncio
async def test_audited_image_file_records_pending_and_sent_after_upload(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    image_api = _FakeImageApi()
    ordered = []

    async def apply(event):
        ordered.append(("event", event))
        return True

    def upload(request):
        ordered.append(("image_upload", request))
        return image_api.create_response

    def create(request):
        ordered.append(("sdk_create", request))
        return _FakeResponse(message_id="om_image_msg")

    adapter._client.im.v1.image = image_api
    adapter._apply_gateway_event = apply
    image_api.create = upload
    message_api.create = create
    image_path = tmp_path / "audit.png"
    image_payload = b"\x89PNG\r\n\x1a\n"
    image_path.write_bytes(image_payload)

    metadata = _metadata_with_attachment_provenance(
        tmp_path,
        image_payload,
        delivery_id="delivery-image",
        mime_class="image",
    )
    result = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert [kind for kind, _ in ordered] == ["event"]
    assert image_api.create_calls == []
    assert message_api.create_calls == []
    assert message_api.reply_calls == []
    _assert_attachment_upload_denied_sanitized(
        ordered[0][1],
        declared_mime_class="image",
        forbidden_values=("oc_chat", str(image_path), image_path.name),
    )


@pytest.mark.asyncio
async def test_audited_uploaded_file_records_pending_and_sent_after_upload(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    file_api = _FakeFileApi()
    ordered = []

    async def apply(event):
        ordered.append(("event", event))
        return True

    def upload(request):
        ordered.append(("file_upload", request))
        return file_api.create_response

    def create(request):
        ordered.append(("sdk_create", request))
        return _FakeResponse(message_id="om_file_msg")

    adapter._client.im.v1.file = file_api
    adapter._apply_gateway_event = apply
    file_api.create = upload
    message_api.create = create
    file_path = tmp_path / "audit.pdf"
    file_payload = b"%PDF-1.4 test"
    file_path.write_bytes(file_payload)

    metadata = _metadata_with_attachment_provenance(
        tmp_path,
        file_payload,
        delivery_id="delivery-file",
        mime_class="file",
    )
    result = await adapter.send_document(
        chat_id="oc_chat",
        file_path=str(file_path),
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert [kind for kind, _ in ordered] == ["event"]
    assert file_api.create_calls == []
    assert message_api.create_calls == []
    assert message_api.reply_calls == []
    _assert_attachment_upload_denied_sanitized(
        ordered[0][1],
        declared_mime_class="file",
        forbidden_values=("oc_chat", str(file_path), file_path.name),
    )


@pytest.mark.asyncio
async def test_audited_image_file_reply_records_reply_operation_after_upload(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    image_api = _FakeImageApi()
    events = _install_event_recorder(adapter)
    adapter._client.im.v1.image = image_api
    image_path = tmp_path / "reply.png"
    image_payload = b"\x89PNG\r\n\x1a\n"
    image_path.write_bytes(image_payload)

    metadata = _metadata_with_attachment_provenance(
        tmp_path,
        image_payload,
        delivery_id="delivery-image-reply",
        mime_class="image",
    )
    result = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        reply_to="om_parent",
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert len(image_api.create_calls) == 0
    assert message_api.create_calls == []
    assert len(message_api.reply_calls) == 0
    assert _event_types(events) == ["feishu_attachment_upload_denied"]
    _assert_attachment_upload_denied_sanitized(
        events[0],
        declared_mime_class="image",
        forbidden_values=("oc_chat", "om_parent", str(image_path), image_path.name),
    )


@pytest.mark.asyncio
async def test_audited_uploaded_file_reply_records_reply_operation_after_upload(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    file_api = _FakeFileApi()
    events = _install_event_recorder(adapter)
    adapter._client.im.v1.file = file_api
    file_path = tmp_path / "reply.pdf"
    file_payload = b"%PDF-1.4 test"
    file_path.write_bytes(file_payload)

    metadata = _metadata_with_attachment_provenance(
        tmp_path,
        file_payload,
        delivery_id="delivery-file-reply",
        mime_class="file",
    )
    result = await adapter.send_document(
        chat_id="oc_chat",
        file_path=str(file_path),
        reply_to="om_parent",
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert len(file_api.create_calls) == 0
    assert message_api.create_calls == []
    assert len(message_api.reply_calls) == 0
    assert _event_types(events) == ["feishu_attachment_upload_denied"]
    _assert_attachment_upload_denied_sanitized(
        events[0],
        declared_mime_class="file",
        forbidden_values=("oc_chat", "om_parent", str(file_path), file_path.name),
    )


@pytest.mark.asyncio
async def test_audited_image_pending_apply_failure_aborts_message_send_after_upload(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    image_api = _FakeImageApi()
    events = _install_event_recorder(adapter, fail_event_types={"delivery_pending"})
    adapter._client.im.v1.image = image_api
    image_path = tmp_path / "audit.png"
    image_payload = b"\x89PNG\r\n\x1a\n"
    image_path.write_bytes(image_payload)

    result = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        metadata=_metadata_with_attachment_provenance(
            tmp_path,
            image_payload,
            delivery_id="delivery-image-pending-fail",
            mime_class="image",
        ),
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert len(image_api.create_calls) == 0
    assert message_api.create_calls == []
    assert message_api.reply_calls == []
    assert _event_types(events) == ["feishu_attachment_upload_denied"]
    _assert_attachment_upload_denied_sanitized(
        events[0],
        declared_mime_class="image",
        forbidden_values=("oc_chat", str(image_path), image_path.name),
    )


@pytest.mark.asyncio
async def test_audited_uploaded_file_sent_apply_failure_records_unknown_with_message_id(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    file_api = _FakeFileApi()
    events = _install_event_recorder(adapter, fail_event_types={"delivery_sent"})
    adapter._client.im.v1.file = file_api
    message_api.create_response = _FakeResponse(message_id="om_file_msg")
    file_path = tmp_path / "audit.pdf"
    file_payload = b"%PDF-1.4 test"
    file_path.write_bytes(file_payload)

    metadata = _metadata_with_attachment_provenance(
        tmp_path,
        file_payload,
        delivery_id="delivery-file-sent-fail",
        mime_class="file",
    )
    result = await adapter.send_document(
        chat_id="oc_chat",
        file_path=str(file_path),
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    assert _event_types(events) == ["feishu_attachment_upload_denied"]
    _assert_attachment_upload_denied_sanitized(
        events[0],
        declared_mime_class="file",
        forbidden_values=("oc_chat", str(file_path), file_path.name),
    )


@pytest.mark.asyncio
async def test_create_persists_pending_before_sdk_and_sent_after_message_id(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(("event", event))
        return True

    adapter._apply_gateway_event = apply

    def create(request):
        events.append(("sdk_create", request))
        return _FakeResponse(message_id="om_created")

    message_api.create = create

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is True
    assert result.message_id == "om_created"
    assert events[0][0] == "event"
    assert events[0][1]["type"] == "delivery_pending"
    assert events[0][1]["operation"] == "normal_final_reply"
    assert events[0][1]["delivery_id"] == "delivery-create"
    assert events[1][0] == "sdk_create"
    assert events[2][0] == "event"
    assert events[2][1]["type"] == "delivery_sent"
    assert events[2][1]["operation"] == "normal_final_reply"
    assert events[2][1]["delivery_id"] == "delivery-create"
    assert events[2][1]["message_id"] == "om_created"


@pytest.mark.asyncio
async def test_reply_send_records_reply_operation(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is True
    assert result.message_id == "om_reply"
    assert message_api.create_calls == []
    assert len(message_api.reply_calls) == 1
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[0]["operation"] == "reply"
    assert events[1]["operation"] == "reply"
    assert events[1]["delivery_id"] == "delivery-reply"
    assert events[1]["message_id"] == "om_reply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case",
        "chat_id",
        "reply_to",
        "metadata",
        "expected_operation",
        "expected_message_id",
        "expected_sdk_method",
        "expected_target",
    ),
    [
        (
            "normal final",
            "oc_chat",
            None,
            _metadata("turn-42-final-1"),
            "normal_final_reply",
            "om_created",
            "create",
            "feishu:chat:oc_chat",
        ),
        (
            "direct reply",
            "oc_chat",
            "om_parent",
            _metadata("om_parent-reply-1"),
            "reply",
            "om_reply",
            "reply",
            "feishu:chat:oc_chat",
        ),
        (
            "queued follow-up first reply",
            "oc_chat",
            None,
            {
                **_metadata("queue-item-7:first-reply"),
                "inbound_id": "queue-item-7",
                "delivery_operation_hint": "queued_followup_first_reply",
            },
            "queued_followup_first_reply",
            "om_created",
            "create",
            "feishu:chat:oc_chat",
        ),
        (
            "stream fresh-final",
            "oc_chat",
            None,
            {
                **_metadata("stream-session-9:fresh-final"),
                "inbound_id": "stream-session-9",
                "delivery_operation_hint": "stream_fresh_final",
            },
            "stream_fresh_final",
            "om_created",
            "create",
            "feishu:chat:oc_chat",
        ),
    ],
)
async def test_send_operation_matrix_covers_delivery_and_sdk_contract(
    tmp_path,
    case,
    chat_id,
    reply_to,
    metadata,
    expected_operation,
    expected_message_id,
    expected_sdk_method,
    expected_target,
):
    adapter, message_api = _adapter(tmp_path)
    timeline = []

    async def apply(event):
        timeline.append(("event", event))
        return True

    def create(request):
        message_api.create_calls.append(request)
        timeline.append(("sdk_create", request))
        return _FakeResponse(message_id="om_created")

    def reply(request):
        message_api.reply_calls.append(request)
        timeline.append(("sdk_reply", request))
        return _FakeResponse(message_id="om_reply")

    adapter._apply_gateway_event = apply
    message_api.create = create
    message_api.reply = reply

    result = await adapter.send(chat_id, f"matrix payload: {case}", reply_to=reply_to, metadata=metadata)

    assert result.success is True
    assert result.message_id == expected_message_id
    assert [entry[0] for entry in timeline] == [
        "event",
        f"sdk_{expected_sdk_method}",
        "event",
    ]
    events = [entry[1] for entry in timeline if entry[0] == "event"]
    request = (
        message_api.create_calls[0]
        if expected_sdk_method == "create"
        else message_api.reply_calls[0]
    )
    expected_delivery_id = adapter._delivery_id_for(
        expected_operation,
        metadata=metadata,
        parts=[
            chat_id,
            reply_to or "",
            request.request_body.msg_type,
            request.request_body.content,
        ],
    )
    delivery_id = events[0]["delivery_id"]
    assert delivery_id == expected_delivery_id
    assert events[1]["delivery_id"] == delivery_id
    _assert_sent_matrix_events(
        events,
        operation=expected_operation,
        delivery_id=delivery_id,
        target=expected_target,
        inbound_id=metadata["inbound_id"],
        session_id=metadata["session_id"],
        correlation_id=metadata["correlation_id"],
        message_id=expected_message_id,
    )

    assert len(message_api.create_calls) + len(message_api.reply_calls) == 1
    if expected_sdk_method == "create":
        assert len(message_api.create_calls) == 1
        _assert_create_request_contract(
            adapter,
            request,
            delivery_id=delivery_id,
            receive_id=chat_id,
            msg_type=request.request_body.msg_type,
        )
        assert request.request_body.receive_id == expected_target.removeprefix("feishu:chat:")
    else:
        assert len(message_api.reply_calls) == 1
        _assert_reply_request_contract(
            adapter,
            request,
            delivery_id=delivery_id,
            parent_message_id=reply_to,
            msg_type=request.request_body.msg_type,
        )
    assert request.request_body.msg_type in {"text", "post"}
    assert request.request_body.content


@pytest.mark.asyncio
async def test_audited_send_uses_allowlisted_stream_fresh_final_hint(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    base_delivery_id = "delivery-stream-final"
    metadata = {
        **_metadata(base_delivery_id),
        "delivery_operation_hint": "stream_fresh_final",
    }

    first = await adapter.send("oc_chat", "hello", metadata=metadata)
    second = await adapter.send("oc_chat", "hello", metadata=metadata)

    assert first.success is True
    assert first.message_id == "om_created"
    assert second.success is True
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_sent",
        "delivery_pending",
        "delivery_sent",
    ]
    assert events[0]["operation"] == "stream_fresh_final"
    assert events[1]["operation"] == "stream_fresh_final"
    assert events[2]["operation"] == "stream_fresh_final"
    assert events[3]["operation"] == "stream_fresh_final"
    assert events[0]["delivery_id"] != base_delivery_id
    assert events[1]["delivery_id"] == events[0]["delivery_id"]
    assert events[2]["delivery_id"] == events[0]["delivery_id"]
    assert events[3]["delivery_id"] == events[0]["delivery_id"]
    assert message_api.create_calls[0].request_body.uuid == events[0]["delivery_id"]
    assert message_api.create_calls[1].request_body.uuid == events[0]["delivery_id"]


@pytest.mark.asyncio
async def test_audited_send_uses_allowlisted_queued_followup_first_reply_hint(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    base_delivery_id = "queued_followup_first_reply:om_followup"
    metadata = {
        **_metadata(base_delivery_id),
        "inbound_id": "om_followup",
        "delivery_operation_hint": "queued_followup_first_reply",
    }

    result = await adapter.send("oc_chat", "hello", metadata=metadata)

    assert result.success is True
    assert result.message_id == "om_created"
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[0]["operation"] == "queued_followup_first_reply"
    assert events[1]["operation"] == "queued_followup_first_reply"
    assert events[0]["delivery_id"] != base_delivery_id
    assert events[1]["delivery_id"] == events[0]["delivery_id"]
    assert message_api.create_calls[0].request_body.uuid == adapter._idempotency_key_for_delivery(
        events[0]["delivery_id"]
    )


@pytest.mark.asyncio
async def test_audited_queued_followup_long_reply_uses_unique_chunk_deliveries(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    metadata = {
        **_metadata("queued_followup_first_reply:om_followup"),
        "inbound_id": "om_followup",
        "delivery_operation_hint": "queued_followup_first_reply",
    }

    result = await adapter.send("oc_chat", "x" * 9000, metadata=metadata)

    assert result.success is True
    assert len(message_api.create_calls) == 2
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_sent",
        "delivery_pending",
        "delivery_sent",
    ]
    assert [event["operation"] for event in events] == [
        "queued_followup_first_reply",
        "queued_followup_first_reply",
        "queued_followup_first_reply",
        "queued_followup_first_reply",
    ]
    first_delivery_id = events[0]["delivery_id"]
    second_delivery_id = events[2]["delivery_id"]
    assert first_delivery_id != second_delivery_id
    assert events[1]["delivery_id"] == first_delivery_id
    assert events[3]["delivery_id"] == second_delivery_id
    uuids = [call.request_body.uuid for call in message_api.create_calls]
    assert uuids[0] != uuids[1]
    assert uuids == [
        adapter._idempotency_key_for_delivery(first_delivery_id),
        adapter._idempotency_key_for_delivery(second_delivery_id),
    ]


@pytest.mark.asyncio
async def test_audited_chunked_send_continues_after_first_chunk_replay(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []
    metadata = {
        **_metadata("queued_followup_first_reply:om_followup"),
        "inbound_id": "om_followup",
        "delivery_operation_hint": "queued_followup_first_reply",
    }

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending" and len(_event_types(events)) == 1:
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id=event["delivery_id"],
                    inbound_id=event["inbound_id"],
                    target=event["target"],
                    session_id=event["session_id"],
                    correlation_id=event["correlation_id"],
                    status="sent",
                    feishu_message_id="om_replayed_chunk_1",
                ),
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send("oc_chat", "x" * 9000, metadata=metadata)

    assert result.success is True
    assert result.message_id == "om_created"
    assert result.continuation_message_ids == ("om_replayed_chunk_1", "om_created")
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_pending",
        "delivery_sent",
    ]
    assert len(message_api.create_calls) == 1
    assert message_api.create_calls[0].request_body.uuid == adapter._idempotency_key_for_delivery(
        events[1]["delivery_id"]
    )


@pytest.mark.asyncio
async def test_audited_send_rejects_unsupported_operation_hint_before_sdk(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    metadata = {
        **_metadata("delivery-bad-hint"),
        "delivery_operation_hint": "raw_payload_override",
    }

    result = await adapter.send("oc_chat", "hello", metadata=metadata)

    assert result.success is False
    assert result.error == "unsupported delivery operation hint"
    assert events == []
    assert message_api.create_calls == []
    assert message_api.reply_calls == []


@pytest.mark.asyncio
async def test_non_audited_send_ignores_operation_hint_for_legacy_behavior():
    adapter, message_api = _non_audited_adapter()
    metadata = {
        **_metadata("delivery-dev-mode"),
        "delivery_operation_hint": "raw_payload_override",
    }

    result = await adapter.send("oc_chat", "hello", metadata=metadata)

    assert result.success is True
    assert result.message_id == "om_created"
    assert len(message_api.create_calls) == 1
    assert message_api.reply_calls == []


@pytest.mark.asyncio
async def test_repeat_pending_delivery_does_not_call_sdk_twice(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []
    pending_count = 0

    async def apply(event):
        nonlocal pending_count
        events.append(event)
        if event.get("type") == "delivery_pending":
            pending_count += 1
            return pending_count == 1
        return True

    adapter._apply_gateway_event = apply

    metadata = _metadata("delivery-reply")
    first = await adapter.send("oc_chat", "hello", reply_to="om_parent", metadata=metadata)
    second = await adapter.send("oc_chat", "hello", reply_to="om_parent", metadata=metadata)

    assert first.success is True
    assert second.success is False
    assert second.error == "delivery_pending apply failed"
    assert _event_types(events) == ["delivery_pending", "delivery_sent", "delivery_pending"]
    assert len(message_api.reply_calls) == 1
    assert message_api.reply_calls[0].request_body.uuid == "delivery-reply"


@pytest.mark.asyncio
async def test_pending_delivery_record_admission_continues_sdk_and_writes_sent(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id="delivery-reply",
                    status="pending",
                    feishu_message_id=None,
                ),
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is True
    assert result.message_id == "om_reply"
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert len(message_api.reply_calls) == 1
    assert message_api.reply_calls[0].request_body.uuid == "delivery-reply"
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_repeat_pending_delivery_reuses_durable_sent_record_without_sdk_or_sent_write(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []
    pending_count = 0

    async def apply(event):
        nonlocal pending_count
        events.append(event)
        if event.get("type") == "delivery_pending":
            pending_count += 1
            if pending_count == 2:
                return SimpleNamespace(
                    ok=True,
                    action=_delivery_record_action(
                        delivery_id="delivery-reply",
                        status="sent",
                        feishu_message_id="om_existing_msg",
                    ),
                )
        return True

    adapter._apply_gateway_event = apply

    metadata = _metadata("delivery-reply")
    first = await adapter.send("oc_chat", "hello", reply_to="om_parent", metadata=metadata)
    second = await adapter.send("oc_chat", "hello", reply_to="om_parent", metadata=metadata)

    assert first.success is True
    assert second.success is True
    assert second.message_id == "om_existing_msg"
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_sent",
        "delivery_pending",
    ]
    assert len(message_api.reply_calls) == 1
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_acked_delivery_record_reuses_without_sdk_or_sent_write(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id="delivery-reply",
                    status="acked",
                    feishu_message_id="om_acked_msg",
                ),
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is True
    assert result.message_id == "om_acked_msg"
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.reply_calls == []
    assert message_api.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_override",
    [
        {"delivery_id": "delivery-other"},
        {"inbound_id": "inbound-other"},
        {"target": "feishu:chat:oc_other"},
        {"session_id": "session-other"},
        {"correlation_id": "corr-other"},
    ],
)
async def test_delivery_record_identity_mismatch_fail_closed_without_sdk(
    tmp_path,
    identity_override,
):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    status="sent",
                    feishu_message_id="om_existing_msg",
                    **identity_override,
                ),
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is False
    assert result.error == "delivery_pending apply failed"
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.reply_calls == []
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_malformed_delivery_record_action_fail_closed_without_sdk(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action={"type": "unexpected_action", "record": {}},
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is False
    assert result.error == "delivery_pending apply failed"
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.reply_calls == []
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_delivery_pending_rejects_success_envelope_with_wrong_event_action(
    tmp_path,
    monkeypatch,
):
    adapter, message_api = _adapter(tmp_path)
    applied = []

    async def apply_gateway_event_async(event, _state_dir):
        applied.append(event)
        return SimpleNamespace(
            ok=True,
            event_type="feishu_inbound",
            action={"type": "inbound_admission", "inbound_id": "inbound-1"},
        )

    monkeypatch.setattr(
        feishu_module.gateway_event_ledger,
        "apply_gateway_event_async",
        apply_gateway_event_async,
    )

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is False
    assert result.error == "delivery_pending apply failed"
    assert _event_types(applied) == ["delivery_pending"]
    assert message_api.reply_calls == []
    assert message_api.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message_id"),
    [
        ("unknown", "om_existing_msg"),
        ("pending", "bad/message/id"),
        ("sent", None),
        ("acked", "bad/message/id"),
    ],
)
async def test_repeat_pending_delivery_record_fail_closed_when_not_reusable(
    tmp_path,
    status,
    message_id,
):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id="delivery-reply",
                    status=status,
                    feishu_message_id=message_id,
                ),
            )
        return True

    adapter._apply_gateway_event = apply

    result = await adapter.send(
        "oc_chat",
        "hello",
        reply_to="om_parent",
        metadata=_metadata("delivery-reply"),
    )

    assert result.success is False
    assert result.error == "delivery_pending apply failed"
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.reply_calls == []
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_pending_apply_failure_aborts_before_sdk(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_pending"})

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is False
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.create_calls == []


@pytest.mark.asyncio
async def test_sdk_success_missing_message_id_after_pending_applies_unknown_not_failed(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    message_api.create_response = _FakeResponse(message_id=None)

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "unknown_delivery_state"]
    assert events[-1]["failure_class"] == "missing_message_id_after_sdk_success"
    assert "delivery_failed" not in _event_types(events)


@pytest.mark.asyncio
async def test_sent_apply_failure_after_sdk_success_applies_unknown_and_does_not_resend(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_sent"})

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is False
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_sent",
        "unknown_delivery_state",
    ]
    assert message_api.create_calls and len(message_api.create_calls) == 1
    assert events[-1]["failure_class"] == "delivery_sent_apply_failed"
    assert events[-1]["message_id"] == "om_created"


@pytest.mark.asyncio
async def test_audited_send_falls_back_to_text_on_post_rejection_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    message_api.create_response = _FakeResponse(
        ok=False,
        code=230001,
        msg="content format of the post type is incorrect",
    )

    def create(request):
        message_api.create_calls.append(request)
        if len(message_api.create_calls) == 1:
            return _FakeResponse(
                ok=False,
                code=230001,
                msg="content format of the post type is incorrect",
            )
        return _FakeResponse(message_id="om_text_fallback")

    message_api.create = create

    result = await adapter.send(
        "oc_chat",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata("delivery-post-response"),
    )

    assert result.success is True
    assert result.message_id == "om_text_fallback"
    assert [call.request_body.msg_type for call in message_api.create_calls] == [
        "post",
        "text",
    ]
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_failed",
        "delivery_pending",
        "delivery_sent",
    ]
    assert events[2]["delivery_id"] != "delivery-post-response"


@pytest.mark.asyncio
async def test_audited_send_invalid_post_response_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_failed"})

    def create(request):
        message_api.create_calls.append(request)
        if len(message_api.create_calls) == 1:
            return _FakeResponse(
                ok=False,
                code=230001,
                msg="content format of the post type is incorrect",
            )
        return _FakeResponse(message_id="om_text_fallback")

    message_api.create = create

    result = await adapter.send(
        "oc_chat",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata("delivery-post-response"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.create_calls) == 1
    assert message_api.create_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_send_falls_back_to_text_on_post_rejection_exception(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    def create(request):
        message_api.create_calls.append(request)
        if len(message_api.create_calls) == 1:
            raise ValueError("content format of the post type is incorrect")
        return _FakeResponse(message_id="om_text_fallback")

    message_api.create = create

    result = await adapter.send(
        "oc_chat",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata("delivery-post-exception"),
    )

    assert result.success is True
    assert result.message_id == "om_text_fallback"
    assert [call.request_body.msg_type for call in message_api.create_calls] == [
        "post",
        "text",
    ]
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_failed",
        "delivery_pending",
        "delivery_sent",
    ]
    assert "unknown_delivery_state" not in _event_types(events)


@pytest.mark.asyncio
async def test_audited_send_invalid_post_exception_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_failed"})

    def create(request):
        message_api.create_calls.append(request)
        if len(message_api.create_calls) == 1:
            raise ValueError("content format of the post type is incorrect")
        return _FakeResponse(message_id="om_text_fallback")

    message_api.create = create

    result = await adapter.send(
        "oc_chat",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata("delivery-post-exception"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.create_calls) == 1
    assert message_api.create_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_send_does_not_fallback_on_ambiguous_invalid_post_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    def create(request):
        message_api.create_calls.append(request)
        return _FakeResponse(
            ok=False,
            code=99991400,
            msg="content format of the post type is incorrect",
        )

    message_api.create = create

    result = await adapter.send(
        "oc_chat",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata("delivery-post-ambiguous"),
    )

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "unknown_delivery_state"]
    assert events[-1]["failure_class"] == "retryable_non_acceptance_after_admission"
    assert len(message_api.create_calls) == 1
    assert message_api.create_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_terminal_non_acceptance_records_delivery_failed(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    message_api.create_response = _FakeResponse(ok=False, code=230001, msg="rejected")

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert events[-1]["failure_class"] == "feishu_terminal_non_acceptance"


@pytest.mark.asyncio
async def test_exception_after_pending_applies_unknown_and_no_delivery_failed_or_resend(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    message_api.create_exception = TimeoutError("timed out")

    result = await adapter.send("oc_chat", "hello", metadata=_metadata())

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "unknown_delivery_state"]
    assert events[-1]["failure_class"] == "sdk_exception_after_admission"
    assert len(message_api.create_calls) == 1


@pytest.mark.asyncio
async def test_edit_validates_existing_message_id_and_records_sent_with_same_id(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter)

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "updated",
        metadata=_metadata_with_current_admission("delivery-edit"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert message_api.update_calls[0].message_id == "om_existing"
    assert _delivery_audit_event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[0]["operation"] == "message_edit"
    delivery_events = _delivery_events(events)
    assert delivery_events[1]["operation"] == "message_edit"
    assert delivery_events[-1]["message_id"] == "om_existing"


@pytest.mark.asyncio
async def test_message_edit_operation_matrix_uses_existing_target_as_message_evidence(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    timeline = []

    async def apply(event):
        timeline.append(("event", event))
        return True

    def update(request):
        message_api.update_calls.append(request)
        timeline.append(("sdk_update", request))
        return _FakeResponse(message_id=None)

    adapter._apply_gateway_event = apply
    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "edited final",
        metadata=_metadata_with_current_admission("outbound-delivery-9-edit-1"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert [
        entry[0]
        if entry[0] == "sdk_update"
        else entry[1]["type"]
        for entry in timeline
        if entry[0] == "sdk_update"
        or entry[1].get("type") in {"delivery_pending", "delivery_sent"}
    ] == ["delivery_pending", "sdk_update", "delivery_sent"]
    events = [entry[1] for entry in timeline if entry[0] == "event"]
    _assert_sent_matrix_events(
        events,
        operation="message_edit",
        delivery_id="outbound-delivery-9-edit-1",
        target="feishu:message:om_existing",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
        message_id="om_existing",
    )
    assert len(message_api.update_calls) == 1
    request = message_api.update_calls[0]
    _assert_update_request_contract(
        request,
        message_id="om_existing",
        msg_type=request.request_body.msg_type,
    )
    assert request.request_body.msg_type in {"text", "post"}
    assert request.request_body.content


@pytest.mark.asyncio
async def test_audited_edit_falls_back_to_text_on_post_rejection_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter)

    def update(request):
        message_api.update_calls.append(request)
        if len(message_api.update_calls) == 1:
            return _FakeResponse(
                ok=False,
                code=230001,
                msg="content format of the post type is incorrect",
            )
        return _FakeResponse(message_id=None)

    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata_with_current_admission("delivery-edit-post-response"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert [call.request_body.msg_type for call in message_api.update_calls] == [
        "post",
        "text",
    ]
    assert _delivery_audit_event_types(events) == [
        "delivery_pending",
        "delivery_failed",
        "delivery_pending",
        "delivery_sent",
    ]
    assert _delivery_events(events)[-1]["message_id"] == "om_existing"


@pytest.mark.asyncio
async def test_audited_edit_invalid_post_response_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_failed"})

    def update(request):
        message_api.update_calls.append(request)
        if len(message_api.update_calls) == 1:
            return _FakeResponse(
                ok=False,
                code=230001,
                msg="content format of the post type is incorrect",
            )
        return _FakeResponse(message_id=None)

    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata_with_current_admission("delivery-edit-post-response"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _delivery_audit_event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_edit_falls_back_to_text_on_post_rejection_exception(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter)

    def update(request):
        message_api.update_calls.append(request)
        if len(message_api.update_calls) == 1:
            raise ValueError("content format of the post type is incorrect")
        return _FakeResponse(message_id=None)

    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata_with_current_admission("delivery-edit-post-exception"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert [call.request_body.msg_type for call in message_api.update_calls] == [
        "post",
        "text",
    ]
    assert _delivery_audit_event_types(events) == [
        "delivery_pending",
        "delivery_failed",
        "delivery_pending",
        "delivery_sent",
    ]
    assert "unknown_delivery_state" not in _delivery_audit_event_types(events)


@pytest.mark.asyncio
async def test_audited_edit_invalid_post_exception_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter, fail_event_types={"delivery_failed"})

    def update(request):
        message_api.update_calls.append(request)
        if len(message_api.update_calls) == 1:
            raise ValueError("content format of the post type is incorrect")
        return _FakeResponse(message_id=None)

    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata_with_current_admission("delivery-edit-post-exception"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _delivery_audit_event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_edit_does_not_fallback_on_ambiguous_invalid_post_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    await _seed_current_bot_owned_message(adapter, message_api)
    events = _install_event_recorder(adapter)

    def update(request):
        message_api.update_calls.append(request)
        return _FakeResponse(
            ok=False,
            code=99991400,
            msg="content format of the post type is incorrect",
        )

    message_api.update = update

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "可以用 **粗体** 和 *斜体*。",
        metadata=_metadata_with_current_admission("delivery-edit-post-ambiguous"),
    )

    assert result.success is False
    assert _delivery_audit_event_types(events) == [
        "delivery_pending",
        "unknown_delivery_state",
    ]
    assert events[-1]["failure_class"] == "retryable_non_acceptance_after_admission"
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


def _create_descriptor(content, *, uuid_value="descriptor-card-create"):
    return {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": "chat_id"},
        "body": {
            "receive_id": "oc_chat",
            "msg_type": "interactive",
            "content": content,
            "uuid": uuid_value,
        },
    }


def _patch_descriptor(content):
    return {
        "operation": "patch_interactive_message",
        "method": "PATCH",
        "path": "/open-apis/im/v1/messages/om_card",
        "params": {},
        "body": {"content": content},
    }


def _status_card_create_action(**overrides):
    action = {
        "type": "create",
        "card_id": "task-1",
        "state": "running",
        "text": "preflight started",
        "requires_final_reply": True,
        "fallback_text": "task running: preflight started",
        "feishu_card": {"config": {"wide_screen_mode": True}},
        "feishu_request": _create_descriptor('{"config":{"wide_screen_mode":true}}'),
    }
    action.update(overrides)
    return action


def _status_card_update_action(**overrides):
    action = {
        "type": "update",
        "card_id": "task-1",
        "state": "completed",
        "text": "preflight complete",
        "requires_final_reply": False,
        "fallback_text": "task succeeded: preflight complete",
        "feishu_card": {"config": {"wide_screen_mode": True}},
        "feishu_request": _patch_descriptor('{"config":{"wide_screen_mode":true}}'),
    }
    action.update(overrides)
    return action


def _status_card_suppressed_action(**overrides):
    action = {
        "type": "suppressed",
        "reason": "status_card_throttled",
        "fallback_text": "task running: preflight started",
        "feishu_card": {"config": {"wide_screen_mode": True}},
    }
    action.update(overrides)
    return action


STATUS_CARD_INTERNALIZATION_DEFERRED = pytest.mark.skip(
    reason="status-card/task card internalization intentionally deferred by user scope"
)


@pytest.mark.asyncio
async def test_execute_feishu_request_descriptor_requires_broker_before_sdk_builder(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    adapter._build_create_message_body = MagicMock(
        wraps=adapter._build_create_message_body
    )
    adapter._build_create_message_request = MagicMock(
        wraps=adapter._build_create_message_request
    )

    result = await adapter.execute_feishu_request_descriptor(
        _create_descriptor('{"config":{"wide_screen_mode":true}}'),
        delivery_id="delivery-card-create",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is False
    assert result.error == "feishu_legacy_descriptor_requires_broker"
    assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
    _assert_legacy_descriptor_denied(events[0], surface="feishu.descriptor")
    adapter._build_create_message_body.assert_not_called()
    adapter._build_create_message_request.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_execute_feishu_request_descriptor_requires_audit_state_before_sdk_builder():
    adapter, message_api = _non_audited_adapter()
    adapter._build_create_message_body = MagicMock(
        wraps=adapter._build_create_message_body
    )
    adapter._build_create_message_request = MagicMock(
        wraps=adapter._build_create_message_request
    )

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            _create_descriptor('{"config":{"wide_screen_mode":true}}'),
            delivery_id="delivery-card-create",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "feishu_legacy_descriptor_audit_state_missing"
    adapter._build_create_message_body.assert_not_called()
    adapter._build_create_message_request.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_descriptor_create_interactive_denies_raw_descriptor_before_sdk_builder(
    tmp_path,
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    adapter._build_create_message_body = MagicMock(
        wraps=adapter._build_create_message_body
    )
    adapter._build_create_message_request = MagicMock(
        wraps=adapter._build_create_message_request
    )
    delivery_id = "delivery-card-create"
    descriptor_uuid = "descriptor-card-create"
    assert descriptor_uuid != delivery_id

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            _create_descriptor(
                '{"config":{"wide_screen_mode":true}}',
                uuid_value=descriptor_uuid,
            ),
            delivery_id=delivery_id,
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "feishu_raw_descriptor_execution_denied"
    assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
    _assert_legacy_descriptor_denied(
        events[0],
        surface="feishu.descriptor",
        failure_class="feishu_raw_descriptor_execution_denied",
    )
    adapter._build_create_message_body.assert_not_called()
    adapter._build_create_message_request.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
async def test_status_card_create_action_executes_descriptor_and_writes_delivery_sent(
    tmp_path,
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    delivery_id = "delivery-card-create"
    descriptor_uuid = "descriptor-card-create"
    assert descriptor_uuid != delivery_id

    with _broker_context():
        result = await adapter.execute_status_card_action(
            {
                "type": "status_card",
                "card_action": _status_card_create_action(
                    feishu_request=_create_descriptor(
                        '{"config":{"wide_screen_mode":true}}',
                        uuid_value=descriptor_uuid,
                    )
                ),
            },
            delivery_id=delivery_id,
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is True
    assert result.message_id == "om_created"
    assert len(message_api.create_calls) == 1
    assert message_api.update_calls == []
    assert message_api.create_calls[0].request_body.uuid == adapter._idempotency_key_for_delivery(
        descriptor_uuid
    )
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[0]["operation"] == "status_card_create"
    assert events[0]["delivery_id"] == delivery_id
    assert events[1]["operation"] == "status_card_create"
    assert events[1]["delivery_id"] == delivery_id
    assert events[-1]["message_id"] == "om_created"


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
async def test_status_card_create_operation_matrix_uses_descriptor_create_builder(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    timeline = []
    delivery_id = "delivery-card-create"
    descriptor_uuid = "descriptor-card-create"
    assert descriptor_uuid != delivery_id
    descriptor = _create_descriptor(
        '{"config":{"wide_screen_mode":true}}',
        uuid_value=descriptor_uuid,
    )

    async def apply(event):
        timeline.append(("event", event))
        return True

    def create(request):
        message_api.create_calls.append(request)
        timeline.append(("sdk_create", request))
        return _FakeResponse(message_id="om_created")

    adapter._apply_gateway_event = apply
    message_api.create = create

    with _broker_context():
        result = await adapter.execute_status_card_action(
            _status_card_create_action(feishu_request=descriptor),
            delivery_id=delivery_id,
            inbound_id="task-1:create",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is True
    assert result.message_id == "om_created"
    assert descriptor["method"] == "POST"
    assert descriptor["path"] == "/open-apis/im/v1/messages"
    assert [entry[0] for entry in timeline] == ["event", "sdk_create", "event"]
    events = [entry[1] for entry in timeline if entry[0] == "event"]
    _assert_sent_matrix_events(
        events,
        operation="status_card_create",
        delivery_id=delivery_id,
        target="feishu:chat:oc_chat",
        inbound_id="task-1:create",
        session_id="session-a",
        correlation_id="corr-a",
        message_id="om_created",
    )
    assert len(message_api.create_calls) == 1
    request = message_api.create_calls[0]
    _assert_create_request_contract(
        adapter,
        request,
        delivery_id=delivery_id,
        receive_id=descriptor["body"]["receive_id"],
        receive_id_type=descriptor["params"]["receive_id_type"],
        msg_type="interactive",
        content=descriptor["body"]["content"],
        idempotency_source_id=descriptor_uuid,
    )


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
async def test_status_card_update_action_executes_patch_descriptor(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    with _broker_context():
        result = await adapter.execute_status_card_action(
            _status_card_update_action(),
            delivery_id="delivery-card-patch",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is True
    assert result.message_id == "om_card"
    assert message_api.create_calls == []
    assert len(message_api.update_calls) == 1
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[0]["operation"] == "status_card_patch"
    assert events[1]["operation"] == "status_card_patch"
    assert events[-1]["message_id"] == "om_card"


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
async def test_status_card_patch_operation_matrix_uses_descriptor_patch_builder(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    timeline = []
    descriptor = _patch_descriptor('{"config":{"wide_screen_mode":true}}')

    async def apply(event):
        timeline.append(("event", event))
        return True

    def update(request):
        message_api.update_calls.append(request)
        timeline.append(("sdk_update", request))
        return _FakeResponse(message_id=None)

    adapter._apply_gateway_event = apply
    message_api.update = update

    with _broker_context():
        result = await adapter.execute_status_card_action(
            _status_card_update_action(feishu_request=descriptor),
            delivery_id="delivery-card-patch",
            inbound_id="task-1:patch-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is True
    assert result.message_id == "om_card"
    assert descriptor["method"] == "PATCH"
    assert descriptor["path"] == "/open-apis/im/v1/messages/om_card"
    assert [entry[0] for entry in timeline] == ["event", "sdk_update", "event"]
    events = [entry[1] for entry in timeline if entry[0] == "event"]
    _assert_sent_matrix_events(
        events,
        operation="status_card_patch",
        delivery_id="delivery-card-patch",
        target="feishu:message:om_card",
        inbound_id="task-1:patch-1",
        session_id="session-a",
        correlation_id="corr-a",
        message_id="om_card",
    )
    assert message_api.create_calls == []
    assert len(message_api.update_calls) == 1
    request = message_api.update_calls[0]
    _assert_update_request_contract(
        request,
        message_id="om_card",
        msg_type="interactive",
        content=descriptor["body"]["content"],
    )


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        _status_card_suppressed_action(),
        _status_card_create_action(feishu_request=None),
        _status_card_update_action(feishu_request=None),
    ],
)
async def test_status_card_action_without_feishu_request_returns_noop_without_delivery(
    tmp_path, action
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    with _broker_context():
        result = await adapter.execute_status_card_action(
            action,
            delivery_id="delivery-card-noop",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is True
    assert result.message_id is None
    assert result.raw_response == {"type": "status_card_noop", "reason": action["type"]}
    assert events == []
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@STATUS_CARD_INTERNALIZATION_DEFERRED
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        {"type": "status_card"},
        {"type": "unknown"},
        _status_card_create_action(state="not_a_task_state"),
        _status_card_suppressed_action(feishu_request=_patch_descriptor("{}")),
    ],
)
async def test_status_card_malformed_or_unsupported_action_fails_closed(tmp_path, action):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    with _broker_context():
        result = await adapter.execute_status_card_action(
            action,
            delivery_id="delivery-card-bad",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "invalid status_card action"
    assert events == []
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_status_card_requires_broker_before_action_extraction(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    validator = MagicMock(wraps=adapter._validated_status_card_action_for_send)
    adapter._validated_status_card_action_for_send = validator

    result = await adapter.execute_status_card_action(
        {
            "type": "status_card",
            "card_action": _status_card_create_action(),
        },
        delivery_id="delivery-card-create",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is False
    assert result.error == "feishu_legacy_descriptor_requires_broker"
    assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
    _assert_legacy_descriptor_denied(events[0], surface="feishu.status_card")
    validator.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_descriptor_patch_interactive_denies_raw_descriptor_before_sdk_builder(
    tmp_path,
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    adapter._build_update_message_body = MagicMock(
        wraps=adapter._build_update_message_body
    )
    adapter._build_update_message_request = MagicMock(
        wraps=adapter._build_update_message_request
    )

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            _patch_descriptor('{"config":{"wide_screen_mode":true}}'),
            delivery_id="delivery-card-patch",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "feishu_raw_descriptor_execution_denied"
    assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
    _assert_legacy_descriptor_denied(
        events[0],
        surface="feishu.descriptor",
        failure_class="feishu_raw_descriptor_execution_denied",
    )
    adapter._build_update_message_body.assert_not_called()
    adapter._build_update_message_request.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_descriptor_rejects_extra_fields_before_network(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    descriptor = {**_create_descriptor("{}"), "url": "https://example.invalid"}

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            descriptor,
            delivery_id="delivery-bad-descriptor",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert events[1]["failure_class"] == "invalid_feishu_request_descriptor"
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_invalid_descriptor_durable_delivery_record_does_not_write_failed(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id="delivery-bad-descriptor",
                    target="feishu:descriptor",
                    status="sent",
                    feishu_message_id="om_existing_msg",
                ),
            )
        return True

    adapter._apply_gateway_event = apply
    descriptor = {**_create_descriptor("{}"), "url": "https://example.invalid"}

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            descriptor,
            delivery_id="delivery-bad-descriptor",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "invalid Feishu request descriptor"
    assert _event_types(events) == ["delivery_pending"]
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_invalid_descriptor_pending_delivery_record_still_writes_failed(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action=_delivery_record_action(
                    delivery_id="delivery-bad-descriptor",
                    target="feishu:descriptor",
                    status="pending",
                    feishu_message_id=None,
                ),
            )
        return True

    adapter._apply_gateway_event = apply
    descriptor = {**_create_descriptor("{}"), "url": "https://example.invalid"}

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            descriptor,
            delivery_id="delivery-bad-descriptor",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "invalid Feishu request descriptor"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert events[1]["failure_class"] == "invalid_feishu_request_descriptor"
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_invalid_descriptor_malformed_delivery_record_still_writes_failed(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = []

    async def apply(event):
        events.append(event)
        if event.get("type") == "delivery_pending":
            return SimpleNamespace(
                ok=True,
                action={"type": "delivery_record", "record": object()},
            )
        return True

    adapter._apply_gateway_event = apply
    descriptor = {**_create_descriptor("{}"), "url": "https://example.invalid"}

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            descriptor,
            delivery_id="delivery-bad-descriptor",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "invalid Feishu request descriptor"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert events[1]["failure_class"] == "invalid_feishu_request_descriptor"
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "descriptor",
    [
        {**_create_descriptor("{}"), "body": {**_create_descriptor("{}")["body"], "receive_id": "oc chat"}},
        {**_create_descriptor("{}"), "body": {**_create_descriptor("{}")["body"], "uuid": "bad uuid"}},
        _create_descriptor(
            json.dumps(
                {"config": {"wide_screen_mode": True}, "pad": "x" * 32768}
            )
        ),
        {
            **_patch_descriptor("{}"),
            "path": "/open-apis/im/v1/messages/om.card",
        },
    ],
)
async def test_descriptor_rejects_values_looser_than_gateway_envelope_validator(
    tmp_path, descriptor
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            descriptor,
            delivery_id="delivery-bad-descriptor-values",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert events[1]["failure_class"] == "invalid_feishu_request_descriptor"
    assert message_api.create_calls == []
    assert message_api.update_calls == []


@pytest.mark.asyncio
async def test_descriptor_content_denial_does_not_build_or_send_sdk_request(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    adapter._build_create_message_body = MagicMock(
        wraps=adapter._build_create_message_body
    )
    content = '{"config":{"wide_screen_mode":true}}'

    with _broker_context():
        result = await adapter.execute_feishu_request_descriptor(
            _create_descriptor(content),
            delivery_id="delivery-card-create",
            inbound_id="inbound-1",
            session_id="session-a",
            correlation_id="corr-a",
        )

    assert result.success is False
    assert result.error == "feishu_raw_descriptor_execution_denied"
    assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
    adapter._build_create_message_body.assert_not_called()
    assert message_api.create_calls == []
    assert message_api.update_calls == []

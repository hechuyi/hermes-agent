import json
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.feishu import FeishuAdapter


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


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
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


def _install_event_recorder(adapter, *, fail_event_types=()):
    calls = []
    fail_event_types = set(fail_event_types)

    async def apply(event):
        calls.append(event)
        return event.get("type") not in fail_event_types

    adapter._apply_gateway_event = apply
    return calls


def _event_types(calls):
    return [call.get("type") for call in calls if "type" in call]


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
    assert events[0][1]["delivery_id"] == "delivery-create"
    assert events[1][0] == "sdk_create"
    assert events[2][0] == "event"
    assert events[2][1]["type"] == "delivery_sent"
    assert events[2][1]["delivery_id"] == "delivery-create"
    assert events[2][1]["message_id"] == "om_created"


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
    events = _install_event_recorder(adapter)

    result = await adapter.edit_message(
        "oc_chat",
        "om_existing",
        "updated",
        metadata=_metadata("delivery-edit"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert message_api.update_calls[0].message_id == "om_existing"
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[-1]["message_id"] == "om_existing"


@pytest.mark.asyncio
async def test_audited_edit_falls_back_to_text_on_post_rejection_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
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
        metadata=_metadata("delivery-edit-post-response"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert [call.request_body.msg_type for call in message_api.update_calls] == [
        "post",
        "text",
    ]
    assert _event_types(events) == [
        "delivery_pending",
        "delivery_failed",
        "delivery_pending",
        "delivery_sent",
    ]
    assert events[-1]["message_id"] == "om_existing"


@pytest.mark.asyncio
async def test_audited_edit_invalid_post_response_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
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
        metadata=_metadata("delivery-edit-post-response"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_edit_falls_back_to_text_on_post_rejection_exception(tmp_path):
    adapter, message_api = _adapter(tmp_path)
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
        metadata=_metadata("delivery-edit-post-exception"),
    )

    assert result.success is True
    assert result.message_id == "om_existing"
    assert [call.request_body.msg_type for call in message_api.update_calls] == [
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
async def test_audited_edit_invalid_post_exception_does_not_fallback_when_failed_apply_fails(tmp_path):
    adapter, message_api = _adapter(tmp_path)
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
        metadata=_metadata("delivery-edit-post-exception"),
    )

    assert result.success is False
    assert result.error == "delivery_failed apply failed"
    assert _event_types(events) == ["delivery_pending", "delivery_failed"]
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


@pytest.mark.asyncio
async def test_audited_edit_does_not_fallback_on_ambiguous_invalid_post_response(tmp_path):
    adapter, message_api = _adapter(tmp_path)
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
        metadata=_metadata("delivery-edit-post-ambiguous"),
    )

    assert result.success is False
    assert _event_types(events) == ["delivery_pending", "unknown_delivery_state"]
    assert events[-1]["failure_class"] == "retryable_non_acceptance_after_admission"
    assert len(message_api.update_calls) == 1
    assert message_api.update_calls[0].request_body.msg_type == "post"


def _create_descriptor(content):
    return {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": "chat_id"},
        "body": {
            "receive_id": "oc_chat",
            "msg_type": "interactive",
            "content": content,
            "uuid": "delivery-card-create",
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
        "state": "succeeded",
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


@pytest.mark.asyncio
async def test_descriptor_create_interactive_uses_sdk_create_builder_not_raw_http(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    result = await adapter.execute_feishu_request_descriptor(
        _create_descriptor('{"config":{"wide_screen_mode":true}}'),
        delivery_id="delivery-card-create",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is True
    assert len(message_api.create_calls) == 1
    request = message_api.create_calls[0]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_chat"
    assert request.request_body.msg_type == "interactive"
    assert request.request_body.uuid == "delivery-card-create"
    assert events[-1]["type"] == "delivery_sent"
    assert events[-1]["message_id"] == "om_created"


@pytest.mark.asyncio
async def test_status_card_create_action_executes_descriptor_and_writes_delivery_sent(
    tmp_path,
):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    result = await adapter.execute_status_card_action(
        {"type": "status_card", "card_action": _status_card_create_action()},
        delivery_id="delivery-card-create",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is True
    assert result.message_id == "om_created"
    assert len(message_api.create_calls) == 1
    assert message_api.update_calls == []
    assert _event_types(events) == ["delivery_pending", "delivery_sent"]
    assert events[-1]["message_id"] == "om_created"


@pytest.mark.asyncio
async def test_status_card_update_action_executes_patch_descriptor(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

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
    assert events[-1]["message_id"] == "om_card"


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
async def test_descriptor_patch_interactive_uses_sdk_update_builder_and_path_message_id(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)

    result = await adapter.execute_feishu_request_descriptor(
        _patch_descriptor('{"config":{"wide_screen_mode":true}}'),
        delivery_id="delivery-card-patch",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is True
    assert len(message_api.update_calls) == 1
    request = message_api.update_calls[0]
    assert request.message_id == "om_card"
    assert request.request_body.msg_type == "interactive"
    assert events[-1]["type"] == "delivery_sent"
    assert events[-1]["message_id"] == "om_card"


@pytest.mark.asyncio
async def test_descriptor_rejects_extra_fields_before_network(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    events = _install_event_recorder(adapter)
    descriptor = {**_create_descriptor("{}"), "url": "https://example.invalid"}

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
async def test_descriptor_content_is_not_double_serialized(tmp_path):
    adapter, message_api = _adapter(tmp_path)
    _install_event_recorder(adapter)
    content = '{"config":{"wide_screen_mode":true}}'

    result = await adapter.execute_feishu_request_descriptor(
        _create_descriptor(content),
        delivery_id="delivery-card-create",
        inbound_id="inbound-1",
        session_id="session-a",
        correlation_id="corr-a",
    )

    assert result.success is True
    request_content = message_api.create_calls[0].request_body.content
    assert request_content == content
    assert isinstance(json.loads(request_content), dict)

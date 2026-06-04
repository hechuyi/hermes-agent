from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.hermes_tools_gateway_event import HermesToolsGatewayEventResult
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.feishu import FeishuAdapter


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
    adapter.handle_message = AsyncMock()
    return adapter


def _message_event(adapter, *, message_id="om_inbound", message_type=MessageType.COMMAND):
    source = adapter.build_source(
        chat_id="oc_chat",
        chat_name="Feishu Chat",
        chat_type="group",
        user_id="ou_user",
        user_name="User",
    )
    return MessageEvent(
        text="/ping" if message_type == MessageType.COMMAND else "hello",
        message_type=message_type,
        source=source,
        raw_message=SimpleNamespace(event=SimpleNamespace()),
        message_id=message_id,
        timestamp=datetime.fromtimestamp(1_700_000_000),
    )


def _read_event(*, message_id="om_sent", event_id="ev_read_1"):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(message=SimpleNamespace(message_id=message_id)),
    )


def _ok(event_type):
    return HermesToolsGatewayEventResult(
        ok=True,
        event_type=event_type,
        action={"type": "delivery_record", "record": {"delivery_id": "delivery-1"}},
    )


def _failure(event_type, failure_class):
    return HermesToolsGatewayEventResult(
        ok=False,
        event_type=event_type,
        failure_class=failure_class,
        reason=failure_class,
        diagnostics="returncode=1 stdout_bytes=0 stderr_json=object",
    )


@pytest.mark.asyncio
async def test_normalized_inbound_event_applies_before_handle_message(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []

    def fake_apply(event, state_dir):
        calls.append(("apply", event, state_dir))
        return _ok("feishu_inbound")

    async def fake_handle(event):
        calls.append(("handle", event.message_id))

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        fake_apply,
    )
    adapter.handle_message = AsyncMock(side_effect=fake_handle)

    await adapter._handle_message_with_guards(_message_event(adapter))

    assert calls[0][0] == "apply"
    assert calls[0][1]["type"] == "feishu_inbound"
    assert calls[0][1]["inbound_id"] == "om_inbound"
    assert calls[0][1]["message_id"] == "om_inbound"
    assert calls[0][1]["timestamp"] == 1_700_000_000
    assert calls[0][2] == tmp_path
    assert calls[1] == ("handle", "om_inbound")


@pytest.mark.asyncio
async def test_inbound_apply_failure_blocks_handle_message(monkeypatch, tmp_path, caplog):
    adapter = _adapter(tmp_path)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda _event, _state_dir: _failure("feishu_inbound", "implicit_session_switch"),
    )

    await adapter._handle_message_with_guards(_message_event(adapter))

    adapter.handle_message.assert_not_awaited()
    assert "implicit_session_switch" in caplog.text


@pytest.mark.asyncio
async def test_text_batch_flush_applies_only_flushed_normalized_event(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []
    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda event, _state_dir: calls.append(event) or _ok("feishu_inbound"),
    )

    event = _message_event(adapter, message_id="om_text", message_type=MessageType.TEXT)
    await adapter._enqueue_text_event(event)

    assert calls == []
    await adapter._flush_text_batch_now(adapter._text_batch_key(event))

    assert [call["inbound_id"] for call in calls] == ["om_text"]
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_batch_flush_applies_only_flushed_normalized_event(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []
    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda event, _state_dir: calls.append(event) or _ok("feishu_inbound"),
    )

    event = _message_event(adapter, message_id="om_media", message_type=MessageType.PHOTO)
    event.media_urls = ["/tmp/image.png"]
    event.media_types = ["image/png"]
    await adapter._enqueue_media_event(event)

    assert calls == []
    await adapter._flush_media_batch_now(adapter._media_batch_key(event))

    assert [call["inbound_id"] for call in calls] == ["om_media"]
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_synthetic_reaction_and_card_command_events_are_applied(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []
    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda event, _state_dir: calls.append(event) or _ok("feishu_inbound"),
    )

    reaction_event = _message_event(adapter, message_id="om_reacted")
    reaction_event.text = "reaction:added:THUMBSUP"
    card_event = _message_event(adapter, message_id="card-token-1")
    card_event.message_type = MessageType.COMMAND
    card_event.text = "/card button"

    await adapter._handle_message_with_guards(reaction_event)
    await adapter._handle_message_with_guards(card_event)

    assert [call["inbound_id"] for call in calls] == ["om_reacted", "card-token-1"]
    assert [call["message_type"] for call in calls] == ["command", "command"]


def test_read_event_applies_feishu_ack_with_stable_feishu_event_id(
    monkeypatch, tmp_path
):
    adapter = _adapter(tmp_path)
    calls = []

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda event, state_dir: calls.append((event, state_dir)) or _ok("feishu_ack"),
    )

    adapter._on_message_read_event(_read_event(message_id="om_sent", event_id="ev_read_1"))

    assert len(calls) == 1
    event, state_dir = calls[0]
    assert event["type"] == "feishu_ack"
    assert event["message_id"] == "om_sent"
    assert event["ack_event_id"] == "ev_read_1"
    assert event["timestamp"] == pytest.approx(int(datetime.now().timestamp()), abs=2)
    assert state_dir == tmp_path


@pytest.mark.parametrize("failure_class", ["unknown_message_id", "conflicting_ack_event"])
def test_read_event_adapter_failures_are_classified_not_swallowed(
    monkeypatch, tmp_path, caplog, failure_class
):
    adapter = _adapter(tmp_path)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda _event, _state_dir: _failure("feishu_ack", failure_class),
    )

    adapter._on_message_read_event(_read_event(message_id="om_unknown", event_id="ev_read_2"))

    assert failure_class in caplog.text
    assert "om_unknown" not in caplog.text


def test_duplicate_read_event_success_is_recorded_without_failure(
    monkeypatch, tmp_path, caplog
):
    adapter = _adapter(tmp_path)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        lambda _event, _state_dir: _ok("feishu_ack"),
    )

    adapter._on_message_read_event(_read_event(message_id="om_sent", event_id="ev_same"))

    assert "failed" not in caplog.text.lower()


@pytest.mark.parametrize(
    "data",
    [
        SimpleNamespace(header=SimpleNamespace(event_id="ev_read_missing"), event=SimpleNamespace(message=SimpleNamespace())),
        SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(message_id="om_sent"))),
        SimpleNamespace(header=SimpleNamespace(), event=SimpleNamespace(message=SimpleNamespace(message_id="om_sent"))),
    ],
)
def test_malformed_read_event_without_reliable_ids_is_dropped(monkeypatch, tmp_path, data):
    adapter = _adapter(tmp_path)
    apply_mock = Mock()
    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        apply_mock,
    )

    adapter._on_message_read_event(data)

    apply_mock.assert_not_called()

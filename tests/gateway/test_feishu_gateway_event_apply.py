from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.feishu import FeishuAdapter, GatewayEventResult
from gateway.platforms.base import MessageEvent, MessageType


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
    adapter.handle_message = AsyncMock()
    return adapter


def test_gateway_event_state_dir_prefers_new_config_key(tmp_path):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"

    adapter = FeishuAdapter(
        PlatformConfig(
            extra={
                "hermes_tools_state_dir": str(old_dir),
                "gateway_event_state_dir": str(new_dir),
            }
        )
    )

    assert adapter._gateway_event_state_dir == new_dir
    assert adapter._hermes_tools_state_dir == new_dir


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


def _raw_message_event_data(*, message_id="om_inbound"):
    message = SimpleNamespace(
        message_id=message_id,
        chat_id="oc_chat",
        chat_type="p2p",
        message_type="text",
        content='{"text": "/ping"}',
        mentions=[],
        thread_id=None,
        parent_id=None,
        upper_message_id=None,
        root_id=None,
    )
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _read_event(*, message_id="om_sent", event_id="ev_read_1"):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(message_id_list=[message_id]),
    )


def _ok(event_type):
    return GatewayEventResult(
        ok=True,
        event_type=event_type,
        action={"type": "inbound_admission", "decision": "continue"},
    )


def _failure(event_type, failure_class):
    return GatewayEventResult(
        ok=False,
        event_type=event_type,
        failure_class=failure_class,
        reason=failure_class,
        diagnostics="returncode=1 stdout_bytes=0 stderr_json=object",
    )


def _reaction_adapter(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._app_id = "cli_self_app"
    msg = SimpleNamespace(
        sender=SimpleNamespace(sender_type="app", id="cli_self_app", id_type="app_id"),
        chat_id="oc_chat",
        chat_type="group",
        thread_id=None,
        parent_id=None,
        upper_message_id=None,
        root_id=None,
    )
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=[msg]))
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(get=Mock(return_value=response))))
    )
    adapter._build_get_message_request = Mock(return_value=object())
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "ou_user", "user_name": "User", "user_id_alt": None}
    )
    adapter.get_chat_info = AsyncMock(return_value={"name": "Feishu Chat", "type": "group", "reliable": True})
    adapter._handle_message_with_guards = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_normalized_inbound_event_applies_before_handle_message(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []

    async def fake_apply(event, state_dir):
        calls.append(("apply", event, state_dir))
        return _ok("feishu_inbound")

    async def fake_handle(event):
        calls.append(("handle", event.message_id))

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
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
async def test_legacy_seen_hit_still_enters_inbound_ledger(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter._seen_message_ids = {"om_legacy_seen": time.time()}
    adapter._seen_message_order = ["om_legacy_seen"]
    calls = []

    async def fake_apply(event, state_dir):
        calls.append(("apply", event, state_dir))
        return _ok("feishu_inbound")

    async def fake_process_inbound_message(**kwargs):
        await adapter._handle_message_with_guards(
            _message_event(adapter, message_id=kwargs["message_id"])
        )

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )
    adapter._process_inbound_message = fake_process_inbound_message

    await adapter._handle_message_event_data(
        _raw_message_event_data(message_id="om_legacy_seen")
    )

    assert calls[0][0] == "apply"
    assert calls[0][1]["type"] == "feishu_inbound"
    assert calls[0][1]["inbound_id"] == "om_legacy_seen"
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_ledger_duplicate_blocks_second_handle_message(tmp_path):
    adapter = _adapter(tmp_path)
    event = _message_event(adapter, message_id="om_duplicate")

    await adapter._handle_message_with_guards(event)
    await adapter._handle_message_with_guards(event)

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_inbound_apply_failure_blocks_handle_message(monkeypatch, tmp_path, caplog):
    adapter = _adapter(tmp_path)

    async def fake_apply(_event, _state_dir):
        return _failure("feishu_inbound", "implicit_session_switch")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )

    await adapter._handle_message_with_guards(_message_event(adapter))

    adapter.handle_message.assert_not_awaited()
    assert "implicit_session_switch" in caplog.text


@pytest.mark.asyncio
async def test_text_batch_flush_applies_only_flushed_normalized_event(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []

    async def fake_apply(event, _state_dir):
        calls.append(event)
        return _ok("feishu_inbound")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
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

    async def fake_apply(event, _state_dir):
        calls.append(event)
        return _ok("feishu_inbound")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
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

    async def fake_apply(event, _state_dir):
        calls.append(event)
        return _ok("feishu_inbound")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
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


@pytest.mark.asyncio
async def test_reaction_synthetic_inbound_id_uses_feishu_event_id_not_target_message(tmp_path):
    adapter = _reaction_adapter(tmp_path)
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="ev_reaction_1"),
        event=SimpleNamespace(
            message_id="om_bot_target",
            user_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
            reaction_type=SimpleNamespace(emoji_type="THUMBSUP"),
        ),
    )

    await adapter._handle_reaction_event("im.message.reaction.created_v1", data)

    synthetic_event = adapter._handle_message_with_guards.await_args.args[0]
    assert synthetic_event.message_id == "ev_reaction_1"
    assert synthetic_event.message_id != "om_bot_target"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        SimpleNamespace(
            event=SimpleNamespace(
                message_id="om_bot_target",
                user_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
                reaction_type=SimpleNamespace(emoji_type="THUMBSUP"),
            )
        ),
        SimpleNamespace(
            header=SimpleNamespace(event_id="ev_reaction_missing_target"),
            event=SimpleNamespace(
                message_id="",
                user_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
                reaction_type=SimpleNamespace(emoji_type="THUMBSUP"),
            ),
        ),
    ],
)
async def test_reaction_without_event_or_target_id_is_dropped(tmp_path, data):
    adapter = _reaction_adapter(tmp_path)

    await adapter._handle_reaction_event("im.message.reaction.created_v1", data)

    adapter._handle_message_with_guards.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_action_missing_token_is_fail_closed_before_message_guards(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "ou_user", "user_name": "User", "user_id_alt": None}
    )
    adapter.get_chat_info = AsyncMock(return_value={"name": "Feishu Chat", "type": "group", "reliable": True})
    adapter._handle_message_with_guards = AsyncMock()
    data = SimpleNamespace(
        event=SimpleNamespace(
            token="",
            context=SimpleNamespace(open_chat_id="oc_chat", thread_id=None, root_id=None),
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value={"custom_action": "x"}),
        )
    )

    await adapter._handle_card_action_event(data)

    adapter._handle_message_with_guards.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_event_applies_feishu_ack_with_stable_feishu_event_id(
    monkeypatch, tmp_path
):
    adapter = _adapter(tmp_path)
    calls = []

    async def fake_apply(event, state_dir):
        calls.append((event, state_dir))
        return _ok("feishu_ack")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )

    await adapter._handle_message_read_event(_read_event(message_id="om_sent", event_id="ev_read_1"))

    assert len(calls) == 1
    event, state_dir = calls[0]
    assert event["type"] == "feishu_ack"
    assert event["message_id"] == "om_sent"
    assert event["ack_event_id"] == "ev_read_1:om_sent"
    assert event["timestamp"] == pytest.approx(int(datetime.now().timestamp()), abs=2)
    assert state_dir == tmp_path


@pytest.mark.asyncio
async def test_read_event_applies_one_ack_per_sdk_message_id(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    calls = []

    async def fake_apply(event, state_dir):
        calls.append((event, state_dir))
        return _ok("feishu_ack")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )

    await adapter._handle_message_read_event(
        SimpleNamespace(
            header=SimpleNamespace(event_id="ev_read_batch"),
            event=SimpleNamespace(message_id_list=["om_1", "om_2"]),
        )
    )

    assert [call[0]["message_id"] for call in calls] == ["om_1", "om_2"]
    assert [call[0]["ack_event_id"] for call in calls] == [
        "ev_read_batch:om_1",
        "ev_read_batch:om_2",
    ]


def test_read_event_callback_schedules_async_apply_without_inline_subprocess(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter._loop = object()
    scheduled = []
    sync_apply = Mock()
    monkeypatch.setattr("gateway.hermes_tools_gateway_event.apply_gateway_event", sync_apply)

    def fake_submit(_loop, coro):
        scheduled.append(coro)
        coro.close()
        return True

    adapter._submit_on_loop = fake_submit

    adapter._on_message_read_event(_read_event(message_id="om_sent", event_id="ev_read_1"))

    assert len(scheduled) == 1
    sync_apply.assert_not_called()


@pytest.mark.parametrize("failure_class", ["unknown_message_id", "conflicting_ack_event"])
@pytest.mark.asyncio
async def test_read_event_adapter_failures_are_classified_not_swallowed(
    monkeypatch, tmp_path, caplog, failure_class
):
    adapter = _adapter(tmp_path)

    async def fake_apply(_event, _state_dir):
        return _failure("feishu_ack", failure_class)

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )

    await adapter._handle_message_read_event(_read_event(message_id="om_unknown", event_id="ev_read_2"))

    assert failure_class in caplog.text
    assert "om_unknown" not in caplog.text


@pytest.mark.asyncio
async def test_duplicate_read_event_success_is_recorded_without_failure(
    monkeypatch, tmp_path, caplog
):
    adapter = _adapter(tmp_path)

    async def fake_apply(_event, _state_dir):
        return _ok("feishu_ack")

    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        fake_apply,
    )

    await adapter._handle_message_read_event(_read_event(message_id="om_sent", event_id="ev_same"))

    assert "failed" not in caplog.text.lower()


@pytest.mark.parametrize(
    "data",
    [
        SimpleNamespace(header=SimpleNamespace(event_id="ev_read_missing"), event=SimpleNamespace(message=SimpleNamespace())),
        SimpleNamespace(header=SimpleNamespace(event_id="ev_read_empty"), event=SimpleNamespace(message_id_list=[])),
        SimpleNamespace(header=SimpleNamespace(event_id="ev_read_bad"), event=SimpleNamespace(message_id_list=[""])),
        SimpleNamespace(event=SimpleNamespace(message_id_list=["om_sent"])),
        SimpleNamespace(header=SimpleNamespace(), event=SimpleNamespace(message_id_list=["om_sent"])),
    ],
)
@pytest.mark.asyncio
async def test_malformed_read_event_without_reliable_ids_is_dropped(monkeypatch, tmp_path, data):
    adapter = _adapter(tmp_path)
    apply_mock = AsyncMock()
    monkeypatch.setattr(
        "gateway.platforms.feishu.gateway_event_ledger.apply_gateway_event_async",
        apply_mock,
    )

    await adapter._handle_message_read_event(data)

    apply_mock.assert_not_awaited()

"""Tests for Feishu interactive card approval buttons."""

import asyncio
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Feishu mock so FeishuAdapter can be imported without lark-oapi
# ---------------------------------------------------------------------------
def _ensure_feishu_mocks():
    """Provide stubs for lark-oapi / aiohttp.web so the import succeeds."""
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in (
            "lark_oapi", "lark_oapi.api.im.v1",
            "lark_oapi.event", "lark_oapi.event.callback_type",
        ):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)


_ensure_feishu_mocks()

from gateway.config import PlatformConfig
from gateway.feishu_legacy_guard import feishu_broker_context
import gateway.platforms.feishu as feishu_module
from gateway.platforms.feishu import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> FeishuAdapter:
    """Create a FeishuAdapter with mocked internals."""
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_audited_adapter(tmp_path) -> FeishuAdapter:
    """Create a FeishuAdapter with hermes-tools delivery auditing enabled."""
    config = PlatformConfig(enabled=True, extra={"hermes_tools_state_dir": str(tmp_path)})
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _install_event_recorder(adapter, *, fail_event_types=()):
    calls = []
    fail_event_types = set(fail_event_types)

    async def apply(event):
        calls.append(event)
        return event.get("type") not in fail_event_types

    adapter._apply_gateway_event = apply
    return calls


def _install_sync_event_recorder(monkeypatch):
    calls = []

    def apply(event, _state_dir):
        calls.append(event)
        return True

    monkeypatch.setattr(feishu_module.gateway_event_ledger, "apply_gateway_event", apply)
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


def _assert_action_denied(event, *, action):
    assert event["type"] == "feishu_action_denied"
    assert event["action"] == action
    assert event["failure_class"] == "feishu_action_requires_broker"
    assert event["action_hash"].startswith("fnv1a64:")
    assert event["correlation_id"]


def _assert_legacy_descriptor_denied(event, *, surface, failure_class):
    assert event["type"] == "feishu_legacy_descriptor_denied"
    assert event["surface"] == surface
    assert event["failure_class"] == failure_class
    assert event["descriptor_hash"].startswith("fnv1a64:")
    assert event["correlation_id"]


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


def _assert_interactive_create_contract(adapter, request, *, delivery_id, receive_id):
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == receive_id
    assert request.request_body.msg_type == "interactive"
    assert request.request_body.uuid == adapter._idempotency_key_for_delivery(delivery_id)
    assert json.loads(request.request_body.content)


def _assert_interactive_update_contract(request, *, message_id):
    assert request.message_id == message_id
    assert request.request_body.msg_type == "interactive"
    assert json.loads(request.request_body.content)


def _delivery_record_action(
    *,
    delivery_id,
    inbound_id,
    target,
    session_id,
    correlation_id,
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


class _FakeResponse:
    def __init__(self, *, ok=True, message_id="om_sent", code=0, msg="ok"):
        self.code = code
        self.msg = msg
        self.data = SimpleNamespace(message_id=message_id) if ok else None
        self._ok = ok

    def success(self):
        return self._ok


class _FakeMessageApi:
    def __init__(self, *, update_delay=0):
        self.create_calls = []
        self.create_response = _FakeResponse(message_id="om_created")
        self.update_calls = []
        self.update_response = _FakeResponse(message_id="om_updated")
        self.update_delay = update_delay

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response

    def update(self, request):
        if self.update_delay:
            time.sleep(self.update_delay)
        self.update_calls.append(request)
        return self.update_response


def _interactive_card_title_from_update_request(request) -> str:
    card = _interactive_card_from_update_request(request)
    return card["header"]["title"]["content"]


def _interactive_card_from_update_request(request) -> dict:
    return json.loads(request.request_body.content)


def _interactive_card_action_values(card: dict) -> list[dict]:
    values = []
    for element in card.get("elements", []):
        if not isinstance(element, dict) or element.get("tag") != "action":
            continue
        for action in element.get("actions", []):
            value = action.get("value") if isinstance(action, dict) else None
            if isinstance(value, dict):
                values.append(value)
    return values


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
    token: str = "tok_abc",
    thread_id: str | None = None,
    root_id: str | None = None,
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id, thread_id=thread_id, root_id=root_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                tag="button",
                value=action_value,
            ),
        ),
    )


def _close_submitted_coro(coro, _loop):
    """Close scheduled coroutines in sync-handler tests to avoid unawaited warnings."""
    coro.close()
    return SimpleNamespace(add_done_callback=lambda *_args, **_kwargs: None)


def _schedule_submitted_coro_on_current_loop(tasks):
    loop = asyncio.get_running_loop()

    def schedule(coro, _loop):
        task = loop.create_task(coro)
        tasks.append(task)
        return task

    return schedule


# ===========================================================================
# send_exec_approval — interactive card with buttons
# ===========================================================================

class TestFeishuExecApproval:
    """Test send_exec_approval sends an interactive card."""

    @pytest.mark.asyncio
    async def test_audited_success_writes_pending_sent_and_stores_approval_state(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter)

        with (
            patch.object(
                adapter,
                "_send_raw_message",
                new_callable=AsyncMock,
                return_value=_FakeResponse(message_id="om_approval"),
            ) as mock_send_raw,
            patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_legacy_send,
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
                metadata={
                    "inbound_id": "inbound-1",
                    "session_id": "session-a",
                    "correlation_id": "corr-a",
                },
            )

        assert result.success is True
        assert result.message_id == "om_approval"
        mock_send_raw.assert_awaited_once()
        mock_legacy_send.assert_not_awaited()
        assert _event_types(events) == ["delivery_pending", "delivery_sent"]
        assert events[0]["operation"] == "approval_prompt_card_create"
        assert events[0]["target"] == "feishu:chat:oc_12345"
        assert events[0]["inbound_id"] == "inbound-1"
        assert events[0]["session_id"] == "session-a"
        assert events[0]["correlation_id"] == "corr-a"
        assert events[1]["delivery_id"] == events[0]["delivery_id"]
        assert events[1]["operation"] == "approval_prompt_card_create"
        assert events[1]["message_id"] == "om_approval"
        assert events[0]["delivery_id"].startswith("approval_prompt_card_create-")
        assert len(events[0]["delivery_id"]) > 50

        kwargs = mock_send_raw.call_args.kwargs
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"
        assert kwargs["reply_to"] is None
        assert len(kwargs["uuid_value"]) <= 50
        assert kwargs["uuid_value"] != events[0]["delivery_id"]
        card = json.loads(kwargs["payload"])
        actions = card["elements"][1]["actions"]
        assert actions[0]["value"]["hermes_card_scope"]["chat_type"] == "group"

        assert len(adapter._approval_state) == 1
        state = next(iter(adapter._approval_state.values()))
        assert state["session_key"] == "agent:main:feishu:group:oc_12345"
        assert state["message_id"] == "om_approval"
        assert state["chat_id"] == "oc_12345"
        assert state["chat_type"] == "group"
        assert state["prompt_card"] == card

    @pytest.mark.asyncio
    async def test_approval_prompt_create_operation_matrix_uses_interactive_create_builder(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        message_api.create_response = _FakeResponse(message_id="om_approval_matrix")
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        timeline = []

        async def apply(event):
            timeline.append(("event", event))
            return True

        def create(request):
            message_api.create_calls.append(request)
            timeline.append(("sdk_create", request))
            return message_api.create_response

        adapter._apply_gateway_event = apply
        message_api.create = create

        result = await adapter.send_exec_approval(
            chat_id="oc_12345",
            command="echo matrix",
            session_key="agent:main:feishu:group:oc_12345",
            description="operation matrix",
            metadata={
                "inbound_id": "approval-prompt-1",
                "session_id": "session-approval",
                "correlation_id": "corr-approval",
            },
        )

        assert result.success is True
        assert result.message_id == "om_approval_matrix"
        assert [entry[0] for entry in timeline] == ["event", "sdk_create", "event"]
        events = [entry[1] for entry in timeline if entry[0] == "event"]
        delivery_id = events[0]["delivery_id"]
        assert delivery_id.startswith("approval_prompt_card_create-")
        _assert_sent_matrix_events(
            events,
            operation="approval_prompt_card_create",
            delivery_id=delivery_id,
            target="feishu:chat:oc_12345",
            inbound_id="approval-prompt-1",
            session_id="session-approval",
            correlation_id="corr-approval",
            message_id="om_approval_matrix",
        )
        assert len(message_api.create_calls) == 1
        request = message_api.create_calls[0]
        _assert_interactive_create_contract(
            adapter,
            request,
            delivery_id=delivery_id,
            receive_id="oc_12345",
        )
        card = json.loads(request.request_body.content)
        action_values = _interactive_card_action_values(card)
        assert delivery_id == adapter._delivery_id_for(
            "approval_prompt_card_create",
            metadata={
                "inbound_id": "approval-prompt-1",
                "session_id": "session-approval",
                "correlation_id": "corr-approval",
            },
            parts=[
                "oc_12345",
                "agent:main:feishu:group:oc_12345",
                str(action_values[0]["approval_id"]),
                request.request_body.content,
            ],
        )
        assert {value.get("hermes_action") for value in action_values} == {
            "approve_once",
            "approve_session",
            "approve_always",
            "deny",
        }
        assert {value.get("hermes_update_prompt_action") for value in action_values} == {None}
        state = next(iter(adapter._approval_state.values()))
        assert state["message_id"] == "om_approval_matrix"

    @pytest.mark.asyncio
    async def test_audited_pending_apply_failure_aborts_before_sdk_and_does_not_store_state(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter, fail_event_types={"delivery_pending"})

        with (
            patch.object(adapter, "_send_raw_message", new_callable=AsyncMock) as mock_send_raw,
            patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_legacy_send,
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
            )

        assert result.success is False
        assert result.error == "delivery_pending apply failed"
        assert _event_types(events) == ["delivery_pending"]
        mock_send_raw.assert_not_awaited()
        mock_legacy_send.assert_not_awaited()
        assert adapter._approval_state == {}

    @pytest.mark.asyncio
    async def test_audited_sent_apply_failure_returns_unknown_and_does_not_store_state(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter, fail_event_types={"delivery_sent"})

        with (
            patch.object(
                adapter,
                "_send_raw_message",
                new_callable=AsyncMock,
                return_value=_FakeResponse(message_id="om_unreconciled"),
            ) as mock_send_raw,
            patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_legacy_send,
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
            )

        assert result.success is False
        assert result.error == "delivery_sent apply failed"
        assert _event_types(events) == [
            "delivery_pending",
            "delivery_sent",
            "unknown_delivery_state",
        ]
        assert events[-1]["failure_class"] == "delivery_sent_apply_failed"
        assert events[-1]["message_id"] == "om_unreconciled"
        mock_send_raw.assert_awaited_once()
        mock_legacy_send.assert_not_awaited()
        assert adapter._approval_state == {}

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
            )

        assert result.success is True
        assert result.message_id == "msg_001"

        mock_send.assert_called_once()
        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"

        # Verify card payload contains the command and buttons
        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "rm -rf /important" in card["elements"][0]["content"]
        assert "dangerous deletion" in card["elements"][0]["content"]

        # Check buttons
        actions = card["elements"][1]["actions"]
        assert len(actions) == 4
        action_names = [a["value"]["hermes_action"] for a in actions]
        assert action_names == [
            "approve_once", "approve_session", "approve_always", "deny"
        ]

    @pytest.mark.asyncio
    async def test_stores_approval_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
            )

        assert len(adapter._approval_state) == 1
        approval_id = list(adapter._approval_state.keys())[0]
        state = adapter._approval_state[approval_id]
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_002"
        assert state["chat_id"] == "oc_12345"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._client = None
        result = await adapter.send_exec_approval(
            chat_id="oc_12345", command="ls", session_key="s"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_truncates_long_command(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_003"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            long_cmd = "x" * 5000
            await adapter.send_exec_approval(
                chat_id="oc_12345", command=long_cmd, session_key="s"
            )

        card = json.loads(mock_send.call_args[1]["payload"])
        content = card["elements"][0]["content"]
        assert "..." in content
        assert len(content) < 5000

    @pytest.mark.asyncio
    async def test_multiple_approvals_get_unique_ids(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_x"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_1", command="cmd1", session_key="s1"
            )
            await adapter.send_exec_approval(
                chat_id="oc_2", command="cmd2", session_key="s2"
            )

        assert len(adapter._approval_state) == 2
        ids = list(adapter._approval_state.keys())
        assert ids[0] != ids[1]


# ===========================================================================
# send_update_prompt — interactive card with buttons
# ===========================================================================

class TestFeishuUpdatePrompt:
    """Test send_update_prompt sends an interactive card."""

    @pytest.mark.asyncio
    async def test_audited_success_writes_pending_sent_and_stores_prompt_state(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter)

        with (
            patch.object(
                adapter,
                "_send_raw_message",
                new_callable=AsyncMock,
                return_value=_FakeResponse(message_id="om_update_prompt"),
            ) as mock_send_raw,
            patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_legacy_send,
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={
                    "thread_id": "th_1",
                    "inbound_id": "inbound-2",
                    "session_id": "session-b",
                    "correlation_id": "corr-b",
                },
            )

        assert result.success is True
        assert result.message_id == "om_update_prompt"
        mock_send_raw.assert_awaited_once()
        mock_legacy_send.assert_not_awaited()
        assert _event_types(events) == ["delivery_pending", "delivery_sent"]
        assert events[0]["operation"] == "update_prompt_card_create"
        assert events[0]["target"] == "feishu:chat:oc_12345"
        assert events[0]["inbound_id"] == "inbound-2"
        assert events[0]["session_id"] == "session-b"
        assert events[0]["correlation_id"] == "corr-b"
        assert events[1]["operation"] == "update_prompt_card_create"
        assert events[1]["message_id"] == "om_update_prompt"

        kwargs = mock_send_raw.call_args.kwargs
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"
        assert kwargs["metadata"]["thread_id"] == "th_1"
        assert len(kwargs["uuid_value"]) <= 50
        card = json.loads(kwargs["payload"])
        actions = card["elements"][1]["actions"]
        assert actions[0]["value"]["hermes_card_scope"]["thread_id"] == "th_1"

        assert len(adapter._update_prompt_state) == 1
        state = next(iter(adapter._update_prompt_state.values()))
        assert state["session_key"] == "agent:main:feishu:group:oc_12345"
        assert state["message_id"] == "om_update_prompt"
        assert state["chat_id"] == "oc_12345"
        assert state["thread_id"] == "th_1"
        assert state["prompt_card"] == card

    @pytest.mark.asyncio
    async def test_update_prompt_create_operation_matrix_is_distinct_from_approval_create(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        message_api.create_response = _FakeResponse(message_id="om_update_matrix")
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        timeline = []

        async def apply(event):
            timeline.append(("event", event))
            return True

        def create(request):
            message_api.create_calls.append(request)
            timeline.append(("sdk_create", request))
            return message_api.create_response

        adapter._apply_gateway_event = apply
        message_api.create = create

        result = await adapter.send_update_prompt(
            chat_id="oc_12345",
            prompt="Continue update?",
            default="y",
            session_key="agent:main:feishu:group:oc_12345",
            metadata={
                "thread_id": "th_update",
                "inbound_id": "update-prompt-1",
                "session_id": "session-update",
                "correlation_id": "corr-update",
            },
        )

        assert result.success is True
        assert result.message_id == "om_update_matrix"
        assert [entry[0] for entry in timeline] == ["event", "sdk_create", "event"]
        events = [entry[1] for entry in timeline if entry[0] == "event"]
        delivery_id = events[0]["delivery_id"]
        assert delivery_id.startswith("update_prompt_card_create-")
        _assert_sent_matrix_events(
            events,
            operation="update_prompt_card_create",
            delivery_id=delivery_id,
            target="feishu:chat:oc_12345",
            inbound_id="update-prompt-1",
            session_id="session-update",
            correlation_id="corr-update",
            message_id="om_update_matrix",
        )
        assert len(message_api.create_calls) == 1
        request = message_api.create_calls[0]
        _assert_interactive_create_contract(
            adapter,
            request,
            delivery_id=delivery_id,
            receive_id="oc_12345",
        )
        card = json.loads(request.request_body.content)
        action_values = _interactive_card_action_values(card)
        assert delivery_id == adapter._delivery_id_for(
            "update_prompt_card_create",
            metadata={
                "thread_id": "th_update",
                "inbound_id": "update-prompt-1",
                "session_id": "session-update",
                "correlation_id": "corr-update",
            },
            parts=[
                "oc_12345",
                "agent:main:feishu:group:oc_12345",
                str(action_values[0]["update_prompt_id"]),
                request.request_body.content,
            ],
        )
        assert {value.get("hermes_update_prompt_action") for value in action_values} == {"y", "n"}
        assert {value.get("hermes_action") for value in action_values} == {None}
        state = next(iter(adapter._update_prompt_state.values()))
        assert state["message_id"] == "om_update_matrix"
        assert state["thread_id"] == "th_update"

    @pytest.mark.asyncio
    async def test_audited_pending_apply_failure_aborts_before_sdk_and_does_not_store_prompt_state(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter, fail_event_types={"delivery_pending"})

        with (
            patch.object(adapter, "_send_raw_message", new_callable=AsyncMock) as mock_send_raw,
            patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_legacy_send,
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Continue update?",
                session_key="my-session-key",
            )

        assert result.success is False
        assert result.error == "delivery_pending apply failed"
        assert _event_types(events) == ["delivery_pending"]
        mock_send_raw.assert_not_awaited()
        mock_legacy_send.assert_not_awaited()
        assert adapter._update_prompt_state == {}

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_up_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={"thread_id": "th_1"},
            )

        assert result.success is True
        assert result.message_id == "msg_up_001"

        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"
        assert kwargs["metadata"] == {"thread_id": "th_1"}

        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "Restore stashed changes after update?" in card["elements"][0]["content"]
        assert "Default: `y`" in card["elements"][0]["content"]
        actions = card["elements"][1]["actions"]
        assert [a["value"]["hermes_update_prompt_action"] for a in actions] == ["y", "n"]

    @pytest.mark.asyncio
    async def test_stores_prompt_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_up_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Continue update?",
                session_key="my-session-key",
            )

        assert len(adapter._update_prompt_state) == 1
        prompt_id = list(adapter._update_prompt_state.keys())[0]
        state = adapter._update_prompt_state[prompt_id]
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_up_002"
        assert state["chat_id"] == "oc_12345"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._client = None
        result = await adapter.send_update_prompt(
            chat_id="oc_12345",
            prompt="Continue update?",
            session_key="s",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_send_failure_returns_error(self):
        adapter = _make_adapter()
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Continue update?",
                session_key="s",
            )

        assert result.success is False
        assert "timed out" in (result.error or "")


class TestFeishuIdempotencyKeys:
    def test_long_delivery_id_maps_to_stable_bounded_uuid(self):
        delivery_id = "approval_prompt_card_create-" + ("a" * 24)

        first = FeishuAdapter._idempotency_key_for_delivery(delivery_id)
        second = FeishuAdapter._idempotency_key_for_delivery(delivery_id)

        assert first == second
        assert first != delivery_id
        assert len(first) <= 50
        assert re.fullmatch(r"[A-Za-z0-9_.:-]+", first)

    def test_short_safe_delivery_id_remains_unchanged(self):
        delivery_id = "update_prompt_card_create-" + ("b" * 24)

        key = FeishuAdapter._idempotency_key_for_delivery(delivery_id)

        assert key == delivery_id
        assert len(key) == 50


# ===========================================================================
# _resolve_approval — approval state pop + gateway resolution
# ===========================================================================

class TestResolveApproval:
    """Test _resolve_approval pops state and calls resolve_gateway_approval."""

    @pytest.mark.asyncio
    async def test_audited_prompt_card_update_replay_success_skips_sdk(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        events = []

        async def apply(event):
            events.append(event)
            if event.get("type") == "delivery_pending":
                return SimpleNamespace(
                    ok=True,
                    action=_delivery_record_action(
                        delivery_id=event["delivery_id"],
                        inbound_id=event["inbound_id"],
                        target=event["target"],
                        session_id=event["session_id"],
                        correlation_id=event["correlation_id"],
                        status="sent",
                        feishu_message_id="om_approval_20",
                    ),
                )
            return True

        adapter._apply_gateway_event = apply
        updated = await adapter._audited_prompt_card_update(
            operation="approval_prompt_card_update",
            state={
                "session_key": "agent:main:feishu:group:oc_12345",
                "message_id": "om_approval_20",
                "inbound_id": "inbound-approval",
                "session_id": "session-approval",
                "correlation_id": "corr-approval",
            },
            prompt_id=20,
            choice="once",
            card={"elements": []},
        )

        assert updated is True
        assert _event_types(events) == ["delivery_pending"]
        assert message_api.update_calls == []

    @pytest.mark.asyncio
    async def test_audited_prompt_card_update_replay_failure_returns_false(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        events = []

        async def apply(event):
            events.append(event)
            if event.get("type") == "delivery_pending":
                return SimpleNamespace(
                    ok=True,
                    action=_delivery_record_action(
                        delivery_id=event["delivery_id"],
                        inbound_id=event["inbound_id"],
                        target=event["target"],
                        session_id=event["session_id"],
                        correlation_id=event["correlation_id"],
                        status="unknown",
                        feishu_message_id="om_unknown_update",
                    ),
                )
            return True

        adapter._apply_gateway_event = apply
        updated = await adapter._audited_prompt_card_update(
            operation="approval_prompt_card_update",
            state={
                "session_key": "agent:main:feishu:group:oc_12345",
                "message_id": "om_approval_21",
                "inbound_id": "inbound-approval",
                "session_id": "session-approval",
                "correlation_id": "corr-approval",
            },
            prompt_id=21,
            choice="once",
            card={"elements": []},
        )

        assert updated is False
        assert _event_types(events) == ["delivery_pending"]
        assert message_api.update_calls == []

    @pytest.mark.asyncio
    async def test_audited_prompt_card_update_replay_message_id_mismatch_fails_closed(
        self,
        tmp_path,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        events = []

        async def apply(event):
            events.append(event)
            if event.get("type") == "delivery_pending":
                return SimpleNamespace(
                    ok=True,
                    action=_delivery_record_action(
                        delivery_id=event["delivery_id"],
                        inbound_id=event["inbound_id"],
                        target=event["target"],
                        session_id=event["session_id"],
                        correlation_id=event["correlation_id"],
                        status="sent",
                        feishu_message_id="om_other_message",
                    ),
                )
            return True

        adapter._apply_gateway_event = apply
        updated = await adapter._audited_prompt_card_update(
            operation="approval_prompt_card_update",
            state={
                "session_key": "agent:main:feishu:group:oc_12345",
                "message_id": "om_approval_22",
                "inbound_id": "inbound-approval",
                "session_id": "session-approval",
                "correlation_id": "corr-approval",
            },
            prompt_id=22,
            choice="once",
            card={"elements": []},
        )

        assert updated is False
        assert _event_types(events) == ["delivery_pending"]
        assert message_api.update_calls == []

    @pytest.mark.asyncio
    async def test_approval_audited_resolution_patches_card_before_resolving_gateway_approval(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        timeline = []

        async def apply(event):
            timeline.append((event["type"], event))
            return True

        adapter._apply_gateway_event = apply
        adapter._approval_state[10] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_approval_10",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-approval",
            "session_id": "session-approval",
            "correlation_id": "corr-approval",
        }

        def resolve(session_key, choice):
            timeline.append(("resolver", (session_key, choice)))
            return 1

        with patch("tools.approval.resolve_gateway_approval", side_effect=resolve) as mock_resolve:
            await adapter._resolve_approval(
                10,
                "once",
                "Alice",
                open_id="ou_user1",
                chat_id="oc_12345",
            )

        assert [entry[0] for entry in timeline] == [
            "delivery_pending",
            "delivery_sent",
            "resolver",
        ]
        assert timeline[0][1]["operation"] == "approval_prompt_card_update"
        assert timeline[0][1]["target"] == "feishu:message:om_approval_10"
        assert timeline[1][1]["operation"] == "approval_prompt_card_update"
        assert timeline[1][1]["message_id"] == "om_approval_10"
        assert message_api.update_calls
        request = message_api.update_calls[0]
        assert request.message_id == "om_approval_10"
        assert request.request_body.msg_type == "interactive"
        mock_resolve.assert_called_once_with("agent:main:feishu:group:oc_12345", "once")
        assert 10 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_approval_prompt_update_operation_matrix_uses_existing_message_evidence(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
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
        state = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_approval_matrix",
            "inbound_id": "approval-prompt-1",
            "session_id": "session-approval",
            "correlation_id": "corr-approval",
        }

        updated = await adapter._audited_prompt_card_update(
            operation="approval_prompt_card_update",
            state=state,
            prompt_id=10,
            choice="once:attempt:1",
            card={"elements": [], "header": {"title": {"content": "Approved"}}},
        )

        expected_delivery_id = adapter._delivery_id_for(
            "approval_prompt_card_update",
            metadata=None,
            parts=[
                "om_approval_matrix",
                "agent:main:feishu:group:oc_12345",
                "10",
                "once:attempt:1",
            ],
        )
        assert updated is True
        assert [entry[0] for entry in timeline] == ["event", "sdk_update", "event"]
        events = [entry[1] for entry in timeline if entry[0] == "event"]
        _assert_sent_matrix_events(
            events,
            operation="approval_prompt_card_update",
            delivery_id=expected_delivery_id,
            target="feishu:message:om_approval_matrix",
            inbound_id="approval-prompt-1",
            session_id="session-approval",
            correlation_id="corr-approval",
            message_id="om_approval_matrix",
        )
        assert len(message_api.update_calls) == 1
        request = message_api.update_calls[0]
        _assert_interactive_update_contract(request, message_id="om_approval_matrix")
        card = json.loads(request.request_body.content)
        assert card["header"]["title"]["content"] == "Approved"
        assert _interactive_card_action_values(card) == []

    @pytest.mark.asyncio
    async def test_approval_audited_resolution_keeps_state_and_releases_claim_when_side_effect_fails(
        self,
        tmp_path,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        events = _install_event_recorder(adapter)
        with patch.object(
            adapter,
            "_send_raw_message",
            new_callable=AsyncMock,
            return_value=_FakeResponse(message_id="om_approval_12"),
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
                metadata={
                    "inbound_id": "inbound-approval",
                    "session_id": "session-approval",
                    "correlation_id": "corr-approval",
                },
            )
        assert result.success is True
        approval_id = next(iter(adapter._approval_state))
        events.clear()
        resolved = []

        def resolve(session_key, choice):
            resolved.append((session_key, choice))
            if len(resolved) == 1:
                raise RuntimeError("approval store unavailable")
            return 1

        with patch("tools.approval.resolve_gateway_approval", side_effect=resolve):
            await adapter._resolve_approval(
                approval_id,
                "once",
                "Alice",
                open_id="ou_user1",
                chat_id="oc_12345",
            )

            assert approval_id in adapter._approval_state
            assert "resolution_claim" not in adapter._approval_state[approval_id]
            assert len(message_api.update_calls) == 2
            retry_card = _interactive_card_from_update_request(message_api.update_calls[1])
            retry_values = _interactive_card_action_values(retry_card)
            assert {value.get("hermes_action") for value in retry_values} == {
                "approve_once",
                "approve_session",
                "approve_always",
                "deny",
            }
            assert {value.get("approval_id") for value in retry_values} == {approval_id}

            await adapter._resolve_approval(
                approval_id,
                "once",
                "Alice",
                open_id="ou_user1",
                chat_id="oc_12345",
            )

        assert resolved == [
            ("agent:main:feishu:group:oc_12345", "once"),
            ("agent:main:feishu:group:oc_12345", "once"),
        ]
        assert len(message_api.update_calls) == 3
        sent_ids = [event["delivery_id"] for event in events if event["type"] == "delivery_sent"]
        assert len(sent_ids) == 3
        assert len(set(sent_ids)) == 3
        assert approval_id not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_approval_audited_conflicting_concurrent_resolutions_single_card_update_matches_side_effect(
        self,
        tmp_path,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi(update_delay=0.05)
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        events = _install_event_recorder(adapter)
        adapter._approval_state[11] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_approval_11",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-approval",
            "session_id": "session-approval",
            "correlation_id": "corr-approval",
        }
        resolved_choices = []

        def resolve(_session_key, choice):
            resolved_choices.append(choice)
            return 1

        with patch("tools.approval.resolve_gateway_approval", side_effect=resolve):
            await asyncio.gather(
                adapter._resolve_approval(
                    11,
                    "once",
                    "Alice",
                    open_id="ou_user1",
                    chat_id="oc_12345",
                ),
                adapter._resolve_approval(
                    11,
                    "deny",
                    "Bob",
                    open_id="ou_user1",
                    chat_id="oc_12345",
                ),
            )

        assert len(message_api.update_calls) == 1
        assert [event["operation"] for event in events if event["type"] == "delivery_sent"] == [
            "approval_prompt_card_update"
        ]
        assert len(resolved_choices) == 1
        title = _interactive_card_title_from_update_request(message_api.update_calls[0])
        expected_title = "Denied" if resolved_choices[0] == "deny" else "Approved once"
        assert expected_title in title
        assert 11 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_resolves_once(self):
        adapter = _make_adapter()
        adapter._approval_state[1] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_001",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(1, "once", "Norbert", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("agent:main:feishu:group:oc_12345", "once")
        assert 1 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_resolves_deny(self):
        adapter = _make_adapter()
        adapter._approval_state[2] = {
            "session_key": "some-session",
            "message_id": "msg_002",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(2, "deny", "Alice", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("some-session", "deny")

    @pytest.mark.asyncio
    async def test_resolves_session(self):
        adapter = _make_adapter()
        adapter._approval_state[3] = {
            "session_key": "sess-3",
            "message_id": "msg_003",
            "chat_id": "oc_99",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(3, "session", "Bob", open_id="ou_user1", chat_id="oc_99")

        mock_resolve.assert_called_once_with("sess-3", "session")

    @pytest.mark.asyncio
    async def test_resolves_always(self):
        adapter = _make_adapter()
        adapter._approval_state[4] = {
            "session_key": "sess-4",
            "message_id": "msg_004",
            "chat_id": "oc_55",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(4, "always", "Carol", open_id="ou_user1", chat_id="oc_55")

        mock_resolve.assert_called_once_with("sess-4", "always")

    @pytest.mark.asyncio
    async def test_already_resolved_drops_silently(self):
        adapter = _make_adapter()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(99, "once", "Nobody", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_click_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_admin"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg_005",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(5, "once", "Mallory", open_id="ou_intruder", chat_id="oc_12345")

        mock_resolve.assert_not_called()
        assert 5 in adapter._approval_state

    @pytest.mark.asyncio
    async def test_chat_mismatch_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._approval_state[6] = {
            "session_key": "sess-6",
            "message_id": "msg_006",
            "chat_id": "oc_expected",
        }

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(6, "session", "Norbert", open_id="ou_user1", chat_id="oc_wrong")

        mock_resolve.assert_not_called()
        assert 6 in adapter._approval_state

# ===========================================================================
# _handle_card_action_event — non-approval card actions
# ===========================================================================

class TestNonApprovalCardAction:
    """Non-approval card actions should still route as synthetic commands."""

    @pytest.mark.asyncio
    async def test_generic_card_requires_broker_before_synthetic_command(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_event_recorder(adapter)

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_requires_broker",
        )

        with (
            patch.object(
                adapter,
                "_resolve_sender_profile",
                new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ) as mock_profile,
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Test Chat", "type": "group", "reliable": True},
            ) as mock_chat,
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)
            await adapter._handle_card_action_event(data)

        assert _event_types(events) == ["feishu_legacy_descriptor_denied"]
        _assert_legacy_descriptor_denied(
            events[0],
            surface="feishu.card_action",
            failure_class="feishu_legacy_card_action_requires_broker",
        )
        mock_profile.assert_not_awaited()
        mock_chat.assert_not_awaited()
        mock_handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generic_card_with_broker_context_routes_as_synthetic_command(self, tmp_path):
        adapter = _make_audited_adapter(tmp_path)

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_brokered_card",
        )

        with (
            _broker_context(),
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Test Chat", "type": "group", "reliable": True},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        mock_handle.assert_called_once()
        event = mock_handle.call_args[0][0]
        assert event.text.startswith("/card button")

    @pytest.mark.asyncio
    async def test_routes_as_synthetic_command(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_normal",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Test Chat", "type": "group", "reliable": True},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        mock_handle.assert_called_once()
        event = mock_handle.call_args[0][0]
        assert "/card button" in event.text

    @pytest.mark.asyncio
    async def test_dm_card_action_routes_to_dm_source(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            chat_id="oc_dm",
            token="tok_dm_card",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Dave", "type": "dm"},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        event = mock_handle.call_args[0][0]
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "oc_dm"

    @pytest.mark.asyncio
    async def test_group_card_action_uses_saved_scope_when_chat_lookup_falls_back_to_dm(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={
                "custom_action": "something_else",
                "hermes_card_scope": {
                    "chat_type": "group",
                    "thread_id": "omt_topic",
                },
            },
            token="tok_group_card",
            thread_id="omt_topic",
            root_id="om_root",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"chat_id": "oc_12345", "name": "oc_12345", "type": "dm", "reliable": False},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        event = mock_handle.call_args[0][0]
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "oc_12345"
        assert event.source.thread_id == "omt_topic"
        assert event.reply_to_message_id == "om_root"

    @pytest.mark.asyncio
    async def test_card_action_uses_saved_state_scope_when_action_value_has_no_scope(self):
        adapter = _make_adapter()
        adapter._update_prompt_state[7] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_7",
            "chat_id": "oc_12345",
            "chat_type": "group",
            "thread_id": "omt_topic",
        }

        data = _make_card_action_data(
            action_value={
                "custom_action": "something_else",
                "update_prompt_id": 7,
            },
            token="fake_redacted_credential",
            thread_id="omt_topic",
            root_id="om_root",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"chat_id": "oc_12345", "name": "oc_12345", "type": "dm", "reliable": False},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        event = mock_handle.call_args[0][0]
        assert event.source.chat_type == "group"
        assert event.source.thread_id == "omt_topic"

    @pytest.mark.asyncio
    async def test_card_action_without_reliable_scope_is_dropped_when_lookup_falls_back_to_dm(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_ambiguous_card",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"chat_id": "oc_12345", "name": "oc_12345", "type": "dm", "reliable": False},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_preserves_context_thread_id(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_thread",
            thread_id="omt_topic",
            root_id="om_root",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Test Chat", "type": "group", "reliable": True},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        event = mock_handle.call_args[0][0]
        assert event.source.thread_id == "omt_topic"
        assert event.reply_to_message_id == "om_root"

    @pytest.mark.asyncio
    async def test_context_root_id_is_not_used_as_thread_id(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_root_only",
            root_id="om_root",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(
                adapter,
                "get_chat_info",
                new_callable=AsyncMock,
                return_value={"name": "Test Chat", "type": "group", "reliable": True},
            ),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        event = mock_handle.call_args[0][0]
        assert event.source.thread_id is None
        assert event.reply_to_message_id == "om_root"


# ===========================================================================
# _on_card_action_trigger — inline card response for approval actions
# ===========================================================================

class _FakeCallBackCard:
    def __init__(self):
        self.type = None
        self.data = None


class _FakeP2Response:
    def __init__(self):
        self.card = None


@pytest.fixture(autouse=False)
def _patch_callback_card_types(monkeypatch):
    """Provide real-ish P2CardActionTriggerResponse / CallBackCard for tests."""
    monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", _FakeP2Response)
    monkeypatch.setattr(feishu_module, "CallBackCard", _FakeCallBackCard)


class TestCardActionCallbackResponse:
    """Test that _on_card_action_trigger returns updated card inline."""

    def test_drops_action_when_loop_not_ready(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = None
        data = _make_card_action_data({"hermes_action": "approve_once", "approval_id": 1})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_returns_card_for_approve_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._approval_state[1] = {
            "session_key": "sess-1",
            "message_id": "msg-1",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 1},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "Approved once" in card["header"]["title"]["content"]
        assert "Bob" in card["elements"][0]["content"]

    def test_approval_requires_broker_before_scheduling_resolution(
        self,
        tmp_path,
        monkeypatch,
        _patch_callback_card_types,
    ):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_sync_event_recorder(monkeypatch)
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_user1"}
        adapter._approval_state[1] = {
            "session_key": "sess-1",
            "message_id": "om_approval_1",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-approval-1",
            "session_id": "session-approval-1",
            "correlation_id": "corr-approval-1",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 1},
            open_id="ou_user1",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert _event_types(events) == ["feishu_action_denied"]
        _assert_action_denied(events[0], action="approval_prompt_card_action")

    @pytest.mark.asyncio
    async def test_approval_audited_click_restores_actionable_card_when_side_effect_fails(
        self,
        tmp_path,
        _patch_callback_card_types,
    ):
        adapter = _make_audited_adapter(tmp_path)
        adapter._loop = asyncio.get_running_loop()
        adapter._allowed_group_users = {"ou_user1"}
        events = _install_event_recorder(adapter)
        with patch.object(
            adapter,
            "_send_raw_message",
            new_callable=AsyncMock,
            return_value=_FakeResponse(message_id="om_approval_handler"),
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
                metadata={
                    "inbound_id": "inbound-approval-handler",
                    "session_id": "session-approval-handler",
                    "correlation_id": "corr-approval-handler",
                },
            )
        assert result.success is True
        approval_id = next(iter(adapter._approval_state))
        events.clear()

        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        adapter._sender_name_cache["ou_user1"] = ("Alice", 9999999999)
        tasks = []
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            open_id="ou_user1",
        )

        with (
            _broker_context(),
            patch("asyncio.run_coroutine_threadsafe", side_effect=_schedule_submitted_coro_on_current_loop(tasks)),
            patch("tools.approval.resolve_gateway_approval", side_effect=RuntimeError("approval store unavailable")),
        ):
            response = adapter._on_card_action_trigger(data)
            assert response is not None
            assert response.card is None
            assert len(tasks) == 1
            await asyncio.gather(*tasks)

        assert approval_id in adapter._approval_state
        assert "resolution_claim" not in adapter._approval_state[approval_id]
        assert len(message_api.update_calls) == 2
        retry_card = _interactive_card_from_update_request(message_api.update_calls[1])
        retry_values = _interactive_card_action_values(retry_card)
        assert {value.get("hermes_action") for value in retry_values} == {
            "approve_once",
            "approve_session",
            "approve_always",
            "deny",
        }
        assert {value.get("approval_id") for value in retry_values} == {approval_id}
        assert [event["operation"] for event in events if event["type"] == "delivery_sent"] == [
            "approval_prompt_card_update",
            "approval_prompt_card_update",
        ]

    def test_returns_card_for_deny_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_user1"}
        adapter._approval_state[2] = {
            "session_key": "sess-2",
            "message_id": "msg-2",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "deny", "approval_id": 2},
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "red"
        assert "Denied" in card["header"]["title"]["content"]

    def test_ignores_missing_approval_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"hermes_action": "approve_once"})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_no_card_for_non_approval_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"some_other": "value"})

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None

    def test_falls_back_to_open_id_when_name_not_cached(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_unknown"}
        adapter._approval_state[3] = {
            "session_key": "sess-3",
            "message_id": "msg-3",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_session", "approval_id": 3},
            open_id="ou_unknown",
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert "ou_unknown" in card["elements"][0]["content"]

    def test_ignores_expired_cached_name(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_expired"}
        adapter._approval_state[4] = {
            "session_key": "sess-4",
            "message_id": "msg-4",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 4},
            open_id="ou_expired",
        )
        adapter._sender_name_cache["ou_expired"] = ("Old Name", 1)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert "Old Name" not in card["elements"][0]["content"]
        assert "ou_expired" in card["elements"][0]["content"]

    def test_rejects_approval_click_from_unauthorized_user(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_allowed"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg-5",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 5},
            open_id="ou_attacker",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_rejects_approval_click_when_callback_chat_mismatches(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._approval_state[6] = {
            "session_key": "sess-6",
            "message_id": "msg-6",
            "chat_id": "oc_expected",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 6},
            chat_id="oc_mismatch",
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_returns_card_for_update_prompt_yes(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_003",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "answered: Yes" in card["header"]["title"]["content"]
        assert "Bob" in card["elements"][0]["content"]

    def test_update_prompt_audited_click_returns_no_inline_card_and_schedules_resolution(
        self,
        tmp_path,
        _patch_callback_card_types,
    ):
        adapter = _make_audited_adapter(tmp_path)
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[8] = {
            "session_key": "sess-up-8",
            "message_id": "om_update_8",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-up-8",
            "session_id": "session-up-8",
            "correlation_id": "corr-up-8",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 8},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with (
            _broker_context(),
            patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro) as mock_submit,
        ):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_called_once()

    def test_update_prompt_requires_broker_before_scheduling_resolution(
        self,
        tmp_path,
        monkeypatch,
        _patch_callback_card_types,
    ):
        adapter = _make_audited_adapter(tmp_path)
        events = _install_sync_event_recorder(monkeypatch)
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[8] = {
            "session_key": "sess-up-8",
            "message_id": "om_update_8",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-up-8",
            "session_id": "session-up-8",
            "correlation_id": "corr-up-8",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 8},
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert _event_types(events) == ["feishu_action_denied"]
        _assert_action_denied(events[0], action="update_prompt_card_action")

    @pytest.mark.asyncio
    async def test_update_prompt_audited_click_restores_actionable_card_when_side_effect_fails(
        self,
        tmp_path,
        monkeypatch,
        _patch_callback_card_types,
    ):
        adapter = _make_audited_adapter(tmp_path)
        adapter._loop = asyncio.get_running_loop()
        events = _install_event_recorder(adapter)
        with patch.object(
            adapter,
            "_send_raw_message",
            new_callable=AsyncMock,
            return_value=_FakeResponse(message_id="om_update_handler"),
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={
                    "inbound_id": "inbound-update-handler",
                    "session_id": "session-update-handler",
                    "correlation_id": "corr-update-handler",
                },
            )
        assert result.success is True
        prompt_id = next(iter(adapter._update_prompt_state))
        events.clear()

        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        monkeypatch.setattr(
            adapter,
            "_write_update_prompt_response",
            lambda _answer: (_ for _ in ()).throw(RuntimeError("response store unavailable")),
        )
        tasks = []
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": prompt_id},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with (
            _broker_context(),
            patch("asyncio.run_coroutine_threadsafe", side_effect=_schedule_submitted_coro_on_current_loop(tasks)),
        ):
            response = adapter._on_card_action_trigger(data)
            assert response is not None
            assert response.card is None
            assert len(tasks) == 1
            await asyncio.gather(*tasks)

        assert prompt_id in adapter._update_prompt_state
        assert "resolution_claim" not in adapter._update_prompt_state[prompt_id]
        assert len(message_api.update_calls) == 2
        retry_card = _interactive_card_from_update_request(message_api.update_calls[1])
        retry_values = _interactive_card_action_values(retry_card)
        assert {value.get("hermes_update_prompt_action") for value in retry_values} == {"y", "n"}
        assert {value.get("update_prompt_id") for value in retry_values} == {prompt_id}
        assert [event["operation"] for event in events if event["type"] == "delivery_sent"] == [
            "update_prompt_card_update",
            "update_prompt_card_update",
        ]

    def test_returns_card_for_update_prompt_no(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[2] = {
            "session_key": "sess-up-2",
            "message_id": "msg_up_004",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "n", "update_prompt_id": 2},
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "red"
        assert "answered: No" in card["header"]["title"]["content"]

    def test_ignores_missing_update_prompt_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"hermes_update_prompt_action": "y"})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_already_resolved_update_prompt_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 99},
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_update_prompt_schedule_failure_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_005",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=RuntimeError("loop closed")):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None

    def test_update_prompt_unauthorized_operator_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_006",
            "chat_id": "oc_12345",
        }
        adapter._allowed_group_users = {"ou_allowed"}
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
            open_id="ou_intruder",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()


class TestResolveUpdatePrompt:
    """Test update prompt resolution persists the response file."""

    @pytest.mark.asyncio
    async def test_update_prompt_audited_resolution_patches_card_before_writing_response(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        timeline = []

        async def apply(event):
            timeline.append((event["type"], event))
            return True

        adapter._apply_gateway_event = apply
        original_write_response = adapter._write_update_prompt_response

        def write_response(answer):
            timeline.append(("write_response", answer))
            original_write_response(answer)

        monkeypatch.setattr(adapter, "_write_update_prompt_response", write_response)
        adapter._update_prompt_state[9] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_update_9",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-update",
            "session_id": "session-update",
            "correlation_id": "corr-update",
        }

        await adapter._resolve_update_prompt(9, "y", "Alice")

        assert [entry[0] for entry in timeline] == [
            "delivery_pending",
            "delivery_sent",
            "write_response",
        ]
        assert timeline[0][1]["operation"] == "update_prompt_card_update"
        assert timeline[0][1]["target"] == "feishu:message:om_update_9"
        assert timeline[0][1]["inbound_id"] == "inbound-update"
        assert timeline[0][1]["session_id"] == "session-update"
        assert timeline[0][1]["correlation_id"] == "corr-update"
        assert timeline[1][1]["delivery_id"] == timeline[0][1]["delivery_id"]
        assert timeline[1][1]["message_id"] == "om_update_9"
        assert message_api.update_calls
        request = message_api.update_calls[0]
        assert request.message_id == "om_update_9"
        assert request.request_body.msg_type == "interactive"
        assert (hermes_home / ".update_response").read_text() == "y"
        assert 9 not in adapter._update_prompt_state

    @pytest.mark.asyncio
    async def test_update_prompt_update_operation_matrix_is_distinct_from_approval_update(
        self,
        tmp_path,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
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
        state = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_update_matrix",
            "inbound_id": "update-prompt-1",
            "session_id": "session-update",
            "correlation_id": "corr-update",
        }

        updated = await adapter._audited_prompt_card_update(
            operation="update_prompt_card_update",
            state=state,
            prompt_id=9,
            choice="y:attempt:1",
            card=adapter._build_resolved_update_prompt_card(answer="y", user_name="Alice"),
        )

        expected_delivery_id = adapter._delivery_id_for(
            "update_prompt_card_update",
            metadata=None,
            parts=[
                "om_update_matrix",
                "agent:main:feishu:group:oc_12345",
                "9",
                "y:attempt:1",
            ],
        )
        assert updated is True
        assert [entry[0] for entry in timeline] == ["event", "sdk_update", "event"]
        events = [entry[1] for entry in timeline if entry[0] == "event"]
        _assert_sent_matrix_events(
            events,
            operation="update_prompt_card_update",
            delivery_id=expected_delivery_id,
            target="feishu:message:om_update_matrix",
            inbound_id="update-prompt-1",
            session_id="session-update",
            correlation_id="corr-update",
            message_id="om_update_matrix",
        )
        assert len(message_api.update_calls) == 1
        request = message_api.update_calls[0]
        _assert_interactive_update_contract(request, message_id="om_update_matrix")
        card = json.loads(request.request_body.content)
        assert "answered: Yes" in card["header"]["title"]["content"]
        assert _interactive_card_action_values(card) == []

    @pytest.mark.asyncio
    async def test_update_prompt_audited_resolution_keeps_state_and_releases_claim_when_side_effect_fails(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        events = _install_event_recorder(adapter)
        with patch.object(
            adapter,
            "_send_raw_message",
            new_callable=AsyncMock,
            return_value=_FakeResponse(message_id="om_update_12"),
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={
                    "inbound_id": "inbound-update",
                    "session_id": "session-update",
                    "correlation_id": "corr-update",
                },
            )
        assert result.success is True
        prompt_id = next(iter(adapter._update_prompt_state))
        events.clear()
        original_write_response = adapter._write_update_prompt_response
        writes = []

        def write_response(answer):
            writes.append(answer)
            if len(writes) == 1:
                raise RuntimeError("response store unavailable")
            original_write_response(answer)

        monkeypatch.setattr(adapter, "_write_update_prompt_response", write_response)

        await adapter._resolve_update_prompt(prompt_id, "y", "Alice")

        assert prompt_id in adapter._update_prompt_state
        assert "resolution_claim" not in adapter._update_prompt_state[prompt_id]
        assert not (hermes_home / ".update_response").exists()
        assert len(message_api.update_calls) == 2
        retry_card = _interactive_card_from_update_request(message_api.update_calls[1])
        retry_values = _interactive_card_action_values(retry_card)
        assert {value.get("hermes_update_prompt_action") for value in retry_values} == {"y", "n"}
        assert {value.get("update_prompt_id") for value in retry_values} == {prompt_id}

        await adapter._resolve_update_prompt(prompt_id, "y", "Alice")

        assert writes == ["y", "y"]
        assert len(message_api.update_calls) == 3
        sent_ids = [event["delivery_id"] for event in events if event["type"] == "delivery_sent"]
        assert len(sent_ids) == 3
        assert len(set(sent_ids)) == 3
        assert (hermes_home / ".update_response").read_text() == "y"
        assert prompt_id not in adapter._update_prompt_state

    @pytest.mark.asyncio
    async def test_update_prompt_audited_patch_pending_failure_does_not_write_response(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        events = _install_event_recorder(adapter, fail_event_types={"delivery_pending"})
        adapter._update_prompt_state[10] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_update_10",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-update",
            "session_id": "session-update",
            "correlation_id": "corr-update",
        }

        await adapter._resolve_update_prompt(10, "n", "Alice")

        assert _event_types(events) == ["delivery_pending"]
        assert message_api.update_calls == []
        assert not (hermes_home / ".update_response").exists()
        assert 10 in adapter._update_prompt_state
        assert "resolution_claim" not in adapter._update_prompt_state[10]

    @pytest.mark.asyncio
    async def test_update_prompt_audited_conflicting_concurrent_resolutions_single_card_update_matches_response(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = _make_audited_adapter(tmp_path)
        message_api = _FakeMessageApi(update_delay=0.05)
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        events = _install_event_recorder(adapter)
        adapter._update_prompt_state[11] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "om_update_11",
            "chat_id": "oc_12345",
            "inbound_id": "inbound-update",
            "session_id": "session-update",
            "correlation_id": "corr-update",
        }

        await asyncio.gather(
            adapter._resolve_update_prompt(11, "y", "Alice"),
            adapter._resolve_update_prompt(11, "n", "Bob"),
        )

        assert len(message_api.update_calls) == 1
        assert [event["operation"] for event in events if event["type"] == "delivery_sent"] == [
            "update_prompt_card_update"
        ]
        response = (hermes_home / ".update_response").read_text()
        assert response in {"y", "n"}
        title = _interactive_card_title_from_update_request(message_api.update_calls[0])
        expected_title = "answered: Yes" if response == "y" else "answered: No"
        assert expected_title in title
        assert 11 not in adapter._update_prompt_state

    @pytest.mark.asyncio
    async def test_writes_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_003",
            "chat_id": "oc_12345",
        }

        await adapter._resolve_update_prompt(1, "y", "Alice")

        assert (tmp_path / ".hermes" / ".update_response").read_text() == "y"
        assert 1 not in adapter._update_prompt_state

    @pytest.mark.asyncio
    async def test_overwrites_existing_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / ".update_response").write_text("n")
        adapter._update_prompt_state[2] = {
            "session_key": "sess-up-2",
            "message_id": "msg_up_004",
            "chat_id": "oc_12345",
        }

        await adapter._resolve_update_prompt(2, "y", "Alice")

        assert (home / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_unknown_prompt_id_drops_silently(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()

        await adapter._resolve_update_prompt(99, "n", "Nobody")

        assert not (tmp_path / ".hermes" / ".update_response").exists()

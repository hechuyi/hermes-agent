import importlib
import json
import multiprocessing

import pytest

from gateway.gateway_event_contract import (
    validate_feishu_request_descriptor,
    validate_gateway_action,
)
from gateway import gateway_event_ledger
from gateway.gateway_event_ledger import LEDGER_FILENAME, LOCK_FILENAME
from gateway.hermes_tools_gateway_event import (
    _validated_feishu_request,
    apply_gateway_event,
    preflight_gateway_event,
)


def _delivery_pending(delivery_id: str, timestamp: int = 1) -> dict[str, object]:
    return {
        "type": "delivery_pending",
        "delivery_id": delivery_id,
        "inbound_id": f"inbound-{delivery_id}",
        "target": "feishu:chat",
        "session_id": "session-1",
        "correlation_id": f"correlation-{delivery_id}",
        "timestamp": timestamp,
    }


def _delivery_sent(
    delivery_id: str, message_id: str, timestamp: int = 2
) -> dict[str, object]:
    return {
        "type": "delivery_sent",
        "delivery_id": delivery_id,
        "message_id": message_id,
        "timestamp": timestamp,
    }


def _feishu_ack(message_id: str, ack_event_id: str, timestamp: int = 3) -> dict[str, object]:
    return {
        "type": "feishu_ack",
        "message_id": message_id,
        "ack_event_id": ack_event_id,
        "timestamp": timestamp,
    }


def _stale_pending_scan(now: int = 100, max_age_seconds: int = 1) -> dict[str, object]:
    return {
        "type": "stale_pending_scan",
        "now": now,
        "max_age_seconds": max_age_seconds,
    }


def _session_locked(
    session_key: str = "feishu:chat:oc_1",
    session_id: str = "session-a",
    correlation_id: str = "corr-1",
) -> dict[str, object]:
    return {
        "type": "session_locked",
        "session_key": session_key,
        "session_id": session_id,
        "correlation_id": correlation_id,
    }


def _compression_result(
    observed_session_id: str,
    *,
    session_key: str = "feishu:chat:oc_1",
    correlation_id: str = "corr-1",
) -> dict[str, object]:
    return {
        "type": "compression_result",
        "session_key": session_key,
        "observed_session_id": observed_session_id,
        "correlation_id": correlation_id,
    }


def _apply_pending_from_process(state_dir, delivery_id, start_event, result_queue):
    start_event.wait(5)
    result = apply_gateway_event(_delivery_pending(delivery_id), state_dir)
    result_queue.put((delivery_id, result.ok, result.failure_class))


def test_facade_does_not_expose_subprocess_adapter():
    module = importlib.import_module("gateway.hermes_tools_gateway_event")

    assert not hasattr(module, "subprocess")
    assert not hasattr(module, "DEFAULT_HERMES_TOOLS_BINARY")


def test_feishu_send_interactive_descriptor_is_allowed_without_raw_content_diagnostics():
    descriptor = {
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
    }

    validated = validate_feishu_request_descriptor(descriptor)

    assert validated == descriptor
    assert _validated_feishu_request(descriptor) == descriptor


def test_feishu_patch_interactive_descriptor_is_allowed():
    descriptor = {
        "operation": "patch_interactive_message",
        "method": "PATCH",
        "path": "/open-apis/im/v1/messages/om_card_123",
        "params": {},
        "body": {"content": '{"config":{"wide_screen_mode":true}}'},
    }

    validated = validate_feishu_request_descriptor(descriptor)

    assert validated == descriptor
    assert _validated_feishu_request(descriptor) == descriptor


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "operation": "send_interactive_message",
            "method": "POST",
            "path": "/open-apis/im/v1/chats",
            "params": {"receive_id_type": "chat_id"},
            "body": {
                "receive_id": "oc_chat",
                "msg_type": "interactive",
                "content": '{"config":{"wide_screen_mode":true}}',
            },
        },
        {
            "operation": "patch_interactive_message",
            "method": "PATCH",
            "path": "/open-apis/im/v1/messages/../../secret",
            "params": {},
            "body": {"content": '{"config":{"wide_screen_mode":true}}'},
        },
        {
            "operation": "send_interactive_message",
            "method": "POST",
            "path": "/open-apis/im/v1/messages",
            "params": {"receive_id_type": "chat_id", "extra": "leak"},
            "body": {
                "receive_id": "oc_chat",
                "msg_type": "interactive",
                "content": '{"config":{"wide_screen_mode":true}}',
            },
        },
    ],
)
def test_feishu_descriptor_generic_or_extra_paths_are_rejected(descriptor):
    assert validate_feishu_request_descriptor(descriptor) is None
    assert _validated_feishu_request(descriptor) is None


def test_feishu_descriptor_receive_id_must_be_safe():
    descriptor = {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": "chat_id"},
        "body": {
            "receive_id": "oc chat",
            "msg_type": "interactive",
            "content": '{"config":{"wide_screen_mode":true}}',
        },
    }

    assert validate_feishu_request_descriptor(descriptor) is None
    assert _validated_feishu_request(descriptor) is None


def test_feishu_inbound_admission_hashes_raw_ids(tmp_path):
    event = {
        "type": "feishu_inbound",
        "inbound_id": "raw-inbound-id",
        "message_id": "raw-message-id",
        "message_type": "text",
        "timestamp": 1_700_000_000,
    }

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.event_type == "feishu_inbound"
    assert result.action is not None
    record = result.action["record"]
    assert result.action["type"] == "inbound_admission"
    assert result.action["decision"] == "continue"
    assert result.action["duplicate"] is False
    assert record["inbound_id_hash"].startswith("fnv1a64:")
    assert record["message_id_hash"].startswith("fnv1a64:")
    assert record["message_type"] == "text"
    assert record["first_seen_at"] == 1_700_000_000
    assert "raw-inbound-id" not in repr(result.action)
    assert "raw-message-id" not in repr(result.action)
    assert result.diagnostics == ""

    duplicate = apply_gateway_event(event, tmp_path)
    assert duplicate.ok is True
    assert duplicate.action is not None
    assert duplicate.action["duplicate"] is True
    assert duplicate.action["record"] == record


def test_late_delivery_failed_does_not_override_acked_or_enter_stale_scan(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True
    acked = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same"), tmp_path)
    assert acked.ok is True
    assert acked.action is not None
    assert acked.action["record"]["status"] == "acked"

    failed = apply_gateway_event(
        {
            "type": "delivery_failed",
            "delivery_id": "delivery-1",
            "failure_class": "send_failed",
            "timestamp": 4,
        },
        tmp_path,
    )

    assert failed.ok is False
    assert failed.failure_class == "invalid_delivery_state_transition"
    repeat_ack = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same", 5), tmp_path)
    assert repeat_ack.ok is True
    assert repeat_ack.action is not None
    assert repeat_ack.action["record"]["status"] == "acked"
    scan = apply_gateway_event(_stale_pending_scan(), tmp_path)
    assert scan.ok is True
    assert scan.action is not None
    assert scan.action["records"] == []


def test_late_unknown_delivery_state_does_not_override_sent_or_enter_stale_scan(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    sent = apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path)
    assert sent.ok is True
    assert sent.action is not None
    assert sent.action["record"]["status"] == "sent"

    unknown = apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-1",
            "failure_class": "provider_state_unknown",
            "timestamp": 3,
        },
        tmp_path,
    )

    assert unknown.ok is False
    assert unknown.failure_class == "invalid_delivery_state_transition"
    repeat_sent = apply_gateway_event(_delivery_sent("delivery-1", "om_message_1", 4), tmp_path)
    assert repeat_sent.ok is True
    assert repeat_sent.action is not None
    assert repeat_sent.action["record"]["status"] == "sent"
    scan = apply_gateway_event(_stale_pending_scan(), tmp_path)
    assert scan.ok is True
    assert scan.action is not None
    assert scan.action["records"] == []


def test_ack_event_id_is_global_and_rejects_different_message_conflict(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_pending("delivery-2"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-2", "om_message_2"), tmp_path).ok is True
    assert apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same"), tmp_path).ok is True

    conflicting = apply_gateway_event(
        _feishu_ack("om_message_2", "ev_read_same", timestamp=4),
        tmp_path,
    )

    assert conflicting.ok is False
    assert conflicting.failure_class == "ack_event_id_conflict"
    assert apply_gateway_event(_feishu_ack("om_message_2", "ev_read_different", 5), tmp_path).ok is True


def test_ack_event_id_repeat_for_same_message_is_idempotent(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True
    first = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same"), tmp_path)
    repeat = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same", timestamp=4), tmp_path)

    assert first.ok is True
    assert repeat.ok is True
    assert repeat.action is not None
    assert repeat.action["record"]["status"] == "acked"
    assert repeat.action["record"]["ack_event_id"] == "ev_read_same"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["ack_event_index"] == {"ev_read_same": "om_message_1"}


def test_corrupted_non_mapping_delivery_record_fails_closed_on_stale_scan(tmp_path):
    ledger = {
        "version": 1,
        "inbounds": {},
        "deliveries": {"delivery-1": ["not", "a", "record"]},
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
    }
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")

    result = apply_gateway_event(_stale_pending_scan(), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.diagnostics == ""


def test_invalid_persisted_ledger_version_fails_with_schema_class(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text(
        json.dumps(
            {
                "version": 2,
                "inbounds": {},
                "deliveries": {},
                "delivery_identity_index": {},
                "feishu_message_index": {},
                "ack_event_index": {},
            }
        ),
        encoding="utf-8",
    )

    result = apply_gateway_event(_stale_pending_scan(), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.diagnostics == ""


def test_mismatched_persisted_delivery_key_fails_with_schema_class(tmp_path):
    ledger = {
        "version": 1,
        "inbounds": {},
        "deliveries": {
            "delivery-wrong": {
                "delivery_id": "delivery-1",
                "inbound_id": "inbound-delivery-1",
                "target": "feishu:chat",
                "session_id": "session-1",
                "correlation_id": "correlation-delivery-1",
                "status": "pending",
                "created_at": 1,
                "updated_at": 1,
                "feishu_message_id": None,
                "failure_class": None,
                "ack_event_id": None,
            }
        },
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
    }
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")

    result = apply_gateway_event(_stale_pending_scan(), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.diagnostics == ""


def test_conflicting_persisted_message_index_fails_with_schema_class(tmp_path):
    ledger = {
        "version": 1,
        "inbounds": {},
        "deliveries": {
            "delivery-1": {
                "delivery_id": "delivery-1",
                "inbound_id": "inbound-delivery-1",
                "target": "feishu:chat",
                "session_id": "session-1",
                "correlation_id": "correlation-delivery-1",
                "status": "sent",
                "created_at": 1,
                "updated_at": 2,
                "feishu_message_id": "om_message_1",
                "failure_class": None,
                "ack_event_id": None,
            }
        },
        "delivery_identity_index": {},
        "feishu_message_index": {"om_message_1": "delivery-other"},
        "ack_event_index": {},
    }
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")

    result = apply_gateway_event(_feishu_ack("om_message_1", "ev_read"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.diagnostics == ""


def test_apply_gateway_event_uses_sidecar_file_lock(monkeypatch, tmp_path):
    lock_names = []
    original = gateway_event_ledger._acquire_file_lock

    def wrapped(handle, deadline):
        lock_names.append(handle.name)
        return original(handle, deadline)

    monkeypatch.setattr(gateway_event_ledger, "_acquire_file_lock", wrapped)

    result = apply_gateway_event(_delivery_pending("delivery-1"), tmp_path)

    assert result.ok is True
    assert lock_names
    assert lock_names[0].endswith(LOCK_FILENAME)


def test_concurrent_process_writes_preserve_all_deliveries(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    delivery_ids = [f"delivery-process-{index}" for index in range(8)]
    processes = [
        ctx.Process(
            target=_apply_pending_from_process,
            args=(tmp_path, delivery_id, start_event, result_queue),
        )
        for delivery_id in delivery_ids
    ]

    try:
        for process in processes:
            process.start()
        start_event.set()
        results = [result_queue.get(timeout=15) for _ in processes]
    finally:
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(delivery_id for delivery_id, ok, _ in results if ok) == delivery_ids
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert sorted(state["deliveries"]) == delivery_ids


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_delivery_timestamp_is_rejected_without_nonstandard_json(
    tmp_path, timestamp
):
    result = apply_gateway_event(_delivery_pending("delivery-1", timestamp=timestamp), tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    ledger_path = tmp_path / LEDGER_FILENAME
    assert not ledger_path.exists()


@pytest.mark.parametrize("field", ["now", "max_age_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_stale_scan_numbers_are_rejected_without_nonstandard_json(
    tmp_path, field, value
):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    event = _stale_pending_scan()
    event[field] = value

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    ledger_text = (tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8")
    assert "NaN" not in ledger_text
    assert "Infinity" not in ledger_text


def test_pending_delivery_can_transition_to_failed(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True

    result = apply_gateway_event(
        {
            "type": "delivery_failed",
            "delivery_id": "delivery-1",
            "failure_class": "send_failed",
            "timestamp": 2,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "failed"
    assert result.action["record"]["failure_class"] == "send_failed"


def test_pending_delivery_can_transition_to_unknown(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True

    result = apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-1",
            "failure_class": "provider_state_unknown",
            "timestamp": 2,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "unknown"
    assert result.action["record"]["failure_class"] == "provider_state_unknown"


def test_unknown_delivery_can_transition_to_failed(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert (
        apply_gateway_event(
            {
                "type": "unknown_delivery_state",
                "delivery_id": "delivery-1",
                "failure_class": "provider_state_unknown",
                "timestamp": 2,
            },
            tmp_path,
        ).ok
        is True
    )

    result = apply_gateway_event(
        {
            "type": "delivery_failed",
            "delivery_id": "delivery-1",
            "failure_class": "send_failed",
            "timestamp": 3,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "failed"
    assert result.action["record"]["failure_class"] == "send_failed"


def test_direct_stale_pending_scan_alerts_without_resend(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1", timestamp=1), tmp_path).ok is True

    result = apply_gateway_event(_stale_pending_scan(now=10, max_age_seconds=5), tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["type"] == "stale_pending_alert"
    assert result.action["alert_required"] is True
    assert result.action["count"] == 1
    assert result.action["resend_permitted"] is False
    assert result.action["records"][0]["delivery_id"] == "delivery-1"


def test_status_card_action_is_not_supported():
    with pytest.raises(ValueError):
        validate_gateway_action({"type": "status_card", "card_action": {}})


def test_task_status_event_fails_closed_without_status_card_support(tmp_path):
    result = apply_gateway_event(
        {
            "type": "task_status",
            "task_id": "task-1",
            "state": "running",
            "timestamp": 1_700_000_001,
        },
        tmp_path,
    )

    assert result.ok is False
    assert result.failure_class == "unsupported_gateway_event_type"
    assert "status" not in result.diagnostics.lower()


def test_invalid_event_type_is_not_echoed_in_result_or_diagnostics(tmp_path):
    result = apply_gateway_event({"type": "bad\nRAW_SECRET"}, tmp_path)

    assert result.ok is False
    assert result.event_type is None
    assert "RAW_SECRET" not in result.reason
    assert "RAW_SECRET" not in result.diagnostics


def test_unknown_delivery_state_with_message_id_preserves_evidence_and_skips_stale_scan(
    tmp_path,
):
    assert apply_gateway_event(_delivery_pending("delivery-1", timestamp=1), tmp_path).ok is True

    result = apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-1",
            "failure_class": "provider_state_unknown",
            "message_id": "om_message_unknown",
            "timestamp": 2,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "unknown"
    assert result.action["record"]["feishu_message_id"] == "om_message_unknown"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["feishu_message_index"] == {"om_message_unknown": "delivery-1"}
    scan = apply_gateway_event(_stale_pending_scan(now=10, max_age_seconds=1), tmp_path)
    assert scan.ok is True
    assert scan.action is not None
    assert scan.action["records"] == []


def test_unknown_delivery_state_message_id_conflict_fails_closed(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_pending("delivery-2"), tmp_path).ok is True

    result = apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-2",
            "failure_class": "provider_state_unknown",
            "message_id": "om_message_1",
            "timestamp": 3,
        },
        tmp_path,
    )

    assert result.ok is False
    assert result.failure_class == "delivery_message_id_conflict"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["deliveries"]["delivery-2"]["feishu_message_id"] is None
    assert state["feishu_message_index"] == {"om_message_1": "delivery-1"}


def test_session_locked_persists_route(tmp_path):
    result = apply_gateway_event(_session_locked(), tmp_path)

    assert result.ok is True
    assert result.event_type == "session_locked"
    assert result.action is not None
    assert result.action["type"] == "session_route"
    assert result.action["record"] == {
        "session_key": "feishu:chat:oc_1",
        "session_id": "session-a",
        "correlation_id": "corr-1",
    }
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["session_routes"]["feishu:chat:oc_1"]["session_id"] == "session-a"


def test_session_locked_same_key_same_session_is_idempotent(tmp_path):
    first = apply_gateway_event(_session_locked(), tmp_path)
    repeat = apply_gateway_event(_session_locked(correlation_id="corr-repeat"), tmp_path)

    assert first.ok is True
    assert repeat.ok is True
    assert repeat.action is not None
    assert repeat.action["record"]["session_id"] == "session-a"
    assert repeat.action["record"]["correlation_id"] == "corr-1"


def test_session_locked_same_key_different_session_conflicts(tmp_path):
    assert apply_gateway_event(_session_locked(), tmp_path).ok is True

    result = apply_gateway_event(_session_locked(session_id="session-b"), tmp_path)

    assert result.ok is False
    assert result.event_type == "session_locked"
    assert result.failure_class == "session_route_conflict"
    assert result.reason == "session route conflict"


def test_compression_result_matching_locked_session_succeeds(tmp_path):
    assert apply_gateway_event(_session_locked(), tmp_path).ok is True

    result = apply_gateway_event(_compression_result("session-a"), tmp_path)

    assert result.ok is True
    assert result.event_type == "compression_result"
    assert result.action is not None
    assert result.action["type"] == "compression_record"
    assert result.action["record"] == {
        "session_key": "feishu:chat:oc_1",
        "locked_session_id": "session-a",
        "observed_session_id": "session-a",
        "correlation_id": "corr-1",
        "status": "accepted",
        "failure_class": None,
    }


def test_compression_result_mismatch_fails_closed_and_persists_audit(tmp_path):
    assert apply_gateway_event(_session_locked(), tmp_path).ok is True

    result = apply_gateway_event(_compression_result("session-b"), tmp_path)

    assert result.ok is False
    assert result.event_type == "compression_result"
    assert result.failure_class == "implicit_session_switch"
    assert result.reason == "implicit session switch"
    assert result.action is None
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["session_routes"]["feishu:chat:oc_1"]["session_id"] == "session-a"
    assert state["compression_rejections"] == [
        {
            "session_key": "feishu:chat:oc_1",
            "locked_session_id": "session-a",
            "observed_session_id": "session-b",
            "correlation_id": "corr-1",
            "failure_class": "implicit_session_switch",
        }
    ]


def test_preflight_checks_exclude_status_card_descriptor(tmp_path):
    result = preflight_gateway_event(tmp_path)

    assert result.ok is True
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
    assert "feishu_request" not in result.action


def test_preflight_session_guard_uses_real_lock_and_mismatch_rejection(tmp_path):
    result = preflight_gateway_event(tmp_path)

    assert result.ok is True
    assert result.action is not None
    checks = {check["name"]: check for check in result.action["checks"]}
    assert checks["session_guard"] == {
        "name": "session_guard",
        "ok": True,
        "detail": "ok",
    }
    ledger_path = tmp_path / LEDGER_FILENAME
    if ledger_path.exists():
        with ledger_path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        assert "preflight-session-key" not in state.get("session_routes", {})

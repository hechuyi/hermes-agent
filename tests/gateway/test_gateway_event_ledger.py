import hashlib
import importlib
import json
import multiprocessing

import pytest

from gateway.gateway_event_contract import (
    GatewayEventContractError,
    validate_feishu_request_descriptor,
    validate_gateway_action,
    validate_gateway_event,
)
from gateway import gateway_event_ledger
from gateway.gateway_event_ledger import (
    LEDGER_FILENAME,
    LOCK_FILENAME,
    feishu_audit_events_for_readiness,
)
from gateway.hermes_tools_gateway_event import (
    _validated_feishu_request,
    apply_gateway_event,
    preflight_gateway_event,
)


def _sha256_ref(domain: str, value: str) -> str:
    material = domain + "\x1f" + value
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _assert_sha256_ref(value: object) -> None:
    assert isinstance(value, str)
    assert value.startswith("sha256:")
    assert len(value) == 71


def _assert_raw_values_absent(value: object, *raw_values: str) -> None:
    rendered = json.dumps(value, sort_keys=True)
    for raw_value in raw_values:
        assert raw_value not in rendered


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


def _feishu_audit_event(event_type: str, **overrides) -> dict[str, object]:
    event: dict[str, object] = {
        "type": event_type,
        "timestamp": 1_700_000_100,
        "correlation_id": "corr-feishu-audit-1",
        "event_hash": "fnv1a64:0123456789abcdef",
        "surface": "feishu.comment",
    }
    event.update(overrides)
    return event


def _c5_provider_decision_event(**overrides) -> dict[str, object]:
    event = _feishu_audit_event(
        "feishu_authorization_provider_decision",
        event_hash="sha256:" + ("0" * 64),
        provider_id="fake_verified_object_acl",
        provider_version="2026-06-10.fake",
        evidence_source_class="verified_object_acl",
        provider_reachability_class="reachable",
        credential_freshness_class="fresh",
        acl_completeness_class="complete",
        unsupported_scope_status="none",
        decision_hash="sha256:" + ("1" * 64),
        contract_hash="sha256:" + ("2" * 64),
        route_snapshot_hash="sha256:" + ("3" * 64),
        object_ref_hash="sha256:" + ("4" * 64),
        authority_subject_hash="sha256:" + ("5" * 64),
        policy_version="policy:v1",
    )
    event.update(overrides)
    return event


def _c5_capability_granted_event(**overrides) -> dict[str, object]:
    event = _feishu_audit_event(
        "feishu_capability_granted",
        event_hash="sha256:" + ("0" * 64),
        grant_hash="sha256:" + ("6" * 64),
        evidence_hashes=["sha256:" + ("7" * 64)],
        contract_hash="sha256:" + ("2" * 64),
        object_type="doc",
        object_ref_hash="sha256:" + ("4" * 64),
        action="read",
        authority_subject_hash="sha256:" + ("5" * 64),
        expiry="no_expiry",
        grant_session_class="one_time",
        policy_version="policy:v1",
    )
    event.update(overrides)
    return event


def _c5_broker_policy_denied_event(**overrides) -> dict[str, object]:
    event = _feishu_audit_event(
        "feishu_broker_policy_denied",
        event_hash="sha256:" + ("0" * 64),
        request_hash="sha256:" + ("8" * 64),
        contract_hash="sha256:" + ("2" * 64),
        route_snapshot_hash="sha256:" + ("3" * 64),
        object_ref_hash="sha256:" + ("4" * 64),
        authority_subject_hash="sha256:" + ("5" * 64),
        action="read",
        failure_class="feishu_broker_policy_denied",
        denial_reason_class="feishu_authorization_provider_missing",
        policy_version="policy:v1",
    )
    event.update(overrides)
    return event


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


def test_delivery_pending_persists_hashed_identity_and_preserves_replay_conflict_semantics(
    tmp_path,
):
    raw_inbound_id = "ou_raw_open_user_1"
    raw_target = "feishu:chat:oc_raw_chat_1"
    raw_session_id = "oc_raw_session_1"
    raw_correlation_id = "om_raw_correlation_1"
    event = {
        "type": "delivery_pending",
        "delivery_id": "delivery-1",
        "inbound_id": raw_inbound_id,
        "target": raw_target,
        "session_id": raw_session_id,
        "correlation_id": raw_correlation_id,
        "timestamp": 1,
    }

    result = apply_gateway_event(event, tmp_path)
    replay = apply_gateway_event(event, tmp_path)
    conflicting_identity = dict(event, delivery_id="delivery-2", timestamp=2)
    conflict = apply_gateway_event(conflicting_identity, tmp_path)
    conflicting_record = dict(event, correlation_id="om_raw_correlation_2", timestamp=2)
    same_delivery_conflict = apply_gateway_event(conflicting_record, tmp_path)

    assert result.ok is True
    assert replay.ok is True
    assert conflict.ok is False
    assert conflict.failure_class == "delivery_identity_conflict"
    assert same_delivery_conflict.ok is False
    assert same_delivery_conflict.failure_class == "delivery_identity_conflict"
    assert result.action is not None
    record = result.action["record"]
    assert replay.action is not None
    assert replay.action["record"] == record
    for field in ("inbound_id", "target", "session_id", "correlation_id"):
        _assert_sha256_ref(record[field])
    _assert_raw_values_absent(
        result.action,
        raw_inbound_id,
        raw_target,
        raw_session_id,
        raw_correlation_id,
    )
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["deliveries"]["delivery-1"] == record
    assert list(state["delivery_identity_index"].values()) == ["delivery-1"]
    identity_key = next(iter(state["delivery_identity_index"]))
    _assert_sha256_ref(identity_key)
    _assert_raw_values_absent(
        state,
        raw_inbound_id,
        raw_target,
        raw_session_id,
        raw_correlation_id,
    )


def test_delivery_sent_persists_hashed_message_ref(tmp_path):
    raw_message_id = "om_message_1"
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True

    result = apply_gateway_event(_delivery_sent("delivery-1", raw_message_id), tmp_path)

    assert result.ok is True
    assert result.action is not None
    message_ref = _sha256_ref("feishu_message", raw_message_id)
    assert result.action["record"]["feishu_message_id"] == message_ref
    _assert_raw_values_absent(result.action, raw_message_id)
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["feishu_message_index"] == {message_ref: "delivery-1"}
    assert state["deliveries"]["delivery-1"]["feishu_message_id"] == message_ref
    _assert_raw_values_absent(state, raw_message_id)


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
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    _assert_raw_values_absent(state, "om_message_1", "om_message_2", "ev_read_same")
    assert apply_gateway_event(_feishu_ack("om_message_2", "ev_read_different", 5), tmp_path).ok is True


def test_ack_event_id_repeat_for_same_message_is_idempotent(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True
    first = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same"), tmp_path)
    repeat = apply_gateway_event(_feishu_ack("om_message_1", "ev_read_same", timestamp=4), tmp_path)
    message_ref = _sha256_ref("feishu_message", "om_message_1")
    ack_ref = _sha256_ref("feishu_ack_event", "ev_read_same")

    assert first.ok is True
    assert repeat.ok is True
    assert repeat.action is not None
    assert repeat.action["record"]["status"] == "acked"
    assert repeat.action["record"]["feishu_message_id"] == message_ref
    assert repeat.action["record"]["ack_event_id"] == ack_ref
    _assert_raw_values_absent(repeat.action, "om_message_1", "ev_read_same")
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["ack_event_index"] == {ack_ref: message_ref}
    _assert_raw_values_absent(state, "om_message_1", "ev_read_same")


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


def test_missing_persisted_ledger_section_fails_without_overwriting_state(tmp_path):
    ledger = {
        "version": 1,
        "inbounds": {},
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
        "session_routes": {},
        "compression_rejections": [],
    }
    ledger_path = tmp_path / LEDGER_FILENAME
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    result = apply_gateway_event(_delivery_pending("delivery-1"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    with ledger_path.open(encoding="utf-8") as handle:
        assert json.load(handle) == ledger


def test_corrupt_persisted_json_fails_with_schema_class(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text('{"version": 1,', encoding="utf-8")

    result = apply_gateway_event(_delivery_pending("delivery-1"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.reason == "gateway event state schema invalid"


@pytest.mark.parametrize(
    ("event_type", "hash_field", "extra"),
    [
        ("feishu_contract_observed", "contract_hash", {"scope": "conversation"}),
        (
            "feishu_authorization_evidence_observed",
            "authorization_evidence_hash",
            {
                "contract_hash": "sha256:" + ("2" * 64),
                "route_snapshot_hash": "sha256:" + ("3" * 64),
                "object_ref_hash": "sha256:" + ("4" * 64),
                "authority_subject_hash": "sha256:" + ("5" * 64),
                "evidence_source_class": "verified_object_acl",
                "evidence_state_class": "current",
                "policy_version": "policy:v1",
            },
        ),
        (
            "feishu_authorization_evidence_denied",
            "authorization_evidence_hash",
            {
                "request_hash": "sha256:" + ("8" * 64),
                "failure_class": "feishu_authorization_evidence_missing",
                "denial_reason_class": "feishu_authorization_evidence_missing",
                "evidence_source_class": "none",
                "policy_version": "policy:v1",
            },
        ),
        (
            "feishu_auth_decision",
            "decision_hash",
            {
                "request_hash": "sha256:" + ("8" * 64),
                "contract_hash": "sha256:" + ("2" * 64),
                "decision": "denied",
                "failure_class": "feishu_authorization_denied",
                "policy_version": "policy:v1",
            },
        ),
        ("feishu_object_scope_resolved", "object_ref_hash", {"object_kind": "doc"}),
        (
            "feishu_capability_granted",
            "grant_hash",
            {
                "evidence_hashes": ["sha256:" + ("7" * 64)],
                "contract_hash": "sha256:" + ("2" * 64),
                "object_type": "doc",
                "object_ref_hash": "sha256:" + ("4" * 64),
                "action": "read",
                "authority_subject_hash": "sha256:" + ("5" * 64),
                "expiry": "no_expiry",
                "grant_session_class": "one_time",
                "policy_version": "policy:v1",
            },
        ),
        (
            "feishu_capability_denied",
            "request_hash",
            {
                "contract_hash": "sha256:" + ("2" * 64),
                "object_ref_hash": "sha256:" + ("4" * 64),
                "action": "read",
                "failure_class": "feishu_capability_missing",
                "denial_reason_class": "feishu_capability_missing",
                "policy_version": "policy:v1",
            },
        ),
        ("feishu_action_requested", "action_hash", {"action": "reply"}),
        ("feishu_action_authorized", "action_hash", {"action": "reply"}),
        (
            "feishu_action_denied",
            "action_hash",
            {"action": "reply", "failure_class": "feishu_action_requires_broker"},
        ),
        ("feishu_action_executed", "action_hash", {"action": "reply"}),
        ("feishu_tool_result_redacted", "result_hash", {"tool": "feishu_doc"}),
        (
            "feishu_api_failure",
            "request_hash",
            {"api": "comment.reply", "failure_class": "feishu_api_failed"},
        ),
        (
            "feishu_legacy_tool_denied",
            "legacy_tool_hash",
            {"tool": "feishu_doc", "failure_class": "feishu_legacy_tool_requires_broker"},
        ),
        (
            "feishu_legacy_descriptor_denied",
            "descriptor_hash",
            {
                "surface": "feishu.descriptor",
                "failure_class": "feishu_legacy_descriptor_requires_broker",
            },
        ),
    ],
)
def test_feishu_audit_event_families_are_persisted_as_sanitized_records(
    tmp_path, event_type, hash_field, extra
):
    event = _feishu_audit_event(
        event_type,
        event_hash="sha256:" + ("0" * 64),
        **{hash_field: "fnv1a64:fedcba9876543210"},
        **extra,
    )

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.event_type == event_type
    assert result.action is not None
    assert result.action["type"] == "feishu_audit_event_record"
    assert result.action["record"] == event
    assert validate_gateway_event(event) == event_type
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["version"] == 1
    assert state["feishu_audit_events"] == [event]


@pytest.mark.parametrize(
    "event",
    [
        _c5_provider_decision_event(),
        _c5_provider_decision_event(
            failure_class="feishu_provider_sdk_unreachable",
            denial_reason_class="feishu_provider_sdk_unreachable",
        ),
        _feishu_audit_event(
            "feishu_authorization_evidence_observed",
            event_hash="sha256:" + ("0" * 64),
            authorization_evidence_hash="sha256:" + ("7" * 64),
            contract_hash="sha256:" + ("2" * 64),
            route_snapshot_hash="sha256:" + ("3" * 64),
            object_ref_hash="sha256:" + ("4" * 64),
            authority_subject_hash="sha256:" + ("5" * 64),
            evidence_source_class="verified_object_acl",
            evidence_state_class="current",
            policy_version="policy:v1",
        ),
        _feishu_audit_event(
            "feishu_authorization_evidence_denied",
            event_hash="sha256:" + ("0" * 64),
            authorization_evidence_hash="sha256:" + ("7" * 64),
            request_hash="sha256:" + ("8" * 64),
            failure_class="feishu_authorization_evidence_missing",
            denial_reason_class="feishu_authorization_evidence_missing",
            evidence_source_class="none",
            policy_version="policy:v1",
        ),
        _feishu_audit_event(
            "feishu_auth_decision",
            event_hash="sha256:" + ("0" * 64),
            decision_hash="sha256:" + ("1" * 64),
            request_hash="sha256:" + ("8" * 64),
            contract_hash="sha256:" + ("2" * 64),
            decision="authorized",
            policy_version="policy:v1",
        ),
        _c5_capability_granted_event(),
        _c5_capability_granted_event(
            expiry="2026-06-10T00:03:00Z",
            grant_session_class="short_session",
        ),
        _feishu_audit_event(
            "feishu_capability_denied",
            event_hash="sha256:" + ("0" * 64),
            request_hash="sha256:" + ("8" * 64),
            contract_hash="sha256:" + ("2" * 64),
            object_ref_hash="sha256:" + ("4" * 64),
            action="read",
            failure_class="feishu_object_ref_mismatch",
            denial_reason_class="feishu_object_ref_mismatch",
            policy_version="policy:v1",
        ),
        _c5_broker_policy_denied_event(),
    ],
)
def test_c5_audit_events_accept_only_required_sanitized_contract(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"] == event


@pytest.mark.parametrize(
    "event",
    [
        _c5_provider_decision_event(provider_version=None),
        _c5_provider_decision_event(provider_reachability_class="unknown"),
        _c5_provider_decision_event(
            credential_freshness_class="revoked",
            failure_class="feishu_provider_revoked_credential",
        ),
        _c5_provider_decision_event(
            failure_class="feishu_provider_sdk_unreachable",
        ),
        _c5_capability_granted_event(evidence_hashes=[]),
        _c5_capability_granted_event(evidence_hashes=["not-a-hash"]),
        _c5_broker_policy_denied_event(denial_reason_class="tenant_access_token"),
        _c5_broker_policy_denied_event(open_id="ou_raw_user_1"),
        _c5_provider_decision_event(raw_acl_json="{}"),
        _c5_provider_decision_event(user_token="secret"),
    ],
)
def test_c5_audit_events_reject_missing_failure_and_raw_material(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "event",
    [
        _c5_provider_decision_event(
            provider_reachability_class="telepathic",
            failure_class="feishu_provider_sdk_unreachable",
            denial_reason_class="feishu_provider_sdk_unreachable",
        ),
        _c5_provider_decision_event(
            credential_freshness_class="banana",
            failure_class="feishu_provider_stale_credential",
            denial_reason_class="feishu_provider_stale_credential",
        ),
        _c5_provider_decision_event(
            acl_completeness_class="maybe",
            failure_class="feishu_provider_acl_incomplete",
            denial_reason_class="feishu_provider_acl_incomplete",
        ),
        _c5_provider_decision_event(
            unsupported_scope_status="sometimes",
            failure_class="feishu_provider_unsupported_scope",
            denial_reason_class="feishu_provider_unsupported_scope",
        ),
        _c5_provider_decision_event(evidence_source_class="nonsense_source"),
        _c5_capability_granted_event(grant_session_class="banana"),
    ],
)
def test_c5_audit_events_reject_semantically_invalid_class_values(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_provider_decision_denial_requires_sanitized_denial_reason_class(tmp_path):
    event = _c5_provider_decision_event(
        provider_reachability_class="unreachable",
        credential_freshness_class="unknown",
        failure_class="feishu_provider_sdk_unreachable",
    )

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "stable_class",
    [
        "feishu_authorization_provider_decision_missing",
        "feishu_provider_authorization_exception",
        "feishu_scope_not_scoped",
        "feishu_authorization_evidence_stale",
        "feishu_authorization_evidence_revoked",
        "feishu_object_authority_evidence_missing",
    ],
)
def test_c5_broker_denials_accept_stable_broker_contract_domains(
    tmp_path, stable_class
):
    event = _c5_broker_policy_denied_event(
        failure_class=stable_class,
        denial_reason_class=stable_class,
    )

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"] == event


@pytest.mark.parametrize(
    "event",
    [
        _c5_broker_policy_denied_event(
            failure_class="random_failure",
            denial_reason_class="feishu_authorization_provider_missing",
        ),
        _c5_broker_policy_denied_event(
            failure_class="feishu_broker_policy_denied",
            denial_reason_class="random_reason",
        ),
    ],
)
def test_c5_audit_denials_reject_unknown_failure_and_denial_domains(
    tmp_path, event
):
    with pytest.raises(GatewayEventContractError):
        validate_gateway_event(event)

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_provider_decision_rejects_unknown_revocation_reason_class(tmp_path):
    event = _c5_provider_decision_event(
        credential_freshness_class="revoked",
        failure_class="feishu_provider_revoked_credential",
        denial_reason_class="feishu_provider_revoked_credential",
        revocation_reason_class="banana",
    )

    with pytest.raises(GatewayEventContractError):
        validate_gateway_event(event)

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "raw_marker",
    [
        "raw_acl",
        "raw_document",
        "object_ref",
        "raw_message",
        "message_content",
        "object_id",
        "feishu_object_id",
        "acl_response_body",
        "document_content",
    ],
)
def test_provider_decision_rejects_raw_marker_provider_id_values(
    tmp_path, raw_marker
):
    event = _c5_provider_decision_event(provider_id=raw_marker)

    with pytest.raises(GatewayEventContractError):
        validate_gateway_event(event)

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "provider_id",
    [
        "safe_raw_message_provider",
        "safe-raw-message-provider",
        "prefixobjectidpostfix",
    ],
)
def test_provider_decision_rejects_embedded_raw_marker_provider_id_values(
    tmp_path, provider_id
):
    event = _c5_provider_decision_event(provider_id=provider_id)

    with pytest.raises(GatewayEventContractError) as exc_info:
        validate_gateway_event(event)

    assert "sensitive value" in str(exc_info.value)
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_provider_decision_accepts_opaque_provider_hash_identifier(tmp_path):
    event = _c5_provider_decision_event(provider_id="provider:sha256:" + ("a" * 64))

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"] == event


@pytest.mark.parametrize(
    ("field", "event_factory"),
    [
        ("provider_id", _c5_provider_decision_event),
        ("provider_version", _c5_provider_decision_event),
        ("policy_version", _c5_provider_decision_event),
        ("action", _c5_broker_policy_denied_event),
        ("tool", _c5_broker_policy_denied_event),
        ("surface", _c5_broker_policy_denied_event),
    ],
)
@pytest.mark.parametrize(
    "raw_marker",
    [
        "raw_acl",
        "raw_document",
        "object_ref",
        "raw_message",
        "message_content",
        "object_id",
        "feishu_object_id",
        "acl_response_body",
        "document_content",
    ],
)
def test_c5_audit_atom_fields_reject_broker_raw_markers(
    tmp_path, field, event_factory, raw_marker
):
    event = event_factory(**{field: raw_marker})

    with pytest.raises(GatewayEventContractError) as exc_info:
        validate_gateway_event(event)

    assert "sensitive value" in str(exc_info.value)
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "raw_marker",
    [
        "raw_acl",
        "raw_document",
        "object_ref",
        "raw_message",
        "message_content",
        "object_id",
        "feishu_object_id",
        "acl_response_body",
        "document_content",
    ],
)
def test_c5_audit_denials_reject_raw_marker_class_values(tmp_path, raw_marker):
    event = _c5_broker_policy_denied_event(
        failure_class=raw_marker,
        denial_reason_class=raw_marker,
    )

    with pytest.raises(GatewayEventContractError) as exc_info:
        validate_gateway_event(event)

    assert "sensitive value" in str(exc_info.value)
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


@pytest.mark.parametrize(
    "event",
    [
        _c5_provider_decision_event(
            provider_reachability_class="unreachable",
            credential_freshness_class="unknown",
            failure_class="tenant_access_token_secret",
            denial_reason_class="feishu_provider_sdk_unreachable",
        ),
        _c5_broker_policy_denied_event(failure_class="tenant_access_token_secret"),
    ],
)
def test_feishu_audit_denials_reject_raw_failure_class_markers(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_feishu_audit_events_are_append_only_without_rolling_truncation(tmp_path):
    for index in range(1001):
        event = _feishu_audit_event(
            "feishu_action_requested",
            correlation_id=f"corr-{index}",
            event_hash=f"fnv1a64:{index:016x}",
            action_hash=f"fnv1a64:{index + 1:016x}",
            action="reply",
        )
        result = apply_gateway_event(event, tmp_path)
        assert result.ok is True

    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)

    assert len(state["feishu_audit_events"]) == 1001
    assert state["feishu_audit_events"][0]["correlation_id"] == "corr-0"
    assert state["feishu_audit_events"][-1]["correlation_id"] == "corr-1000"


def test_feishu_audit_events_for_readiness_is_read_only_and_uses_sanitized_audit_section(
    tmp_path,
):
    event = _feishu_audit_event(
        "feishu_contract_observed",
        event_hash="sha256:" + ("0" * 64),
        contract_hash="fnv1a64:fedcba9876543210",
        route_snapshot_hash="fnv1a64:0123456789abcdef",
        scope="conversation",
    )
    result = apply_gateway_event(event, tmp_path)
    assert result.ok is True
    before = (tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8")

    readiness_events = feishu_audit_events_for_readiness(tmp_path)

    after = (tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8")
    assert readiness_events == (event,)
    assert before == after
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["feishu_audit_events"] == [event]
    assert state["deliveries"] == {}
    assert state["inbounds"] == {}


@pytest.mark.parametrize(
    "event",
    [
        _feishu_audit_event(
            "feishu_legacy_tool_denied",
            token="tenant-access-token",
            failure_class="feishu_legacy_tool_requires_broker",
        ),
        _feishu_audit_event("feishu_action_requested", body={"content": "raw"}),
        _feishu_audit_event("feishu_action_requested", file_path="/tmp/raw-file"),
        _feishu_audit_event("feishu_action_requested", open_id="ou_raw_feishu_id"),
        _feishu_audit_event("feishu_tool_result_redacted", content="raw document text"),
        _feishu_audit_event(
            "feishu_api_failure",
            api_response={"code": 999, "msg": "raw provider response"},
            failure_class="feishu_api_failed",
        ),
        _feishu_audit_event("feishu_action_requested", surface="/open-apis/im/v1/messages"),
        _feishu_audit_event("feishu_action_requested", action='{"raw":"body"}'),
        _feishu_audit_event("feishu_action_requested", object_ref="doccn_raw_id"),
        _feishu_audit_event("feishu_action_requested", action="ou_raw_feishu_id"),
        _feishu_audit_event("feishu_action_requested", surface="oc_raw_chat_id"),
        _feishu_audit_event("feishu_action_requested", tool="tenant_access_token"),
        _feishu_audit_event("feishu_action_requested", surface="doccnrawid"),
        _feishu_audit_event("feishu_action_requested", surface="raw_response"),
        _feishu_audit_event("feishu_action_requested", action="body"),
    ],
)
def test_feishu_audit_rejects_raw_token_body_document_path_and_ids(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_feishu_auth_decision_success_event_does_not_require_failure_class(tmp_path):
    event = _feishu_audit_event(
        "feishu_auth_decision",
        decision="authorized",
        decision_hash="fnv1a64:aaaaaaaaaaaaaaaa",
        request_hash="sha256:" + ("8" * 64),
        contract_hash="sha256:" + ("2" * 64),
        policy_version="policy:v1",
    )

    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"] == event


@pytest.mark.parametrize(
    "event",
    [
        _feishu_audit_event("feishu_action_requested", event_hash="not-a-hash"),
        _feishu_audit_event("feishu_action_requested", event_hash="sha256:" + ("z" * 64)),
        _feishu_audit_event("feishu_action_requested", event_hash=None),
        _feishu_audit_event("feishu_action_denied", action_hash="fnv1a64:aaaaaaaaaaaaaaaa"),
        _feishu_audit_event(
            "feishu_auth_decision",
            decision="denied",
            decision_hash="fnv1a64:aaaaaaaaaaaaaaaa",
        ),
    ],
)
def test_feishu_audit_requires_sanitized_hashes_and_denial_failure_class(tmp_path, event):
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_feishu_audit_keeps_legacy_v1_ledger_version_and_backfills_missing_section(
    tmp_path,
):
    legacy_v1 = {
        "version": 1,
        "inbounds": {},
        "deliveries": {},
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
        "session_routes": {},
        "compression_rejections": [],
    }
    ledger_path = tmp_path / LEDGER_FILENAME
    ledger_path.write_text(json.dumps(legacy_v1), encoding="utf-8")

    result = apply_gateway_event(_feishu_audit_event("feishu_contract_observed"), tmp_path)

    assert result.ok is True
    with ledger_path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["version"] == 1
    assert state["feishu_audit_events"] == [
        _feishu_audit_event("feishu_contract_observed")
    ]


@pytest.mark.parametrize(
    "poisoned_event",
    [
        _feishu_audit_event("feishu_action_requested", token="tenant-access-token"),
        _feishu_audit_event("feishu_action_requested", action="ou_raw_feishu_id"),
        _feishu_audit_event("feishu_action_requested", surface="doccnrawid"),
    ],
)
def test_feishu_audit_poisoned_persisted_records_fail_closed_without_overwrite(
    tmp_path, poisoned_event
):
    ledger = {
        "version": 1,
        "inbounds": {},
        "deliveries": {},
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
        "session_routes": {},
        "compression_rejections": [],
        "feishu_audit_events": [poisoned_event],
    }
    ledger_path = tmp_path / LEDGER_FILENAME
    original_text = json.dumps(ledger)
    ledger_path.write_text(original_text, encoding="utf-8")

    result = apply_gateway_event(_feishu_audit_event("feishu_contract_observed"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert ledger_path.read_text(encoding="utf-8") == original_text


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


def test_raw_legacy_delivery_identity_and_message_state_fails_closed_without_overwrite(
    tmp_path,
):
    ledger = {
        "version": 1,
        "inbounds": {},
        "deliveries": {
            "delivery-1": {
                "delivery_id": "delivery-1",
                "inbound_id": "ou_raw_open_user_1",
                "target": "feishu:chat:oc_raw_chat_1",
                "session_id": "oc_raw_session_1",
                "correlation_id": "om_raw_correlation_1",
                "status": "sent",
                "created_at": 1,
                "updated_at": 2,
                "feishu_message_id": "om_message_1",
                "failure_class": None,
                "ack_event_id": None,
            }
        },
        "delivery_identity_index": {
            "ou_raw_open_user_1\x1ffeishu:chat:oc_raw_chat_1\x1foc_raw_session_1\x1fom_raw_correlation_1": "delivery-1"
        },
        "feishu_message_index": {"om_message_1": "delivery-1"},
        "ack_event_index": {},
    }
    ledger_path = tmp_path / LEDGER_FILENAME
    original_text = json.dumps(ledger)
    ledger_path.write_text(original_text, encoding="utf-8")

    result = apply_gateway_event(_feishu_ack("om_message_1", "ev_read"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.diagnostics == ""
    assert ledger_path.read_text(encoding="utf-8") == original_text


def test_persisted_failed_delivery_without_failure_class_fails_with_schema_class(tmp_path):
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
                "status": "failed",
                "created_at": 1,
                "updated_at": 2,
                "feishu_message_id": None,
                "failure_class": None,
                "ack_event_id": None,
            }
        },
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
        "session_routes": {},
        "compression_rejections": [],
    }
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")

    result = apply_gateway_event(_stale_pending_scan(), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_schema_invalid"


def test_validate_delivery_record_rejects_stale_failure_class_on_sent():
    with pytest.raises(ValueError):
        validate_gateway_action(
            {
                "type": "delivery_record",
                "record": {
                    "delivery_id": "delivery-1",
                    "inbound_id": "inbound-delivery-1",
                    "target": "feishu:chat",
                    "session_id": "session-1",
                    "correlation_id": "correlation-delivery-1",
                    "status": "sent",
                    "created_at": 1,
                    "updated_at": 2,
                    "feishu_message_id": "om_message_1",
                    "failure_class": "provider_state_unknown",
                    "ack_event_id": None,
                },
            }
        )


def test_unknown_delivery_to_sent_clears_failure_class(tmp_path):
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

    result = apply_gateway_event(_delivery_sent("delivery-1", "om_message_1", timestamp=3), tmp_path)

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "sent"
    assert result.action["record"]["failure_class"] is None


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


def test_delivery_sent_rejects_unsafe_feishu_message_id(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True

    result = apply_gateway_event(_delivery_sent("delivery-1", "om bad id"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["deliveries"]["delivery-1"]["feishu_message_id"] is None
    assert state["feishu_message_index"] == {}


def test_feishu_ack_rejects_unsafe_ack_event_id(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1"), tmp_path).ok is True
    assert apply_gateway_event(_delivery_sent("delivery-1", "om_message_1"), tmp_path).ok is True

    result = apply_gateway_event(_feishu_ack("om_message_1", "ev bad\nid"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "invalid_gateway_event_contract"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["deliveries"]["delivery-1"]["status"] == "sent"
    assert state["ack_event_index"] == {}


def test_late_delivery_sent_timestamp_fails_without_moving_updated_at_back(tmp_path):
    assert apply_gateway_event(_delivery_pending("delivery-1", timestamp=10), tmp_path).ok is True

    result = apply_gateway_event(_delivery_sent("delivery-1", "om_message_1", timestamp=9), tmp_path)

    assert result.ok is False
    assert result.failure_class == "delivery_timestamp_regression"
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["deliveries"]["delivery-1"]["updated_at"] == 10
    assert state["feishu_message_index"] == {}


def test_parent_directory_fsync_failure_fails_apply(monkeypatch, tmp_path):
    def fail_parent_fsync(_path):
        raise OSError("parent dir fsync failed")

    monkeypatch.setattr(gateway_event_ledger, "_fsync_parent_dir", fail_parent_fsync)

    result = apply_gateway_event(_delivery_pending("delivery-1"), tmp_path)

    assert result.ok is False
    assert result.failure_class == "gateway_event_state_io_failed"


def test_parent_directory_fsync_propagates_durability_failure(monkeypatch, tmp_path):
    parent_fd = 987654
    original_fsync = gateway_event_ledger.os.fsync
    original_close = gateway_event_ledger.os.close

    def fake_open(_path, _flags):
        return parent_fd

    def fake_fsync(fd):
        if fd == parent_fd:
            raise OSError("parent dir fsync failed")
        return original_fsync(fd)

    def fake_close(fd):
        if fd == parent_fd:
            return None
        return original_close(fd)

    monkeypatch.setattr(gateway_event_ledger.os, "open", fake_open)
    monkeypatch.setattr(gateway_event_ledger.os, "fsync", fake_fsync)
    monkeypatch.setattr(gateway_event_ledger.os, "close", fake_close)

    with pytest.raises(OSError):
        gateway_event_ledger._fsync_parent_dir(tmp_path / LEDGER_FILENAME)


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
    raw_message_id = "om_message_unknown"
    assert apply_gateway_event(_delivery_pending("delivery-1", timestamp=1), tmp_path).ok is True

    result = apply_gateway_event(
        {
            "type": "unknown_delivery_state",
            "delivery_id": "delivery-1",
            "failure_class": "provider_state_unknown",
            "message_id": raw_message_id,
            "timestamp": 2,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action["record"]["status"] == "unknown"
    message_ref = _sha256_ref("feishu_message", raw_message_id)
    assert result.action["record"]["feishu_message_id"] == message_ref
    _assert_raw_values_absent(result.action, raw_message_id)
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["feishu_message_index"] == {message_ref: "delivery-1"}
    _assert_raw_values_absent(state, raw_message_id)
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
    message_ref = _sha256_ref("feishu_message", "om_message_1")
    assert state["feishu_message_index"] == {message_ref: "delivery-1"}
    _assert_raw_values_absent(state, "om_message_1")


def test_session_locked_persists_route(tmp_path):
    result = apply_gateway_event(_session_locked(), tmp_path)

    assert result.ok is True
    assert result.event_type == "session_locked"
    assert result.action is not None
    assert result.action["type"] == "session_route"
    session_key_ref = _sha256_ref("session_route.session_key", "feishu:chat:oc_1")
    session_id_ref = _sha256_ref("session_route.session_id", "session-a")
    correlation_ref = _sha256_ref("session_route.correlation_id", "corr-1")
    assert result.action["record"] == {
        "session_key": session_key_ref,
        "session_id": session_id_ref,
        "correlation_id": correlation_ref,
    }
    _assert_raw_values_absent(result.action, "feishu:chat:oc_1", "session-a", "corr-1")
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["session_routes"][session_key_ref]["session_id"] == session_id_ref
    _assert_raw_values_absent(state, "feishu:chat:oc_1", "session-a", "corr-1")


def test_session_locked_same_key_same_session_is_idempotent(tmp_path):
    first = apply_gateway_event(_session_locked(), tmp_path)
    repeat = apply_gateway_event(_session_locked(correlation_id="corr-repeat"), tmp_path)

    assert first.ok is True
    assert repeat.ok is True
    assert repeat.action is not None
    assert repeat.action["record"]["session_id"] == _sha256_ref(
        "session_route.session_id", "session-a"
    )
    assert repeat.action["record"]["correlation_id"] == _sha256_ref(
        "session_route.correlation_id", "corr-1"
    )


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
    session_key_ref = _sha256_ref("session_route.session_key", "feishu:chat:oc_1")
    session_id_ref = _sha256_ref("session_route.session_id", "session-a")
    correlation_ref = _sha256_ref("session_route.correlation_id", "corr-1")
    assert result.action["record"] == {
        "session_key": session_key_ref,
        "locked_session_id": session_id_ref,
        "observed_session_id": session_id_ref,
        "correlation_id": correlation_ref,
        "status": "accepted",
        "failure_class": None,
    }
    _assert_raw_values_absent(result.action, "feishu:chat:oc_1", "session-a", "corr-1")


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
    session_key_ref = _sha256_ref("session_route.session_key", "feishu:chat:oc_1")
    locked_session_ref = _sha256_ref("session_route.session_id", "session-a")
    observed_session_ref = _sha256_ref("session_route.session_id", "session-b")
    correlation_ref = _sha256_ref("session_route.correlation_id", "corr-1")
    assert state["session_routes"][session_key_ref]["session_id"] == locked_session_ref
    assert state["compression_rejections"] == [
        {
            "session_key": session_key_ref,
            "locked_session_id": locked_session_ref,
            "observed_session_id": observed_session_ref,
            "correlation_id": correlation_ref,
            "failure_class": "implicit_session_switch",
        }
    ]
    _assert_raw_values_absent(state, "feishu:chat:oc_1", "session-a", "session-b", "corr-1")


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


def test_preflight_probe_state_omits_raw_feishu_delivery_ids(monkeypatch, tmp_path):
    probe_dir = tmp_path / "preflight-probe"

    class PersistentTemporaryDirectory:
        def __init__(self, *, dir):
            assert dir == tmp_path

        def __enter__(self):
            probe_dir.mkdir()
            return str(probe_dir)

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        gateway_event_ledger.tempfile,
        "TemporaryDirectory",
        PersistentTemporaryDirectory,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is True
    with (probe_dir / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        state = json.load(handle)
    _assert_raw_values_absent(
        state,
        "preflight-message",
        "preflight_feishu_message",
        "preflight-ack",
        "preflight-session-key",
        "preflight-session-a",
        "preflight-session-b",
        "preflight-correlation",
    )


def test_preflight_fails_closed_when_live_ledger_schema_is_invalid(tmp_path):
    ledger_path = tmp_path / LEDGER_FILENAME
    ledger_path.write_text(
        json.dumps(
            {
                "version": 999,
                "secret": "super-secret-token-value",
            }
        ),
        encoding="utf-8",
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is False
    assert result.event_type == "preflight"
    assert result.failure_class == "gateway_event_state_schema_invalid"
    assert result.reason == "gateway event state schema invalid"
    assert result.action is None
    assert "super-secret-token-value" not in result.diagnostics


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

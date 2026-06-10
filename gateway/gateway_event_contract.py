"""Hermes gateway-event contract validation.

This module intentionally models only the internalized ledger events. It does
not support task/status-card actions or Feishu interactive request descriptors.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class GatewayEventResult:
    ok: bool
    event_type: str | None = None
    action: dict[str, Any] | None = None
    failure_class: str | None = None
    reason: str | None = None
    diagnostics: str = ""


HermesToolsGatewayEventResult = GatewayEventResult

EVENT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "feishu_inbound": ("inbound_id", "message_id", "message_type", "timestamp"),
    "delivery_pending": (
        "delivery_id",
        "inbound_id",
        "target",
        "session_id",
        "correlation_id",
        "timestamp",
    ),
    "delivery_sent": ("delivery_id", "message_id", "timestamp"),
    "delivery_failed": ("delivery_id", "failure_class", "timestamp"),
    "unknown_delivery_state": ("delivery_id", "failure_class", "timestamp"),
    "feishu_ack": ("message_id", "ack_event_id", "timestamp"),
    "stale_pending_scan": ("now", "max_age_seconds"),
    "session_locked": ("session_key", "session_id", "correlation_id"),
    "compression_result": ("session_key", "observed_session_id", "correlation_id"),
}

FEISHU_AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "feishu_contract_observed",
        "feishu_authorization_provider_decision",
        "feishu_authorization_evidence_observed",
        "feishu_authorization_evidence_denied",
        "feishu_auth_decision",
        "feishu_object_scope_resolved",
        "feishu_capability_granted",
        "feishu_capability_denied",
        "feishu_broker_policy_denied",
        "feishu_action_requested",
        "feishu_action_authorized",
        "feishu_action_denied",
        "feishu_action_executed",
        "feishu_tool_result_redacted",
        "feishu_api_failure",
        "feishu_legacy_tool_denied",
        "feishu_legacy_descriptor_denied",
    }
)

FEISHU_AUDIT_FAILURE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "feishu_authorization_evidence_denied",
        "feishu_capability_denied",
        "feishu_broker_policy_denied",
        "feishu_action_denied",
        "feishu_api_failure",
        "feishu_legacy_tool_denied",
        "feishu_legacy_descriptor_denied",
    }
)

FEISHU_DELIVERY_LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "feishu_delivery_attempted",
        "feishu_delivery_sent",
        "feishu_delivery_ack_unknown",
        "feishu_delivery_failed",
    }
)

FEISHU_BROKER_ACTION_LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "feishu_broker_action_created",
        "feishu_broker_action_accepted",
        "feishu_broker_action_resolved",
        "feishu_broker_action_denied",
        "feishu_broker_action_replayed",
    }
)

FEISHU_ATTACHMENT_PROVENANCE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "feishu_attachment_provenance_recorded",
        "feishu_attachment_upload_denied",
    }
)

PREFLIGHT_CHECK_NAMES: tuple[str, ...] = (
    "state_dir_writable",
    "feishu_inbound",
    "delivery_lifecycle",
    "feishu_ack",
    "stale_pending_scan",
    "session_guard",
)

_SAFE_EVENT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_SAFE_FAILURE_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_FNV1A64_RE = re.compile(r"^fnv1a64:[a-f0-9]{16}$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ROUTE_SNAPSHOT_AUDIT_HASH_RE = re.compile(
    r"^(?:route_session_snapshot_hash|route_partition_hash):sha256:[a-f0-9]{64}$"
)
_BROKER_ACTION_ID_RE = re.compile(r"^broker_action:sha256:[a-f0-9]{64}$")
_BROKER_GRANT_HANDLE_RE = re.compile(r"^broker_grant_handle:sha256:[a-f0-9]{64}$")
_SAFE_AUDIT_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RAW_FEISHU_ID_VALUE_RE = re.compile(
    r"^(?:ou|oc|on|om|user|open|union|chat|doccn|file|fld|boxcn|wikcn)[a-z0-9_.:-]*$"
)
_SAFE_FEISHU_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,256}$")
_SAFE_FEISHU_RECEIVE_ID_RE = re.compile(r"^[A-Za-z0-9_@.+-]{1,256}$")
_SAFE_DESCRIPTOR_UUID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,127}$")
_SAFE_FEISHU_EVENT_ID_RE = _SAFE_DESCRIPTOR_UUID_RE
_SAFE_SESSION_ROUTE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,256}$")
FEISHU_INBOUND_TRANSPORT_KINDS = frozenset(
    {"webhook", "websocket", "dm", "group", "thread"}
)
FEISHU_INBOUND_IDEMPOTENCY_EVIDENCE_STATES = frozenset(
    {"current_admitted", "current_required", "legacy_unscoped"}
)
_FEISHU_PATCH_MESSAGE_PATH_RE = re.compile(
    r"^/open-apis/im/v1/messages/([A-Za-z0-9_]{1,256})$"
)
_FEISHU_RECEIVE_ID_TYPES = frozenset(
    {"open_id", "user_id", "union_id", "email", "chat_id"}
)
_MAX_FEISHU_DESCRIPTOR_CONTENT_CHARS = 32768
_FEISHU_AUDIT_HASH_FIELDS = frozenset(
    {
        "event_hash",
        "contract_hash",
        "authorization_evidence_hash",
        "grant_hash",
        "decision_hash",
        "object_ref_hash",
        "authority_subject_hash",
        "actor_hash",
        "route_snapshot_hash",
        "capability_hash",
        "action_hash",
        "result_hash",
        "request_hash",
        "response_hash",
        "legacy_tool_hash",
        "descriptor_hash",
    }
)
_FEISHU_AUDIT_HASH_LIST_FIELDS = frozenset({"evidence_hashes"})
_FEISHU_DELIVERY_LIFECYCLE_HASH_FIELDS = frozenset(
    {
        "correlation_hash",
        "delivery_hash",
        "target_ref_hash",
        "message_ref_hash",
        "route_partition_hash",
        "route_snapshot_hash",
        "contract_hash",
        "original_delivery_hash",
        "bot_ownership_hash",
    }
)
_FEISHU_DELIVERY_LIFECYCLE_ATOM_FIELDS = frozenset(
    {
        "action",
        "evidence_state",
    }
)
_FEISHU_DELIVERY_LIFECYCLE_COMMON_FIELDS = frozenset(
    {"type", "timestamp", "failure_class"}
)
_FEISHU_DELIVERY_ACTIONS = frozenset({"send", "edit"})
_FEISHU_DELIVERY_EVIDENCE_STATES = frozenset(
    {"current", "missing", "stale", "unknown", "denied"}
)
_FEISHU_BROKER_ACTION_KINDS = frozenset({"clarification", "confirmation"})
_FEISHU_BROKER_ACTION_HASH_FIELDS = frozenset(
    {
        "route_partition_hash",
        "route_snapshot_hash",
        "operator_hash",
        "contract_hash",
        "payload_hash",
        "choice_hash",
        "callback_hash",
        "idempotency_key_hash",
    }
)
_FEISHU_BROKER_ACTION_ATOM_FIELDS = frozenset(
    {
        "action_id",
        "grant_handle",
        "action_kind",
    }
)
_FEISHU_BROKER_ACTION_COMMON_FIELDS = frozenset(
    {"type", "timestamp", "expires_at", "failure_class"}
)
_FEISHU_ATTACHMENT_HASH_FIELDS = frozenset(
    {
        "provenance_hash",
        "source_event_hash",
        "file_key_hash",
        "route_partition_hash",
        "route_snapshot_hash",
        "contract_hash",
        "safe_output_root_proof_hash",
        "producing_tool_action_hash",
        "content_hash",
        "delivery_plan_hash",
    }
)
_FEISHU_ATTACHMENT_ATOM_FIELDS = frozenset(
    {
        "provenance_kind",
        "declared_mime_class",
        "size_class",
        "retention_class",
        "retention_state",
        "sensitivity_classification",
        "sensitivity_state",
        "redaction_state",
        "retention_policy",
        "generator_state",
        "source_grant_state",
        "safe_output_root_state",
    }
)
_FEISHU_ATTACHMENT_COMMON_FIELDS = frozenset(
    {"type", "timestamp", "failure_class", "source_grant_handles"}
)
_FEISHU_ATTACHMENT_PROVENANCE_KINDS = frozenset(
    {"inbound_user_attachment", "generated"}
)
_FEISHU_ATTACHMENT_MIME_CLASSES = frozenset(
    {"image", "file", "audio", "media", "document"}
)
_FEISHU_ATTACHMENT_SIZE_CLASSES = frozenset({"small", "medium", "large"})
_FEISHU_ATTACHMENT_RETENTION_CLASSES = frozenset({"ephemeral", "session", "retained"})
_FEISHU_ATTACHMENT_STATES = frozenset({"current", "missing", "stale"})
_FEISHU_ATTACHMENT_REDACTION_STATES = frozenset({"redacted", "not_required"})
_FEISHU_ATTACHMENT_SENSITIVITY_CLASSES = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
_FEISHU_ATTACHMENT_RETENTION_POLICIES = frozenset(
    {"ephemeral", "session", "retained"}
)
_FEISHU_ATTACHMENT_GENERATOR_STATES = frozenset({"complete"})
_FEISHU_AUDIT_ATOM_FIELDS = frozenset(
    {
        "surface",
        "tool",
        "action",
        "scope",
        "decision",
        "capability",
        "object_kind",
        "api",
        "method",
        "outcome",
        "provider_id",
        "provider_version",
        "evidence_source_class",
        "provider_reachability_class",
        "credential_freshness_class",
        "acl_completeness_class",
        "unsupported_scope_status",
        "revocation_reason_class",
        "policy_version",
        "denial_reason_class",
        "evidence_state_class",
        "object_type",
        "expiry",
        "grant_session_class",
    }
)
_FEISHU_AUDIT_COMMON_FIELDS = frozenset(
    {"type", "timestamp", "correlation_id", "failure_class"}
)
_FEISHU_AUDIT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "feishu_authorization_provider_decision": (
        "event_hash",
        "provider_id",
        "provider_version",
        "evidence_source_class",
        "provider_reachability_class",
        "credential_freshness_class",
        "acl_completeness_class",
        "unsupported_scope_status",
        "decision_hash",
        "contract_hash",
        "route_snapshot_hash",
        "object_ref_hash",
        "authority_subject_hash",
        "policy_version",
    ),
    "feishu_authorization_evidence_observed": (
        "event_hash",
        "authorization_evidence_hash",
        "contract_hash",
        "route_snapshot_hash",
        "object_ref_hash",
        "authority_subject_hash",
        "evidence_source_class",
        "evidence_state_class",
        "policy_version",
    ),
    "feishu_authorization_evidence_denied": (
        "event_hash",
        "request_hash",
        "failure_class",
        "denial_reason_class",
        "evidence_source_class",
        "policy_version",
    ),
    "feishu_auth_decision": (
        "event_hash",
        "decision_hash",
        "request_hash",
        "contract_hash",
        "decision",
        "policy_version",
    ),
    "feishu_capability_granted": (
        "event_hash",
        "grant_hash",
        "evidence_hashes",
        "contract_hash",
        "object_type",
        "object_ref_hash",
        "action",
        "authority_subject_hash",
        "expiry",
        "grant_session_class",
        "policy_version",
    ),
    "feishu_capability_denied": (
        "event_hash",
        "request_hash",
        "contract_hash",
        "object_ref_hash",
        "action",
        "failure_class",
        "denial_reason_class",
        "policy_version",
    ),
    "feishu_broker_policy_denied": (
        "event_hash",
        "request_hash",
        "contract_hash",
        "route_snapshot_hash",
        "object_ref_hash",
        "authority_subject_hash",
        "action",
        "failure_class",
        "denial_reason_class",
        "policy_version",
    ),
}
_FEISHU_AUDIT_RAW_FIELD_NAMES = frozenset(
    {
        "token",
        "secret",
        "body",
        "raw_body",
        "document_content",
        "file_path",
        "api_response",
        "open_id",
        "user_id",
        "union_id",
        "message_id",
        "file_id",
        "path",
        "content",
        "object_ref",
        "raw_acl_json",
        "raw_openapi_body",
        "raw_document_content",
        "raw_message_body",
        "app_token",
        "user_token",
        "chat_id",
        "document_token",
        "file_token",
        "comment_id",
        "local_path",
    }
)
_FEISHU_AUDIT_SENSITIVE_VALUE_TOKENS = frozenset(
    {
        "access_token",
        "tenant_access_token",
        "token",
        "secret",
        "raw_body",
        "raw_response",
        "api_response",
        "document_content",
        "body",
        "content",
        "path",
        "response",
    }
)
_FEISHU_AUDIT_NEGATIVE_DECISIONS = frozenset(
    {
        "denied",
        "deny",
        "failure",
        "failed",
        "rejected",
        "unauthorized",
        "forbidden",
        "blocked",
        "not_allowed",
    }
)
_FEISHU_AUDIT_SAFE_CLASS_VALUES = frozenset({"one_time"})


class GatewayEventContractError(ValueError):
    def __init__(self, failure_class: str, reason: str):
        super().__init__(reason)
        self.failure_class = failure_class
        self.reason = reason


def event_type_from(event: Mapping[str, Any]) -> str | None:
    event_type = event.get("type")
    if isinstance(event_type, str) and _SAFE_EVENT_TYPE_RE.fullmatch(event_type):
        return event_type
    return None


def validate_gateway_event(event: Mapping[str, Any]) -> str:
    if not isinstance(event, Mapping):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "event must be a mapping",
        )
    event_type = event_type_from(event)
    if event_type is None:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "event type is missing or invalid",
        )
    required = EVENT_REQUIREMENTS.get(event_type)
    if required is None and event_type in FEISHU_AUDIT_EVENT_TYPES:
        _validate_feishu_audit_event(event_type, event)
        return event_type
    if required is None and event_type in FEISHU_DELIVERY_LIFECYCLE_EVENT_TYPES:
        _validate_feishu_delivery_lifecycle_event(event_type, event)
        return event_type
    if required is None and event_type in FEISHU_BROKER_ACTION_LIFECYCLE_EVENT_TYPES:
        _validate_feishu_broker_action_lifecycle_event(event_type, event)
        return event_type
    if required is None and event_type in FEISHU_ATTACHMENT_PROVENANCE_EVENT_TYPES:
        _validate_feishu_attachment_provenance_event(event_type, event)
        return event_type
    if required is None:
        raise GatewayEventContractError(
            "unsupported_gateway_event_type",
            "unsupported gateway event type",
        )
    for field in required:
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    if event_type == "stale_pending_scan":
        _require_number(event, "now")
        _require_number(event, "max_age_seconds", minimum=0)
        return event_type
    if event_type in {"session_locked", "compression_result"}:
        for field in required:
            _require_session_route_value(event, field)
        return event_type
    if event_type == "delivery_sent":
        _require_nonempty_string(event, "delivery_id")
        _require_feishu_message_id(event, "message_id")
        _require_number(event, "timestamp")
        return event_type
    if event_type == "unknown_delivery_state" and "message_id" in event:
        _require_feishu_message_id(event, "message_id")
    if event_type == "feishu_ack":
        _require_feishu_message_id(event, "message_id")
        _require_feishu_event_id(event, "ack_event_id")
        _require_number(event, "timestamp")
        return event_type
    if event_type == "feishu_inbound" and "idempotency_evidence_state" in event:
        if event.get("idempotency_evidence_state") not in FEISHU_INBOUND_IDEMPOTENCY_EVIDENCE_STATES:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "invalid Feishu inbound idempotency evidence state",
            )
    for field in required:
        if field == "timestamp":
            _require_number(event, field)
        elif field == "failure_class":
            require_failure_class(event.get(field))
        else:
            _require_nonempty_string(event, field)
    return event_type


def require_failure_class(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_FAILURE_CLASS_RE.fullmatch(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "failure_class is missing or invalid",
        )
    return value


def validate_gateway_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError("action must be a mapping")
    action_type = action.get("type")
    if action_type == "inbound_admission":
        return _validate_inbound_admission(action)
    if action_type == "delivery_record":
        return _validate_delivery_record_action(action)
    if action_type == "stale_pending_alert":
        return _validate_stale_pending_alert(action)
    if action_type == "session_route":
        return _validate_session_route_action(action)
    if action_type == "compression_record":
        return _validate_compression_record_action(action)
    if action_type == "preflight":
        return _validate_preflight_action(action)
    if action_type == "feishu_audit_event_record":
        return _validate_feishu_audit_event_record_action(action)
    if action_type == "feishu_broker_action_record":
        return _validate_feishu_broker_action_record_action(action)
    if action_type == "feishu_attachment_provenance_record":
        return _validate_feishu_attachment_provenance_record_action(action)
    if action_type == "feishu_attachment_denial_record":
        return _validate_feishu_attachment_denial_record_action(action)
    raise ValueError("unsupported gateway action type")


def validate_feishu_request_descriptor(
    descriptor: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(descriptor, Mapping):
        return None
    operation = descriptor.get("operation")
    if operation == "send_interactive_message":
        return _validate_send_interactive_message_descriptor(descriptor)
    if operation == "patch_interactive_message":
        return _validate_patch_interactive_message_descriptor(descriptor)
    return None


def _validate_feishu_broker_action_lifecycle_event(
    event_type: str, event: Mapping[str, Any]
) -> None:
    allowed_fields = (
        _FEISHU_BROKER_ACTION_COMMON_FIELDS
        | _FEISHU_BROKER_ACTION_HASH_FIELDS
        | _FEISHU_BROKER_ACTION_ATOM_FIELDS
    )
    for field, value in event.items():
        if not isinstance(field, str) or field not in allowed_fields:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu broker action lifecycle event contains unsupported field",
            )
        if _is_feishu_audit_raw_field(field):
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu broker action lifecycle event contains raw field",
            )
        if field in _FEISHU_BROKER_ACTION_HASH_FIELDS:
            _require_sanitized_hash_value(value, field)
        elif field == "action_id":
            _require_broker_action_id(value)
        elif field == "grant_handle":
            _require_broker_grant_handle(value)
        elif field == "action_kind" and value not in _FEISHU_BROKER_ACTION_KINDS:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "invalid Feishu broker action kind",
            )

    if "timestamp" not in event:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "missing required field: timestamp",
        )
    _require_number(event, "timestamp")

    if event_type == "feishu_broker_action_created":
        required_fields = (
            "action_id",
            "grant_handle",
            "action_kind",
            "route_partition_hash",
            "route_snapshot_hash",
            "operator_hash",
            "contract_hash",
            "payload_hash",
            "expires_at",
            "idempotency_key_hash",
        )
    elif event_type in {
        "feishu_broker_action_accepted",
        "feishu_broker_action_resolved",
        "feishu_broker_action_replayed",
    }:
        required_fields = (
            "action_id",
            "action_kind",
            "route_partition_hash",
            "route_snapshot_hash",
            "operator_hash",
            "contract_hash",
            "payload_hash",
            "callback_hash",
        )
    else:
        required_fields = ("callback_hash", "failure_class")

    for field in required_fields:
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    if event_type == "feishu_broker_action_resolved" and "choice_hash" not in event:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "missing required field: choice_hash",
        )
    if event_type == "feishu_broker_action_denied":
        require_failure_class(event.get("failure_class"))
    elif "failure_class" in event:
        require_failure_class(event.get("failure_class"))
    if "expires_at" in event:
        _require_number(event, "expires_at", minimum=0)


def _validate_send_interactive_message_descriptor(
    descriptor: Mapping[str, Any],
) -> dict[str, Any] | None:
    if set(descriptor) != {"operation", "method", "path", "params", "body"}:
        return None
    if descriptor.get("method") != "POST":
        return None
    if descriptor.get("path") != "/open-apis/im/v1/messages":
        return None

    params = descriptor.get("params")
    if not isinstance(params, Mapping) or set(params) != {"receive_id_type"}:
        return None
    receive_id_type = params.get("receive_id_type")
    if receive_id_type not in _FEISHU_RECEIVE_ID_TYPES:
        return None

    body = descriptor.get("body")
    if not isinstance(body, Mapping):
        return None
    body_keys = set(body)
    if body_keys not in (
        {"receive_id", "msg_type", "content"},
        {"receive_id", "msg_type", "content", "uuid"},
    ):
        return None
    if not _is_safe_feishu_receive_id(body.get("receive_id")):
        return None
    if body.get("msg_type") != "interactive":
        return None
    content = body.get("content")
    if not _is_nonempty_json_object_string(content):
        return None
    if "uuid" in body and not _is_safe_descriptor_uuid(body.get("uuid")):
        return None

    validated_body = {
        "receive_id": body["receive_id"],
        "msg_type": "interactive",
        "content": content,
    }
    if "uuid" in body:
        validated_body["uuid"] = body["uuid"]
    return {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": receive_id_type},
        "body": validated_body,
    }


def _validate_patch_interactive_message_descriptor(
    descriptor: Mapping[str, Any],
) -> dict[str, Any] | None:
    if set(descriptor) != {"operation", "method", "path", "params", "body"}:
        return None
    if descriptor.get("method") != "PATCH":
        return None
    path = descriptor.get("path")
    if not isinstance(path, str):
        return None
    path_match = _FEISHU_PATCH_MESSAGE_PATH_RE.fullmatch(path)
    if path_match is None or not _SAFE_FEISHU_MESSAGE_ID_RE.fullmatch(path_match.group(1)):
        return None
    params = descriptor.get("params")
    if not isinstance(params, Mapping) or set(params) != set():
        return None
    body = descriptor.get("body")
    if not isinstance(body, Mapping) or set(body) != {"content"}:
        return None
    content = body.get("content")
    if not _is_nonempty_json_object_string(content):
        return None
    return {
        "operation": "patch_interactive_message",
        "method": "PATCH",
        "path": path,
        "params": {},
        "body": {"content": content},
    }


def _validate_inbound_admission(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "decision", "duplicate", "record"}:
        raise ValueError("invalid inbound admission action keys")
    if action.get("decision") not in {
        "continue",
        "feishu_inbound_duplicate",
    } or not isinstance(action.get("duplicate"), bool):
        raise ValueError("invalid inbound admission action")
    record = action.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("invalid inbound admission record")
    legacy_keys = {
        "inbound_id_hash",
        "message_id_hash",
        "message_type",
        "first_seen_at",
    }
    current_keys = legacy_keys | {
        "canonical_event_ref",
        "route_partition_hash",
        "contract_hash",
        "transport_kind",
    }
    record_keys = set(record)
    if record_keys != legacy_keys and record_keys != current_keys:
        raise ValueError("invalid inbound admission record keys")
    if not isinstance(record.get("inbound_id_hash"), str) or not _FNV1A64_RE.fullmatch(
        record["inbound_id_hash"]
    ):
        raise ValueError("invalid inbound id hash")
    if not isinstance(record.get("message_id_hash"), str) or not _FNV1A64_RE.fullmatch(
        record["message_id_hash"]
    ):
        raise ValueError("invalid message id hash")
    if not isinstance(record.get("message_type"), str) or not record["message_type"]:
        raise ValueError("invalid message type")
    if not _is_number(record.get("first_seen_at")):
        raise ValueError("invalid first_seen_at")
    if record_keys == current_keys:
        if not isinstance(record.get("canonical_event_ref"), str) or not _SHA256_RE.fullmatch(
            record["canonical_event_ref"]
        ):
            raise ValueError("invalid canonical event ref")
        if not isinstance(record.get("route_partition_hash"), str) or not _SHA256_RE.fullmatch(
            record["route_partition_hash"]
        ):
            raise ValueError("invalid route partition hash")
        if not isinstance(record.get("contract_hash"), str) or not _SHA256_RE.fullmatch(
            record["contract_hash"]
        ):
            raise ValueError("invalid contract hash")
        if record.get("transport_kind") not in FEISHU_INBOUND_TRANSPORT_KINDS:
            raise ValueError("invalid transport kind")
    return {
        "type": "inbound_admission",
        "decision": action["decision"],
        "duplicate": action["duplicate"],
        "record": dict(record),
    }


def _validate_delivery_record_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid delivery action keys")
    record = _validate_delivery_record(action.get("record"))
    return {"type": "delivery_record", "record": record}


def _validate_delivery_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid delivery record")
    keys = {
        "delivery_id",
        "inbound_id",
        "target",
        "session_id",
        "correlation_id",
        "status",
        "created_at",
        "updated_at",
        "feishu_message_id",
        "failure_class",
        "ack_event_id",
    }
    if set(value) != keys:
        raise ValueError("invalid delivery record keys")
    status = value.get("status")
    if status not in {"pending", "sent", "failed", "acked", "unknown"}:
        raise ValueError("invalid delivery status")
    failure_class = value.get("failure_class")
    if status in {"failed", "unknown"} and failure_class is None:
        raise ValueError("non-success delivery state requires failure_class")
    if status in {"pending", "sent", "acked"} and failure_class is not None:
        raise ValueError("successful delivery state cannot have failure_class")
    for field in ("delivery_id", "status"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"invalid {field}")
    for field in ("created_at", "updated_at"):
        if not _is_number(value.get(field)):
            raise ValueError(f"invalid {field}")
    for field in (
        "inbound_id",
        "target",
        "session_id",
        "correlation_id",
        "feishu_message_id",
        "failure_class",
        "ack_event_id",
    ):
        if value.get(field) is not None and not isinstance(value.get(field), str):
            raise ValueError(f"invalid {field}")
    if failure_class is not None:
        require_failure_class(failure_class)
    return dict(value)


def _validate_feishu_broker_action_record_action(
    action: Mapping[str, Any]
) -> dict[str, Any]:
    if set(action) != {"type", "record", "outcome"}:
        raise ValueError("invalid Feishu broker action record action keys")
    record = _validate_feishu_broker_action_record(action.get("record"))
    outcome = action.get("outcome")
    if outcome not in {
        "created",
        "accepted",
        "resolved",
        "denied",
        "replayed",
        "duplicate",
    }:
        raise ValueError("invalid Feishu broker action outcome")
    return {
        "type": "feishu_broker_action_record",
        "record": record,
        "outcome": outcome,
    }


def _validate_feishu_broker_action_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid Feishu broker action record")
    required_keys = {
        "action_id",
        "grant_handle",
        "action_kind",
        "route_partition_hash",
        "route_snapshot_hash",
        "operator_hash",
        "contract_hash",
        "payload_hash",
        "expires_at",
        "idempotency_key_hash",
        "status",
        "created_at",
        "accepted_at",
        "resolved_at",
        "replayed_at",
    }
    if set(value) != required_keys:
        raise ValueError("invalid Feishu broker action record keys")
    _require_broker_action_id(value.get("action_id"))
    _require_broker_grant_handle(value.get("grant_handle"))
    if value.get("action_kind") not in _FEISHU_BROKER_ACTION_KINDS:
        raise ValueError("invalid Feishu broker action kind")
    for field in (
        "route_partition_hash",
        "route_snapshot_hash",
        "operator_hash",
        "contract_hash",
        "payload_hash",
        "idempotency_key_hash",
    ):
        _require_sanitized_hash_value(value.get(field), field)
    if value.get("status") not in {"created", "accepted", "resolved"}:
        raise ValueError("invalid Feishu broker action status")
    for field in ("expires_at", "created_at"):
        if not _is_number(value.get(field)):
            raise ValueError(f"invalid Feishu broker action {field}")
    for field in ("accepted_at", "resolved_at", "replayed_at"):
        if value.get(field) is not None and not _is_number(value.get(field)):
            raise ValueError(f"invalid Feishu broker action {field}")
    return dict(value)


def _validate_feishu_attachment_provenance_record_action(
    action: Mapping[str, Any]
) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid Feishu attachment provenance action keys")
    record = _validate_feishu_attachment_provenance_record(action.get("record"))
    return {"type": "feishu_attachment_provenance_record", "record": record}


def _validate_feishu_attachment_denial_record_action(
    action: Mapping[str, Any]
) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid Feishu attachment denial action keys")
    record = action.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("invalid Feishu attachment denial record")
    if set(record) != {
        "provenance_hash",
        "failure_class",
        "route_partition_hash",
        "contract_hash",
        "declared_mime_class",
        "size_class",
        "timestamp",
    }:
        raise ValueError("invalid Feishu attachment denial record keys")
    _require_sanitized_hash_value(record.get("provenance_hash"), "provenance_hash")
    require_failure_class(record.get("failure_class"))
    _require_sanitized_hash_value(
        record.get("route_partition_hash"), "route_partition_hash"
    )
    _require_sanitized_hash_value(record.get("contract_hash"), "contract_hash")
    if record.get("declared_mime_class") not in _FEISHU_ATTACHMENT_MIME_CLASSES:
        raise ValueError("invalid Feishu attachment denial MIME class")
    if record.get("size_class") not in _FEISHU_ATTACHMENT_SIZE_CLASSES:
        raise ValueError("invalid Feishu attachment denial size class")
    if not _is_number(record.get("timestamp")):
        raise ValueError("invalid Feishu attachment denial timestamp")
    return {"type": "feishu_attachment_denial_record", "record": dict(record)}


def _validate_feishu_attachment_provenance_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid Feishu attachment provenance record")
    event_type = event_type_from(value)
    if event_type != "feishu_attachment_provenance_recorded":
        raise ValueError("invalid Feishu attachment provenance record type")
    try:
        _validate_feishu_attachment_provenance_event(event_type, value)
    except GatewayEventContractError as exc:
        raise ValueError("invalid Feishu attachment provenance record") from exc
    if "provenance_hash" not in value:
        raise ValueError("missing Feishu attachment provenance hash")
    return dict(value)


def _validate_stale_pending_alert(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "alert_required", "resend_permitted", "count", "records"}:
        raise ValueError("invalid stale pending action keys")
    if not isinstance(action.get("alert_required"), bool):
        raise ValueError("invalid stale pending alert_required")
    if action.get("resend_permitted") is not False:
        raise ValueError("stale pending resend is not permitted")
    if not isinstance(action.get("count"), int) or action["count"] < 0:
        raise ValueError("invalid stale pending count")
    records = action.get("records")
    if not isinstance(records, list) or len(records) != action["count"]:
        raise ValueError("invalid stale pending records")
    validated_records = [_validate_delivery_record(record) for record in records]
    for record in validated_records:
        if record["status"] not in {"pending", "unknown"}:
            raise ValueError("stale pending record has invalid status")
    return {
        "type": "stale_pending_alert",
        "alert_required": action["alert_required"],
        "resend_permitted": False,
        "count": action["count"],
        "records": validated_records,
    }


def _validate_session_route_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid session route action keys")
    record = _validate_session_route_record(action.get("record"))
    return {"type": "session_route", "record": record}


def _validate_session_route_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid session route record")
    if set(value) != {"session_key", "session_id", "correlation_id"}:
        raise ValueError("invalid session route record keys")
    for field in ("session_key", "session_id", "correlation_id"):
        if not _is_safe_session_route_value(value.get(field)):
            raise ValueError(f"invalid {field}")
    return {
        "session_key": value["session_key"],
        "session_id": value["session_id"],
        "correlation_id": value["correlation_id"],
    }


def _validate_compression_record_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid compression action keys")
    record = _validate_compression_record(action.get("record"))
    return {"type": "compression_record", "record": record}


def _validate_compression_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid compression record")
    keys = {
        "session_key",
        "locked_session_id",
        "observed_session_id",
        "correlation_id",
        "status",
        "failure_class",
    }
    if set(value) != keys:
        raise ValueError("invalid compression record keys")
    for field in ("session_key", "locked_session_id", "observed_session_id", "correlation_id"):
        if not _is_safe_session_route_value(value.get(field)):
            raise ValueError(f"invalid {field}")
    status = value.get("status")
    if status not in {"accepted", "rejected"}:
        raise ValueError("invalid compression status")
    failure_class = value.get("failure_class")
    if failure_class is not None:
        require_failure_class(failure_class)
    if status == "accepted" and failure_class is not None:
        raise ValueError("accepted compression cannot have failure_class")
    if status == "rejected" and failure_class != "implicit_session_switch":
        raise ValueError("invalid compression rejection failure_class")
    return {
        "session_key": value["session_key"],
        "locked_session_id": value["locked_session_id"],
        "observed_session_id": value["observed_session_id"],
        "correlation_id": value["correlation_id"],
        "status": status,
        "failure_class": failure_class,
    }


def _validate_preflight_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "checks"}:
        raise ValueError("invalid preflight action keys")
    checks = action.get("checks")
    if not isinstance(checks, list):
        raise ValueError("invalid preflight checks")
    validated = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {"name", "ok", "detail"}:
            raise ValueError("invalid preflight check")
        name = check.get("name")
        if name not in PREFLIGHT_CHECK_NAMES:
            raise ValueError("unsupported preflight check")
        if not isinstance(check.get("ok"), bool):
            raise ValueError("invalid preflight check ok")
        detail = check.get("detail")
        if not isinstance(detail, str):
            raise ValueError("invalid preflight check detail")
        validated.append({"name": name, "ok": check["ok"], "detail": detail})
    return {"type": "preflight", "checks": validated}


def _validate_feishu_audit_event_record_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if set(action) != {"type", "record"}:
        raise ValueError("invalid feishu audit action keys")
    record = action.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("invalid feishu audit record")
    event_type = event_type_from(record)
    if event_type is None or event_type not in (
        FEISHU_AUDIT_EVENT_TYPES | FEISHU_DELIVERY_LIFECYCLE_EVENT_TYPES
    ):
        raise ValueError("invalid feishu audit event type")
    try:
        if event_type in FEISHU_AUDIT_EVENT_TYPES:
            _validate_feishu_audit_event(event_type, record)
        else:
            _validate_feishu_delivery_lifecycle_event(event_type, record)
    except GatewayEventContractError as exc:
        raise ValueError("invalid feishu audit record") from exc
    return {"type": "feishu_audit_event_record", "record": dict(record)}


def _validate_feishu_attachment_provenance_event(
    event_type: str, event: Mapping[str, Any]
) -> None:
    allowed_fields = (
        _FEISHU_ATTACHMENT_COMMON_FIELDS
        | _FEISHU_ATTACHMENT_HASH_FIELDS
        | _FEISHU_ATTACHMENT_ATOM_FIELDS
    )
    for field, value in event.items():
        if not isinstance(field, str) or field not in allowed_fields:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu attachment provenance event contains unsupported field",
            )
        if _is_feishu_audit_raw_field(field):
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu attachment provenance event contains raw field",
            )
        if field in _FEISHU_ATTACHMENT_HASH_FIELDS:
            _require_sanitized_hash_value(value, field)

    _require_number(event, "timestamp")
    if event_type == "feishu_attachment_upload_denied":
        for field in (
            "provenance_hash",
            "failure_class",
            "route_partition_hash",
            "contract_hash",
            "declared_mime_class",
            "size_class",
        ):
            if field not in event:
                raise GatewayEventContractError(
                    "invalid_gateway_event_contract",
                    f"missing required field: {field}",
                )
        require_failure_class(event.get("failure_class"))
        if event.get("declared_mime_class") not in _FEISHU_ATTACHMENT_MIME_CLASSES:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "invalid Feishu attachment MIME class",
            )
        if event.get("size_class") not in _FEISHU_ATTACHMENT_SIZE_CLASSES:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "invalid Feishu attachment size class",
            )
        return

    if event.get("provenance_kind") not in _FEISHU_ATTACHMENT_PROVENANCE_KINDS:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment provenance kind",
        )
    for field in (
        "declared_mime_class",
        "size_class",
        "route_partition_hash",
        "route_snapshot_hash",
        "contract_hash",
        "sensitivity_classification",
        "sensitivity_state",
        "redaction_state",
        "retention_state",
    ):
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    if event.get("declared_mime_class") not in _FEISHU_ATTACHMENT_MIME_CLASSES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment MIME class",
        )
    if event.get("size_class") not in _FEISHU_ATTACHMENT_SIZE_CLASSES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment size class",
        )
    if event.get("retention_state") not in _FEISHU_ATTACHMENT_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment retention state",
        )
    if event.get("sensitivity_state") not in _FEISHU_ATTACHMENT_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment sensitivity state",
        )
    if (
        event.get("sensitivity_classification")
        not in _FEISHU_ATTACHMENT_SENSITIVITY_CLASSES
    ):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment sensitivity classification",
        )
    if event.get("redaction_state") not in _FEISHU_ATTACHMENT_REDACTION_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment redaction state",
        )

    if event.get("provenance_kind") == "inbound_user_attachment":
        for field in ("source_event_hash", "file_key_hash", "retention_class"):
            if field not in event:
                raise GatewayEventContractError(
                    "invalid_gateway_event_contract",
                    f"missing required field: {field}",
                )
        if event.get("retention_class") not in _FEISHU_ATTACHMENT_RETENTION_CLASSES:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "invalid Feishu attachment retention class",
            )
        return

    for field in (
        "safe_output_root_proof_hash",
        "producing_tool_action_hash",
        "content_hash",
        "delivery_plan_hash",
        "retention_policy",
        "generator_state",
        "source_grant_handles",
    ):
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    if "safe_output_root_state" in event and event.get("safe_output_root_state") not in _FEISHU_ATTACHMENT_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment safe-root state",
        )
    if "source_grant_state" in event and event.get("source_grant_state") not in _FEISHU_ATTACHMENT_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment source grant state",
        )
    if event.get("retention_policy") not in _FEISHU_ATTACHMENT_RETENTION_POLICIES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment retention policy",
        )
    if event.get("generator_state") not in _FEISHU_ATTACHMENT_GENERATOR_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu attachment generator state",
        )
    _require_broker_grant_handle_list(event.get("source_grant_handles"))


def _validate_feishu_audit_event(event_type: str, event: Mapping[str, Any]) -> None:
    allowed_fields = (
        _FEISHU_AUDIT_COMMON_FIELDS
        | _FEISHU_AUDIT_HASH_FIELDS
        | _FEISHU_AUDIT_HASH_LIST_FIELDS
        | _FEISHU_AUDIT_ATOM_FIELDS
    )
    hash_fields_present = []
    for field, value in event.items():
        if not isinstance(field, str) or field not in allowed_fields:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu audit event contains unsupported field",
            )
        if _is_feishu_audit_raw_field(field):
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu audit event contains raw field",
            )
        if field in _FEISHU_AUDIT_HASH_FIELDS:
            _require_sanitized_hash_value(value, field)
            hash_fields_present.append(field)
        elif field in _FEISHU_AUDIT_HASH_LIST_FIELDS:
            _require_sanitized_hash_list(value, field)
            hash_fields_present.append(field)
        elif field in _FEISHU_AUDIT_ATOM_FIELDS:
            _require_safe_audit_atom(value, field)
            _reject_sensitive_audit_value(value)

    for field in ("timestamp", "correlation_id"):
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    _require_number(event, "timestamp")
    _require_session_route_value(event, "correlation_id")
    if not hash_fields_present:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "feishu audit event requires a sanitized hash field",
        )
    for field in _FEISHU_AUDIT_REQUIRED_FIELDS.get(event_type, ()):
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    if "denial_reason_class" in event:
        _require_feishu_audit_class_value(
            event.get("denial_reason_class"),
            "denial_reason_class",
        )
    provider_decision_denial = _feishu_provider_decision_is_denial(event) or (
        event_type == "feishu_authorization_provider_decision"
        and "failure_class" in event
    )
    if provider_decision_denial:
        _require_feishu_audit_class_value(event.get("failure_class"), "failure_class")
        _require_feishu_audit_class_value(
            event.get("denial_reason_class"),
            "denial_reason_class",
        )
    if event_type == "feishu_authorization_provider_decision":
        if event.get("credential_freshness_class") == "revoked" and (
            "revocation_reason_class" not in event
        ):
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "missing required field: revocation_reason_class",
            )
    if _feishu_audit_event_requires_failure_class(event_type, event):
        _require_feishu_audit_class_value(event.get("failure_class"), "failure_class")
    elif "failure_class" in event:
        _require_feishu_audit_class_value(event.get("failure_class"), "failure_class")


def _validate_feishu_delivery_lifecycle_event(
    event_type: str, event: Mapping[str, Any]
) -> None:
    allowed_fields = (
        _FEISHU_DELIVERY_LIFECYCLE_COMMON_FIELDS
        | _FEISHU_DELIVERY_LIFECYCLE_HASH_FIELDS
        | _FEISHU_DELIVERY_LIFECYCLE_ATOM_FIELDS
    )
    for field, value in event.items():
        if not isinstance(field, str) or field not in allowed_fields:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu delivery lifecycle event contains unsupported field",
            )
        if _is_feishu_audit_raw_field(field):
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "feishu delivery lifecycle event contains raw field",
            )
        if field in _FEISHU_DELIVERY_LIFECYCLE_HASH_FIELDS:
            _require_sanitized_hash_value(value, field)

    for field in (
        "timestamp",
        "correlation_hash",
        "delivery_hash",
        "target_ref_hash",
        "action",
        "evidence_state",
    ):
        if field not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                f"missing required field: {field}",
            )
    _require_number(event, "timestamp")
    if event.get("action") not in _FEISHU_DELIVERY_ACTIONS:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu delivery lifecycle action",
        )
    if event.get("evidence_state") not in _FEISHU_DELIVERY_EVIDENCE_STATES:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "invalid Feishu delivery lifecycle evidence state",
        )
    if event_type in {"feishu_delivery_failed", "feishu_delivery_ack_unknown"}:
        require_failure_class(event.get("failure_class"))
    elif "failure_class" in event:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "successful Feishu delivery lifecycle event cannot have failure_class",
        )
    if event_type in {"feishu_delivery_sent", "feishu_delivery_ack_unknown"}:
        for field in ("message_ref_hash", "route_partition_hash", "contract_hash"):
            if field not in event:
                raise GatewayEventContractError(
                    "invalid_gateway_event_contract",
                    f"missing required field: {field}",
                )
    if event_type == "feishu_delivery_sent" and "bot_ownership_hash" not in event:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "missing required field: bot_ownership_hash",
        )
    if event.get("action") == "edit" and event_type == "feishu_delivery_sent":
        if "original_delivery_hash" not in event:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "missing required field: original_delivery_hash",
            )


def _feishu_audit_event_requires_failure_class(
    event_type: str, event: Mapping[str, Any]
) -> bool:
    if event_type in FEISHU_AUDIT_FAILURE_EVENT_TYPES:
        return True
    if event_type != "feishu_auth_decision":
        return False
    decision = event.get("decision")
    return isinstance(decision, str) and decision.lower() in _FEISHU_AUDIT_NEGATIVE_DECISIONS


def _feishu_provider_decision_is_denial(event: Mapping[str, Any]) -> bool:
    if event.get("type") != "feishu_authorization_provider_decision":
        return False
    return (
        event.get("provider_reachability_class") != "reachable"
        or event.get("credential_freshness_class") != "fresh"
        or event.get("acl_completeness_class") != "complete"
        or event.get("unsupported_scope_status") != "none"
    )


def _is_feishu_audit_raw_field(field: str) -> bool:
    normalized = field.lower()
    if normalized in _FEISHU_AUDIT_RAW_FIELD_NAMES:
        return True
    if normalized.endswith("_hash"):
        return False
    parts = normalized.split("_")
    return "token" in parts or "secret" in parts


def _require_sanitized_hash_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not (
        _FNV1A64_RE.fullmatch(value) or _SHA256_RE.fullmatch(value)
        or (
            field == "route_snapshot_hash"
            and _ROUTE_SNAPSHOT_AUDIT_HASH_RE.fullmatch(value)
        )
    ):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _require_sanitized_hash_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return tuple(_require_sanitized_hash_value(item, field) for item in value)


def _require_broker_action_id(value: Any) -> str:
    if not isinstance(value, str) or _BROKER_ACTION_ID_RE.fullmatch(value) is None:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "action_id is missing or invalid",
        )
    return value


def _require_broker_grant_handle(value: Any) -> str:
    if not isinstance(value, str) or _BROKER_GRANT_HANDLE_RE.fullmatch(value) is None:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "grant_handle is missing or invalid",
        )
    return value


def _require_broker_grant_handle_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "source_grant_handles is missing or invalid",
        )
    handles = []
    for item in value:
        handles.append(_require_broker_grant_handle(item))
    return tuple(handles)


def _require_safe_audit_atom(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_AUDIT_ATOM_RE.fullmatch(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _require_feishu_audit_class_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_FAILURE_CLASS_RE.fullmatch(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    _reject_sensitive_audit_value(value)
    return value


def _reject_sensitive_audit_value(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str) or _is_feishu_audit_raw_field(key):
                raise GatewayEventContractError(
                    "invalid_gateway_event_contract",
                    "feishu audit event contains sensitive value",
                )
            _reject_sensitive_audit_value(nested_value)
        return
    if isinstance(value, list):
        for item in value:
            _reject_sensitive_audit_value(item)
        return
    if not isinstance(value, str):
        return
    normalized = value.strip().lower()
    if normalized in _FEISHU_AUDIT_SAFE_CLASS_VALUES:
        return
    tokenized = normalized.replace("-", "_").replace(".", "_").replace(":", "_")
    if (
        normalized.startswith("/")
        or "/" in normalized
        or "open_apis" in tokenized
        or _RAW_FEISHU_ID_VALUE_RE.fullmatch(normalized) is not None
        or tokenized in _FEISHU_AUDIT_SENSITIVE_VALUE_TOKENS
        or any(
            token in tokenized
            for token in ("access_token", "api_response", "raw_response")
        )
    ):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            "feishu audit event contains sensitive value",
        )


def _require_nonempty_string(event: Mapping[str, Any], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _require_session_route_value(event: Mapping[str, Any], field: str) -> str:
    value = event.get(field)
    if not _is_safe_session_route_value(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _require_feishu_message_id(event: Mapping[str, Any], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not _SAFE_FEISHU_MESSAGE_ID_RE.fullmatch(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _require_feishu_event_id(event: Mapping[str, Any], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not _SAFE_FEISHU_EVENT_ID_RE.fullmatch(value):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_safe_session_route_value(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_SESSION_ROUTE_VALUE_RE.fullmatch(value))


def _is_safe_descriptor_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_DESCRIPTOR_UUID_RE.fullmatch(value))


def _is_safe_feishu_receive_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_FEISHU_RECEIVE_ID_RE.fullmatch(value))


def _is_nonempty_json_object_string(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_FEISHU_DESCRIPTOR_CONTENT_CHARS:
        return False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed)


def _require_number(
    event: Mapping[str, Any], field: str, *, minimum: int | float | None = None
) -> int | float:
    value = event.get(field)
    if not _is_number(value) or (minimum is not None and value < minimum):
        raise GatewayEventContractError(
            "invalid_gateway_event_contract",
            f"{field} is missing or invalid",
        )
    return value


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )

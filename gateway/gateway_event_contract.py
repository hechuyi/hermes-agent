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
_SAFE_FEISHU_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,256}$")
_SAFE_FEISHU_RECEIVE_ID_RE = re.compile(r"^[A-Za-z0-9_@.+-]{1,256}$")
_SAFE_DESCRIPTOR_UUID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,127}$")
_SAFE_SESSION_ROUTE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,256}$")
_FEISHU_PATCH_MESSAGE_PATH_RE = re.compile(
    r"^/open-apis/im/v1/messages/([A-Za-z0-9_]{1,256})$"
)
_FEISHU_RECEIVE_ID_TYPES = frozenset(
    {"open_id", "user_id", "union_id", "email", "chat_id"}
)
_MAX_FEISHU_DESCRIPTOR_CONTENT_CHARS = 32768


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
    if action.get("decision") != "continue" or not isinstance(action.get("duplicate"), bool):
        raise ValueError("invalid inbound admission action")
    record = action.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("invalid inbound admission record")
    if set(record) != {
        "inbound_id_hash",
        "message_id_hash",
        "message_type",
        "first_seen_at",
    }:
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
    return {"type": "inbound_admission", "decision": "continue", "duplicate": action["duplicate"], "record": dict(record)}


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
    if status == "unknown" and value.get("failure_class") is None:
        raise ValueError("unknown delivery state requires failure_class")
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
    if value.get("failure_class") is not None:
        require_failure_class(value["failure_class"])
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

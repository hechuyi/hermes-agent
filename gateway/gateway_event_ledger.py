"""Internal JSON ledger implementation for Hermes gateway events."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from gateway.gateway_event_contract import (
    FEISHU_AUDIT_EVENT_TYPES,
    FEISHU_BROKER_ACTION_LIFECYCLE_EVENT_TYPES,
    FEISHU_DELIVERY_LIFECYCLE_EVENT_TYPES,
    GatewayEventContractError,
    GatewayEventResult,
    PREFLIGHT_CHECK_NAMES,
    event_type_from,
    validate_gateway_action,
    validate_gateway_event,
)


LEDGER_FILENAME = "gateway_event_ledger.json"
LOCK_FILENAME = ".gateway_event_ledger.lock"
_DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_MAX_COMPRESSION_REJECTIONS = 1000
_STATE_DICT_SECTIONS: tuple[str, ...] = (
    "inbounds",
    "deliveries",
    "delivery_identity_index",
    "feishu_message_index",
    "ack_event_index",
    "session_routes",
    "feishu_broker_actions",
)
_STATE_LIST_SECTIONS: tuple[str, ...] = (
    "compression_rejections",
    "feishu_audit_events",
    "feishu_delivery_lifecycle",
    "feishu_broker_action_lifecycle",
)
_IS_WINDOWS = os.name == "nt"
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


class _GatewayEventPersistedFailure(Exception):
    def __init__(self, failure_class: str, reason: str):
        super().__init__(reason)
        self.failure_class = failure_class
        self.reason = reason


def apply_gateway_event(
    event: Mapping[str, Any],
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
    binary: str | None = None,
) -> GatewayEventResult:
    del binary
    event_type = _safe_event_type(event)
    lock_timeout = _lock_timeout_seconds(timeout_seconds)
    try:
        validated_event_type = validate_gateway_event(event)
        with _state_lock(state_dir, lock_timeout):
            state_path = _state_path(state_dir)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = _read_state(state_path)
            try:
                action = _apply_validated_event(validated_event_type, event, state)
            except _GatewayEventPersistedFailure as exc:
                _write_state_atomic(state_path, state)
                return _failure(
                    exc.failure_class,
                    exc.reason,
                    event_type=validated_event_type,
                )
            validate_gateway_action(action)
            _write_state_atomic(state_path, state)
        return GatewayEventResult(ok=True, event_type=validated_event_type, action=action)
    except GatewayEventContractError as exc:
        return _failure(exc.failure_class, exc.reason, event_type=event_type)
    except OSError:
        return _failure("gateway_event_state_io_failed", "state ledger IO failed", event_type=event_type)
    except (TypeError, ValueError):
        return _failure("gateway_event_apply_failed", "gateway event apply failed", event_type=event_type)


async def apply_gateway_event_async(
    event: Mapping[str, Any],
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
    binary: str | None = None,
) -> GatewayEventResult:
    return await asyncio.to_thread(
        apply_gateway_event,
        event,
        state_dir,
        timeout_seconds=timeout_seconds,
        binary=binary,
    )


def preflight_gateway_event(
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
    binary: str | None = None,
) -> GatewayEventResult:
    del binary
    lock_timeout = _lock_timeout_seconds(timeout_seconds)
    checks: list[dict[str, Any]] = []
    try:
        _check_state_dir_writable(state_dir)
        checks.append(_check("state_dir_writable", True))
        with _state_lock(state_dir, lock_timeout):
            _read_state(_state_path(state_dir))
        with tempfile.TemporaryDirectory(dir=Path(state_dir)) as probe_dir:
            checks.extend(_run_preflight_event_checks(Path(probe_dir), lock_timeout))
    except GatewayEventContractError as exc:
        return _failure(exc.failure_class, exc.reason, event_type="preflight")
    except OSError:
        checks.append(_check("state_dir_writable", False))
    except Exception:
        return _failure("gateway_event_preflight_failed", "gateway event preflight failed", event_type="preflight")

    seen = {check["name"] for check in checks}
    for name in PREFLIGHT_CHECK_NAMES:
        if name not in seen:
            checks.append(_check(name, False))
    checks.sort(key=lambda check: PREFLIGHT_CHECK_NAMES.index(check["name"]))
    action = {"type": "preflight", "checks": checks}
    try:
        validate_gateway_action(action)
    except ValueError:
        return _failure("gateway_event_preflight_failed", "gateway event preflight failed", event_type="preflight")
    return GatewayEventResult(
        ok=all(check["ok"] for check in checks),
        event_type="preflight",
        action=action,
        failure_class=None if all(check["ok"] for check in checks) else "gateway_event_preflight_failed",
        reason=None if all(check["ok"] for check in checks) else "gateway event preflight failed",
    )


def feishu_audit_events_for_readiness(
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
) -> tuple[dict[str, Any], ...]:
    """Return sanitized Feishu audit records already persisted in the ledger."""

    lock_timeout = _lock_timeout_seconds(timeout_seconds)
    with _state_lock(state_dir, lock_timeout):
        state = _read_state(_state_path(state_dir))
    return tuple(dict(event) for event in state["feishu_audit_events"])


def feishu_delivery_lifecycle_events_for_readiness(
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
) -> tuple[dict[str, Any], ...]:
    """Return sanitized Feishu delivery lifecycle records already persisted."""

    lock_timeout = _lock_timeout_seconds(timeout_seconds)
    with _state_lock(state_dir, lock_timeout):
        state = _read_state(_state_path(state_dir))
    return tuple(dict(event) for event in state["feishu_delivery_lifecycle"])


def feishu_broker_action_record(
    state_dir: str | Path,
    action_id: str,
    *,
    timeout_seconds: int | float = 10,
) -> dict[str, Any] | None:
    """Return one sanitized broker-action projection by opaque action ID."""

    lock_timeout = _lock_timeout_seconds(timeout_seconds)
    with _state_lock(state_dir, lock_timeout):
        state = _read_state(_state_path(state_dir))
    record = state["feishu_broker_actions"].get(action_id)
    return dict(record) if isinstance(record, Mapping) else None


def _apply_validated_event(
    event_type: str, event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    if event_type == "feishu_inbound":
        return _apply_feishu_inbound(event, state)
    if event_type == "delivery_pending":
        return _apply_delivery_pending(event, state)
    if event_type == "delivery_sent":
        return _apply_delivery_sent(event, state)
    if event_type == "delivery_failed":
        return _apply_delivery_failed(event, state)
    if event_type == "unknown_delivery_state":
        return _apply_unknown_delivery_state(event, state)
    if event_type == "feishu_ack":
        return _apply_feishu_ack(event, state)
    if event_type == "stale_pending_scan":
        return _apply_stale_pending_scan(event, state)
    if event_type == "session_locked":
        return _apply_session_locked(event, state)
    if event_type == "compression_result":
        return _apply_compression_result(event, state)
    if event_type in FEISHU_AUDIT_EVENT_TYPES:
        return _apply_feishu_audit_event(event, state)
    if event_type in FEISHU_DELIVERY_LIFECYCLE_EVENT_TYPES:
        return _apply_feishu_delivery_lifecycle_event(event, state)
    if event_type in FEISHU_BROKER_ACTION_LIFECYCLE_EVENT_TYPES:
        return _apply_feishu_broker_action_lifecycle_event(event, state)
    raise GatewayEventContractError(
        "unsupported_gateway_event_type", "unsupported gateway event type"
    )


def _apply_feishu_inbound(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    record = {
        "inbound_id_hash": _fnv1a64(str(event["inbound_id"])),
        "message_id_hash": _fnv1a64(str(event["message_id"])),
        "message_type": str(event["message_type"]),
        "first_seen_at": event["timestamp"],
    }
    current_key = _feishu_current_inbound_key(event)
    if current_key is None and _feishu_inbound_requires_current_evidence(event):
        raise GatewayEventContractError(
            "feishu_inbound_idempotency_unknown",
            "feishu inbound idempotency evidence missing",
        )
    key = current_key or f'{record["inbound_id_hash"]}:{record["message_id_hash"]}'
    if current_key is not None:
        record.update(
            {
                "canonical_event_ref": str(event["canonical_event_ref"]),
                "route_partition_hash": _sha256_ref(str(event["route_partition_key"])),
                "contract_hash": str(event["contract_hash"]),
                "transport_kind": str(event["transport_kind"]),
            }
        )
    inbounds = state["inbounds"]
    if current_key is not None:
        _prove_persisted_current_inbound_keys(inbounds)
    duplicate = key in inbounds
    if duplicate:
        record = dict(inbounds[key])
    else:
        inbounds[key] = record
    decision = "feishu_inbound_duplicate" if duplicate else "continue"
    return {
        "type": "inbound_admission",
        "decision": decision,
        "duplicate": duplicate,
        "record": record,
    }


def _feishu_current_inbound_key(event: Mapping[str, Any]) -> str | None:
    present = {
        field
        for field in (
            "canonical_event_ref",
            "route_partition_key",
            "contract_hash",
            "transport_kind",
        )
        if field in event
    }
    if not present:
        return None
    required = {
        "canonical_event_ref",
        "route_partition_key",
        "contract_hash",
        "transport_kind",
    }
    if present != required:
        raise GatewayEventContractError(
            "feishu_inbound_idempotency_unknown",
            "feishu inbound idempotency evidence incomplete",
        )
    canonical_event_ref = str(event.get("canonical_event_ref") or "")
    route_partition_key = str(event.get("route_partition_key") or "")
    contract_hash = str(event.get("contract_hash") or "")
    transport_kind = str(event.get("transport_kind") or "")
    if (
        not _is_sha256_ref(canonical_event_ref)
        or not route_partition_key
        or not _is_sha256_ref(contract_hash)
        or transport_kind not in {"webhook", "websocket", "dm", "group", "thread"}
    ):
        raise GatewayEventContractError(
            "feishu_inbound_idempotency_unknown",
            "feishu inbound idempotency evidence invalid",
        )
    key_material = "\x1f".join(
        (
            canonical_event_ref,
            _sha256_ref(route_partition_key),
            contract_hash,
        )
    )
    return _sha256_ref(key_material)


def _feishu_inbound_requires_current_evidence(event: Mapping[str, Any]) -> bool:
    return event.get("idempotency_evidence_state") in {
        "current_admitted",
        "current_required",
    }


def _prove_persisted_current_inbound_keys(inbounds: Mapping[str, Any]) -> None:
    for persisted_key, record in inbounds.items():
        if not isinstance(record, Mapping):
            raise _GatewayEventPersistedFailure(
                "feishu_inbound_idempotency_unknown",
                "feishu inbound persisted evidence invalid",
            )
        expected_key = _feishu_current_inbound_key_from_record(record, persisted_key)
        if expected_key is None:
            continue
        if not isinstance(persisted_key, str) or persisted_key != expected_key:
            raise _GatewayEventPersistedFailure(
                "feishu_inbound_idempotency_unknown",
                "feishu inbound persisted key mismatch",
            )


def _feishu_current_inbound_key_from_record(
    record: Mapping[str, Any], persisted_key: Any = None
) -> str | None:
    current_fields = {
        "canonical_event_ref",
        "route_partition_hash",
        "contract_hash",
        "transport_kind",
    }
    present = {field for field in current_fields if field in record}
    if not present:
        if isinstance(persisted_key, str) and _is_sha256_ref(persisted_key):
            raise _GatewayEventPersistedFailure(
                "feishu_inbound_idempotency_unknown",
                "feishu inbound persisted evidence missing",
            )
        return None
    if present != current_fields:
        raise _GatewayEventPersistedFailure(
            "feishu_inbound_idempotency_unknown",
            "feishu inbound persisted evidence incomplete",
        )
    canonical_event_ref = record.get("canonical_event_ref")
    route_partition_hash = record.get("route_partition_hash")
    contract_hash = record.get("contract_hash")
    transport_kind = record.get("transport_kind")
    if (
        not isinstance(canonical_event_ref, str)
        or not _is_sha256_ref(canonical_event_ref)
        or not isinstance(route_partition_hash, str)
        or not _is_sha256_ref(route_partition_hash)
        or not isinstance(contract_hash, str)
        or not _is_sha256_ref(contract_hash)
        or transport_kind not in {"webhook", "websocket", "dm", "group", "thread"}
    ):
        raise _GatewayEventPersistedFailure(
            "feishu_inbound_idempotency_unknown",
            "feishu inbound persisted evidence invalid",
        )
    key_material = "\x1f".join(
        (
            canonical_event_ref,
            route_partition_hash,
            contract_hash,
        )
    )
    return _sha256_ref(key_material)


def _apply_delivery_pending(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    delivery_id = str(event["delivery_id"])
    identity = _delivery_identity_from_event(event)
    identity_key = _identity_key(identity)
    records = state["deliveries"]
    identity_index = state["delivery_identity_index"]
    existing_by_identity = identity_index.get(identity_key)
    if existing_by_identity is not None and existing_by_identity != delivery_id:
        raise GatewayEventContractError(
            "delivery_identity_conflict", "delivery identity conflict"
        )
    existing = records.get(delivery_id)
    if existing is not None:
        if _delivery_identity_from_record(existing) != identity:
            raise GatewayEventContractError(
                "delivery_identity_conflict", "delivery identity conflict"
            )
        return {"type": "delivery_record", "record": dict(existing)}
    record = {
        "delivery_id": delivery_id,
        "inbound_id": identity["inbound_id"],
        "target": identity["target"],
        "session_id": identity["session_id"],
        "correlation_id": identity["correlation_id"],
        "status": "pending",
        "created_at": event["timestamp"],
        "updated_at": event["timestamp"],
        "feishu_message_id": None,
        "failure_class": None,
        "ack_event_id": None,
    }
    records[delivery_id] = record
    identity_index[identity_key] = delivery_id
    return {"type": "delivery_record", "record": dict(record)}


def _apply_delivery_sent(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    delivery_id = str(event["delivery_id"])
    record = state["deliveries"].get(delivery_id)
    if record is None:
        raise GatewayEventContractError("unknown_delivery_id", "unknown delivery id")
    _reject_timestamp_regression(event, record)
    message_id = str(event["message_id"])
    existing_message_id = record.get("feishu_message_id")
    if existing_message_id is not None and existing_message_id != message_id:
        raise GatewayEventContractError(
            "delivery_message_id_conflict", "delivery message id conflict"
        )
    if record["status"] not in {"pending", "unknown", "sent"}:
        raise GatewayEventContractError(
            "invalid_delivery_state_transition", "invalid delivery state transition"
        )
    edit_existing_message = record.get("target") == f"feishu:message:{message_id}"
    indexed_delivery = state["feishu_message_index"].get(message_id)
    if (
        indexed_delivery is not None
        and indexed_delivery != delivery_id
        and not edit_existing_message
    ):
        raise GatewayEventContractError(
            "delivery_message_id_conflict", "delivery message id conflict"
        )
    record["status"] = "sent"
    record["updated_at"] = event["timestamp"]
    if not edit_existing_message:
        record["feishu_message_id"] = message_id
    record["failure_class"] = None
    if not edit_existing_message:
        state["feishu_message_index"][message_id] = delivery_id
    return {"type": "delivery_record", "record": dict(record)}


def _apply_delivery_failed(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    delivery_id = str(event["delivery_id"])
    record = state["deliveries"].get(delivery_id)
    if record is None:
        record = _minimal_delivery_record(delivery_id, event["timestamp"])
        state["deliveries"][delivery_id] = record
    else:
        _reject_timestamp_regression(event, record)
    status = record.get("status")
    if status not in {"pending", "unknown", "failed"}:
        raise GatewayEventContractError(
            "invalid_delivery_state_transition", "invalid delivery state transition"
        )
    if status == "failed":
        return {"type": "delivery_record", "record": dict(record)}
    record["status"] = "failed"
    record["updated_at"] = event["timestamp"]
    record["failure_class"] = str(event["failure_class"])
    return {"type": "delivery_record", "record": dict(record)}


def _apply_unknown_delivery_state(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    delivery_id = str(event["delivery_id"])
    record = state["deliveries"].get(delivery_id)
    if record is None:
        record = _minimal_delivery_record(delivery_id, event["timestamp"])
        state["deliveries"][delivery_id] = record
    else:
        _reject_timestamp_regression(event, record)
    if record.get("status") not in {"pending", "unknown"}:
        raise GatewayEventContractError(
            "invalid_delivery_state_transition", "invalid delivery state transition"
        )
    message_id = None
    if "message_id" in event:
        raw_message_id = event["message_id"]
        message_id = raw_message_id
        existing_message_id = record.get("feishu_message_id")
        if existing_message_id is not None and existing_message_id != message_id:
            raise GatewayEventContractError(
                "delivery_message_id_conflict", "delivery message id conflict"
            )
        indexed_delivery = state["feishu_message_index"].get(message_id)
        if indexed_delivery is not None and indexed_delivery != delivery_id:
            raise GatewayEventContractError(
                "delivery_message_id_conflict", "delivery message id conflict"
            )
    record["status"] = "unknown"
    record["updated_at"] = event["timestamp"]
    record["failure_class"] = str(event["failure_class"])
    if message_id is not None:
        record["feishu_message_id"] = message_id
        state["feishu_message_index"][message_id] = delivery_id
    return {"type": "delivery_record", "record": dict(record)}


def _apply_feishu_ack(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    message_id = str(event["message_id"])
    delivery_id = state["feishu_message_index"].get(message_id)
    if delivery_id is None:
        raise GatewayEventContractError("unknown_feishu_message_id", "unknown feishu message id")
    record = state["deliveries"].get(delivery_id)
    if record is None or record.get("status") not in {"sent", "acked"}:
        raise GatewayEventContractError(
            "invalid_delivery_state_transition", "invalid delivery state transition"
        )
    _reject_timestamp_regression(event, record)
    ack_event_id = str(event["ack_event_id"])
    indexed_message_id = state["ack_event_index"].get(ack_event_id)
    if indexed_message_id is not None and indexed_message_id != message_id:
        raise GatewayEventContractError("ack_event_id_conflict", "ack event id conflict")
    if record.get("ack_event_id") is not None and record["ack_event_id"] != ack_event_id:
        raise GatewayEventContractError("ack_event_id_conflict", "ack event id conflict")
    record["status"] = "acked"
    record["updated_at"] = event["timestamp"]
    record["ack_event_id"] = ack_event_id
    record["failure_class"] = None
    state["ack_event_index"][ack_event_id] = message_id
    return {"type": "delivery_record", "record": dict(record)}


def _apply_stale_pending_scan(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    now = event["now"]
    max_age = event["max_age_seconds"]
    stale_records = []
    for record in state["deliveries"].values():
        if record.get("status") not in {"pending", "unknown"}:
            continue
        if record.get("feishu_message_id") is not None or record.get("ack_event_id") is not None:
            continue
        if now - record.get("created_at", now) >= max_age:
            stale_records.append(dict(record))
    stale_records.sort(key=lambda record: record["delivery_id"])
    return {
        "type": "stale_pending_alert",
        "alert_required": bool(stale_records),
        "resend_permitted": False,
        "count": len(stale_records),
        "records": stale_records,
    }


def _apply_session_locked(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    session_key = str(event["session_key"])
    session_id = str(event["session_id"])
    correlation_id = str(event["correlation_id"])
    routes = state["session_routes"]
    existing = routes.get(session_key)
    if existing is not None:
        if existing.get("session_id") != session_id:
            raise GatewayEventContractError(
                "session_route_conflict", "session route conflict"
            )
        return {"type": "session_route", "record": dict(existing)}
    record = {
        "session_key": session_key,
        "session_id": session_id,
        "correlation_id": correlation_id,
    }
    routes[session_key] = record
    return {"type": "session_route", "record": dict(record)}


def _apply_compression_result(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    session_key = str(event["session_key"])
    observed_session_id = str(event["observed_session_id"])
    correlation_id = str(event["correlation_id"])
    route = state["session_routes"].get(session_key)
    if route is None:
        raise GatewayEventContractError(
            "session_route_missing", "session route missing"
        )
    locked_session_id = str(route["session_id"])
    record = {
        "session_key": session_key,
        "locked_session_id": locked_session_id,
        "observed_session_id": observed_session_id,
        "correlation_id": correlation_id,
        "status": "accepted",
        "failure_class": None,
    }
    if observed_session_id == locked_session_id:
        return {"type": "compression_record", "record": record}

    rejection = {
        "session_key": session_key,
        "locked_session_id": locked_session_id,
        "observed_session_id": observed_session_id,
        "correlation_id": correlation_id,
        "failure_class": "implicit_session_switch",
    }
    rejections = state["compression_rejections"]
    rejections.append(rejection)
    if len(rejections) > _MAX_COMPRESSION_REJECTIONS:
        del rejections[: len(rejections) - _MAX_COMPRESSION_REJECTIONS]
    raise _GatewayEventPersistedFailure(
        "implicit_session_switch", "implicit session switch"
    )


def _apply_feishu_audit_event(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    record = dict(event)
    audit_events = state["feishu_audit_events"]
    audit_events.append(record)
    return {"type": "feishu_audit_event_record", "record": record}


def _apply_feishu_delivery_lifecycle_event(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    record = dict(event)
    lifecycle = state["feishu_delivery_lifecycle"]
    lifecycle.append(record)
    return {"type": "feishu_audit_event_record", "record": record}


def _apply_feishu_broker_action_lifecycle_event(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    event_type = str(event["type"])
    if event_type == "feishu_broker_action_created":
        return _apply_feishu_broker_action_created(event, state)
    if event_type == "feishu_broker_action_accepted":
        return _apply_feishu_broker_action_accepted(event, state)
    if event_type == "feishu_broker_action_resolved":
        return _apply_feishu_broker_action_resolved(event, state)
    if event_type == "feishu_broker_action_replayed":
        return _apply_feishu_broker_action_replayed(event, state)
    if event_type == "feishu_broker_action_denied":
        record = _minimal_broker_denial_record(event)
        state["feishu_broker_action_lifecycle"].append(dict(event))
        return {
            "type": "feishu_broker_action_record",
            "record": record,
            "outcome": "denied",
        }
    raise GatewayEventContractError(
        "unsupported_gateway_event_type", "unsupported gateway event type"
    )


def _apply_feishu_broker_action_created(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(event["action_id"])
    actions = state["feishu_broker_actions"]
    existing = actions.get(action_id)
    if existing is not None:
        if _broker_record_matches_created_event(existing, event):
            return {
                "type": "feishu_broker_action_record",
                "record": dict(existing),
                "outcome": "created",
            }
        raise GatewayEventContractError(
            "feishu_broker_action_conflict",
            "broker action id already has different binding",
        )
    record = {
        "action_id": action_id,
        "grant_handle": str(event["grant_handle"]),
        "action_kind": str(event["action_kind"]),
        "route_partition_hash": str(event["route_partition_hash"]),
        "route_snapshot_hash": str(event["route_snapshot_hash"]),
        "operator_hash": str(event["operator_hash"]),
        "contract_hash": str(event["contract_hash"]),
        "payload_hash": str(event["payload_hash"]),
        "expires_at": event["expires_at"],
        "idempotency_key_hash": str(event["idempotency_key_hash"]),
        "status": "created",
        "created_at": event["timestamp"],
        "accepted_at": None,
        "resolved_at": None,
        "replayed_at": None,
    }
    actions[action_id] = record
    state["feishu_broker_action_lifecycle"].append(dict(event))
    return {"type": "feishu_broker_action_record", "record": dict(record), "outcome": "created"}


def _apply_feishu_broker_action_accepted(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    record = _broker_action_record_for_event(event, state)
    if record["status"] == "resolved":
        return {
            "type": "feishu_broker_action_record",
            "record": dict(record),
            "outcome": "duplicate",
        }
    record["status"] = "accepted"
    record["accepted_at"] = event["timestamp"]
    state["feishu_broker_action_lifecycle"].append(dict(event))
    return {"type": "feishu_broker_action_record", "record": dict(record), "outcome": "accepted"}


def _apply_feishu_broker_action_resolved(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    record = _broker_action_record_for_event(event, state)
    if record["status"] == "resolved":
        return {
            "type": "feishu_broker_action_record",
            "record": dict(record),
            "outcome": "duplicate",
        }
    if record["status"] != "accepted":
        raise GatewayEventContractError(
            "feishu_broker_action_state_invalid",
            "broker action was not accepted",
        )
    record["status"] = "resolved"
    record["resolved_at"] = event["timestamp"]
    state["feishu_broker_action_lifecycle"].append(dict(event))
    return {"type": "feishu_broker_action_record", "record": dict(record), "outcome": "resolved"}


def _apply_feishu_broker_action_replayed(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    record = _broker_action_record_for_event(event, state)
    if record["replayed_at"] is None:
        record["replayed_at"] = event["timestamp"]
        state["feishu_broker_action_lifecycle"].append(dict(event))
    return {"type": "feishu_broker_action_record", "record": dict(record), "outcome": "replayed"}


def _broker_action_record_for_event(
    event: Mapping[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(event["action_id"])
    record = state["feishu_broker_actions"].get(action_id)
    if not isinstance(record, dict):
        raise GatewayEventContractError(
            "feishu_broker_action_unknown",
            "unknown broker action id",
        )
    for field in (
        "action_kind",
        "route_partition_hash",
        "route_snapshot_hash",
        "operator_hash",
        "contract_hash",
        "payload_hash",
    ):
        if record.get(field) != event.get(field):
            raise GatewayEventContractError(
                "feishu_broker_action_binding_mismatch",
                "broker action binding mismatch",
            )
    return record


def _broker_record_matches_created_event(
    record: Mapping[str, Any], event: Mapping[str, Any]
) -> bool:
    fields = (
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
    return all(record.get(field) == event.get(field) for field in fields)


def _minimal_broker_denial_record(event: Mapping[str, Any]) -> dict[str, Any]:
    action_id = str(event.get("action_id") or _sha256_ref(str(event.get("callback_hash"))))
    if not action_id.startswith("broker_action:"):
        action_id = f"broker_action:{_sha256_ref(action_id)}"
    grant_handle = str(event.get("grant_handle") or f"broker_grant_handle:{_sha256_ref(action_id)}")
    action_kind = str(event.get("action_kind") or "clarification")
    return {
        "action_id": action_id,
        "grant_handle": grant_handle,
        "action_kind": action_kind if action_kind in {"clarification", "confirmation"} else "clarification",
        "route_partition_hash": str(event.get("route_partition_hash") or _sha256_ref("missing")),
        "route_snapshot_hash": str(event.get("route_snapshot_hash") or _sha256_ref("missing")),
        "operator_hash": str(event.get("operator_hash") or _sha256_ref("missing")),
        "contract_hash": str(event.get("contract_hash") or _sha256_ref("missing")),
        "payload_hash": str(event.get("payload_hash") or _sha256_ref("missing")),
        "expires_at": 0,
        "idempotency_key_hash": _sha256_ref(str(event.get("callback_hash"))),
        "status": "created",
        "created_at": event["timestamp"],
        "accepted_at": None,
        "resolved_at": None,
        "replayed_at": None,
    }


def _read_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return _empty_state()
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise _state_schema_error() from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise _state_schema_error()
    state = _empty_state()
    required_keys = {
        "version",
        *_STATE_DICT_SECTIONS,
        *tuple(
            key
            for key in _STATE_LIST_SECTIONS
            if key
            not in {
                "feishu_audit_events",
                "feishu_delivery_lifecycle",
                "feishu_broker_action_lifecycle",
            }
        ),
    }
    required_keys.discard("feishu_broker_actions")
    if not required_keys.issubset(raw):
        raise _state_schema_error()
    for key in _STATE_DICT_SECTIONS:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise _state_schema_error()
        state[key] = value
    compression_rejections = raw["compression_rejections"]
    if not isinstance(compression_rejections, list):
        raise _state_schema_error()
    state["compression_rejections"] = compression_rejections
    # `feishu_audit_events` is the only v1-compatible additive section. Missing
    # legacy ledgers are deterministically backfilled; all other missing
    # sections remain fail-closed through the required_keys check above.
    feishu_audit_events = raw.get("feishu_audit_events", [])
    if not isinstance(feishu_audit_events, list):
        raise _state_schema_error()
    state["feishu_audit_events"] = feishu_audit_events
    feishu_delivery_lifecycle = raw.get("feishu_delivery_lifecycle", [])
    if not isinstance(feishu_delivery_lifecycle, list):
        raise _state_schema_error()
    state["feishu_delivery_lifecycle"] = feishu_delivery_lifecycle
    feishu_broker_action_lifecycle = raw.get("feishu_broker_action_lifecycle", [])
    if not isinstance(feishu_broker_action_lifecycle, list):
        raise _state_schema_error()
    state["feishu_broker_action_lifecycle"] = feishu_broker_action_lifecycle
    _validate_persisted_inbound_records(state["inbounds"])
    _validate_persisted_delivery_records(state["deliveries"])
    _validate_persisted_session_routes(state["session_routes"])
    _validate_persisted_compression_rejections(state["compression_rejections"])
    _validate_persisted_feishu_audit_events(state["feishu_audit_events"])
    _validate_persisted_feishu_delivery_lifecycle(
        state["feishu_delivery_lifecycle"]
    )
    _validate_persisted_feishu_broker_actions(
        state["feishu_broker_actions"],
        state["feishu_broker_action_lifecycle"],
    )
    _reconcile_persisted_indexes(state)
    return state


def _write_state_atomic(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.", suffix=".tmp", dir=str(state_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                state,
                handle,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, state_path)
        _fsync_parent_dir(state_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "inbounds": {},
        "deliveries": {},
        "delivery_identity_index": {},
        "feishu_message_index": {},
        "ack_event_index": {},
        "session_routes": {},
        "compression_rejections": [],
        "feishu_audit_events": [],
        "feishu_delivery_lifecycle": [],
        "feishu_broker_actions": {},
        "feishu_broker_action_lifecycle": [],
    }


def _validate_persisted_delivery_records(deliveries: Mapping[str, Any]) -> None:
    for delivery_key, record in deliveries.items():
        try:
            validate_gateway_action({"type": "delivery_record", "record": record})
        except ValueError as exc:
            raise _state_schema_error() from exc
        if not isinstance(delivery_key, str) or record.get("delivery_id") != delivery_key:
            raise _state_schema_error()


def _validate_persisted_inbound_records(inbounds: Mapping[str, Any]) -> None:
    for persisted_key, record in inbounds.items():
        if isinstance(record, Mapping):
            try:
                _feishu_current_inbound_key_from_record(record, persisted_key)
            except _GatewayEventPersistedFailure as exc:
                raise GatewayEventContractError(exc.failure_class, exc.reason) from exc
        try:
            validate_gateway_action(
                {
                    "type": "inbound_admission",
                    "decision": "continue",
                    "duplicate": False,
                    "record": record,
                }
            )
        except ValueError as exc:
            raise _state_schema_error() from exc


def _validate_persisted_session_routes(routes: Mapping[str, Any]) -> None:
    for route_key, record in routes.items():
        try:
            validated = validate_gateway_action(
                {"type": "session_route", "record": record}
            )
        except ValueError as exc:
            raise _state_schema_error() from exc
        if not isinstance(route_key, str) or validated["record"]["session_key"] != route_key:
            raise _state_schema_error()


def _validate_persisted_compression_rejections(rejections: list[Any]) -> None:
    for rejection in rejections:
        if not isinstance(rejection, Mapping):
            raise _state_schema_error()
        if set(rejection) != {
            "session_key",
            "locked_session_id",
            "observed_session_id",
            "correlation_id",
            "failure_class",
        }:
            raise _state_schema_error()
        try:
            validate_gateway_action(
                {
                    "type": "compression_record",
                    "record": {
                        "session_key": rejection["session_key"],
                        "locked_session_id": rejection["locked_session_id"],
                        "observed_session_id": rejection["observed_session_id"],
                        "correlation_id": rejection["correlation_id"],
                        "status": "rejected",
                        "failure_class": rejection["failure_class"],
                    },
                }
            )
        except (GatewayEventContractError, ValueError) as exc:
            raise _state_schema_error() from exc


def _validate_persisted_feishu_audit_events(events: list[Any]) -> None:
    for event in events:
        try:
            validate_gateway_action(
                {"type": "feishu_audit_event_record", "record": event}
            )
        except ValueError as exc:
            raise _state_schema_error() from exc


def _validate_persisted_feishu_delivery_lifecycle(events: list[Any]) -> None:
    for event in events:
        try:
            validate_gateway_event(event)
            validate_gateway_action(
                {"type": "feishu_audit_event_record", "record": event}
            )
        except (GatewayEventContractError, ValueError) as exc:
            raise _state_schema_error() from exc


def _validate_persisted_feishu_broker_actions(
    actions: Mapping[str, Any], lifecycle: list[Any]
) -> None:
    for action_id, record in actions.items():
        if not isinstance(action_id, str):
            raise _state_schema_error()
        try:
            validated = validate_gateway_action(
                {
                    "type": "feishu_broker_action_record",
                    "record": record,
                    "outcome": "created",
                }
            )
        except (GatewayEventContractError, ValueError) as exc:
            raise _state_schema_error() from exc
        if validated["record"]["action_id"] != action_id:
            raise _state_schema_error()
    for event in lifecycle:
        try:
            validate_gateway_event(event)
        except GatewayEventContractError as exc:
            raise _state_schema_error() from exc


def _reconcile_persisted_indexes(state: dict[str, Any]) -> None:
    expected_identity_index: dict[str, str] = {}
    expected_message_index: dict[str, str] = {}
    expected_ack_index: dict[str, str] = {}
    for record in state["deliveries"].values():
        if not isinstance(record, Mapping):
            raise _state_schema_error()
        delivery_id = record.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise _state_schema_error()

        identity_values = [
            record.get(field)
            for field in ("inbound_id", "target", "session_id", "correlation_id")
        ]
        if all(isinstance(value, str) and value for value in identity_values):
            identity_key = _identity_key(_delivery_identity_from_record(record))
            existing_delivery_id = expected_identity_index.get(identity_key)
            if existing_delivery_id is not None and existing_delivery_id != delivery_id:
                raise _state_schema_error()
            expected_identity_index[identity_key] = delivery_id
        elif any(value is not None for value in identity_values):
            raise _state_schema_error()

        message_id = record.get("feishu_message_id")
        if isinstance(message_id, str):
            existing_delivery_id = expected_message_index.get(message_id)
            if existing_delivery_id is not None and existing_delivery_id != delivery_id:
                raise _state_schema_error()
            expected_message_index[message_id] = delivery_id

        ack_event_id = record.get("ack_event_id")
        if isinstance(ack_event_id, str):
            if not isinstance(message_id, str):
                raise _state_schema_error()
            indexed_message_id = expected_ack_index.get(ack_event_id)
            if indexed_message_id is not None and indexed_message_id != message_id:
                raise _state_schema_error()
            expected_ack_index[ack_event_id] = message_id

    _validate_persisted_index_subset(
        state["delivery_identity_index"], expected_identity_index
    )
    _validate_persisted_index_subset(state["feishu_message_index"], expected_message_index)
    _validate_persisted_index_subset(state["ack_event_index"], expected_ack_index)
    state["delivery_identity_index"] = expected_identity_index
    state["feishu_message_index"] = expected_message_index
    state["ack_event_index"] = expected_ack_index


def _validate_persisted_index_subset(
    persisted: Mapping[str, Any], expected: Mapping[str, str]
) -> None:
    for key, value in persisted.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _state_schema_error()
        if expected.get(key) != value:
            raise _state_schema_error()


def _minimal_delivery_record(delivery_id: str, timestamp: int | float) -> dict[str, Any]:
    return {
        "delivery_id": delivery_id,
        "inbound_id": None,
        "target": None,
        "session_id": None,
        "correlation_id": None,
        "status": "unknown",
        "created_at": timestamp,
        "updated_at": timestamp,
        "feishu_message_id": None,
        "failure_class": None,
        "ack_event_id": None,
    }


def _delivery_identity_from_event(event: Mapping[str, Any]) -> dict[str, str]:
    return {
        "inbound_id": str(event["inbound_id"]),
        "target": str(event["target"]),
        "session_id": str(event["session_id"]),
        "correlation_id": str(event["correlation_id"]),
    }


def _delivery_identity_from_record(record: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "inbound_id": record.get("inbound_id"),
        "target": record.get("target"),
        "session_id": record.get("session_id"),
        "correlation_id": record.get("correlation_id"),
    }


def _identity_key(identity: Mapping[str, Any]) -> str:
    return "\x1f".join(
        str(identity[field])
        for field in ("inbound_id", "target", "session_id", "correlation_id")
    )


def _reject_timestamp_regression(
    event: Mapping[str, Any], record: Mapping[str, Any]
) -> None:
    updated_at = record.get("updated_at")
    event_timestamp = event.get("timestamp")
    if _is_ordered_number(event_timestamp) and _is_ordered_number(updated_at):
        if event_timestamp < updated_at:
            raise GatewayEventContractError(
                "delivery_timestamp_regression",
                "delivery timestamp regression",
            )


def _is_ordered_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _fnv1a64(value: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{digest:016x}"


def _sha256_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _is_sha256_ref(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(char in "0123456789abcdef" for char in value[7:])


def _state_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / LEDGER_FILENAME


def _lock_for(state_dir: str | Path) -> threading.Lock:
    key = str(Path(state_dir).expanduser())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _state_lock(state_dir: str | Path, timeout_seconds: float):
    path = Path(state_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    thread_lock = _lock_for(path)
    if not thread_lock.acquire(timeout=timeout_seconds):
        raise GatewayEventContractError(
            "gateway_event_state_lock_timeout",
            "gateway event state lock timeout",
        )
    try:
        with _state_file_lock(path / LOCK_FILENAME, deadline):
            yield
    finally:
        thread_lock.release()


@contextlib.contextmanager
def _state_file_lock(lock_path: Path, deadline: float):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        _acquire_file_lock(handle, deadline)
        yield
    finally:
        try:
            _release_file_lock(handle)
        finally:
            handle.close()


def _acquire_file_lock(handle: Any, deadline: float) -> None:
    while True:
        try:
            if _IS_WINDOWS:
                import msvcrt  # type: ignore[import-not-found]

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except ImportError as exc:
            raise GatewayEventContractError(
                "gateway_event_state_lock_unavailable",
                "gateway event state lock unavailable",
            ) from exc
        except (BlockingIOError, OSError, PermissionError) as exc:
            now = time.monotonic()
            if now >= deadline:
                raise GatewayEventContractError(
                    "gateway_event_state_lock_timeout",
                    "gateway event state lock timeout",
                ) from exc
            time.sleep(min(0.05, max(0.0, deadline - now)))


def _release_file_lock(handle: Any) -> None:
    try:
        if _IS_WINDOWS:
            import msvcrt  # type: ignore[import-not-found]

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError, IOError):
        pass


def _safe_event_type(event: Any) -> str | None:
    if isinstance(event, Mapping):
        return event_type_from(event)
    return None


def _failure(
    failure_class: str, reason: str, *, event_type: str | None = None
) -> GatewayEventResult:
    return GatewayEventResult(
        ok=False,
        event_type=event_type,
        failure_class=failure_class,
        reason=reason,
        diagnostics="",
    )


def _state_schema_error() -> GatewayEventContractError:
    return GatewayEventContractError(
        "gateway_event_state_schema_invalid",
        "gateway event state schema invalid",
    )


def _lock_timeout_seconds(value: int | float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_LOCK_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return _DEFAULT_LOCK_TIMEOUT_SECONDS
    return timeout


def _fsync_parent_dir(path: Path) -> None:
    if _IS_WINDOWS:
        return
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check(name: str, ok: bool) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": "ok" if ok else "failed"}


def _check_state_dir_writable(state_dir: str | Path) -> None:
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".gateway-event-preflight.", dir=str(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("ok")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _run_preflight_event_checks(
    probe_dir: Path, timeout_seconds: float
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    inbound = {
        "type": "feishu_inbound",
        "inbound_id": "preflight-inbound",
        "message_id": "preflight-message",
        "message_type": "text",
        "timestamp": 1,
    }
    checks.append(
        _check(
            "feishu_inbound",
            apply_gateway_event(
                inbound, probe_dir, timeout_seconds=timeout_seconds
            ).ok,
        )
    )

    pending = {
        "type": "delivery_pending",
        "delivery_id": "delivery-preflight",
        "inbound_id": "preflight-inbound",
        "target": "feishu:preflight",
        "session_id": "session-preflight",
        "correlation_id": "correlation-preflight",
        "timestamp": 2,
    }
    sent = {
        "type": "delivery_sent",
        "delivery_id": "delivery-preflight",
        "message_id": "preflight_feishu_message",
        "timestamp": 3,
    }
    lifecycle_ok = apply_gateway_event(
        pending, probe_dir, timeout_seconds=timeout_seconds
    ).ok and apply_gateway_event(sent, probe_dir, timeout_seconds=timeout_seconds).ok
    checks.append(_check("delivery_lifecycle", lifecycle_ok))

    ack = {
        "type": "feishu_ack",
        "message_id": "preflight_feishu_message",
        "ack_event_id": "preflight-ack",
        "timestamp": 4,
    }
    checks.append(
        _check(
            "feishu_ack",
            apply_gateway_event(ack, probe_dir, timeout_seconds=timeout_seconds).ok,
        )
    )

    stale_pending = {
        "type": "delivery_pending",
        "delivery_id": "delivery-preflight-stale",
        "inbound_id": "preflight-inbound-stale",
        "target": "feishu:preflight",
        "session_id": "session-preflight",
        "correlation_id": "correlation-preflight-stale",
        "timestamp": 1,
    }
    scan = {"type": "stale_pending_scan", "now": 10, "max_age_seconds": 1}
    stale_result = apply_gateway_event(
        stale_pending, probe_dir, timeout_seconds=timeout_seconds
    )
    if stale_result.ok:
        stale_result = apply_gateway_event(
            scan, probe_dir, timeout_seconds=timeout_seconds
        )
    stale_ok = (
        stale_result.ok
        and stale_result.action is not None
        and stale_result.action.get("resend_permitted") is False
    )
    checks.append(_check("stale_pending_scan", bool(stale_ok)))

    session_lock = apply_gateway_event(
        {
            "type": "session_locked",
            "session_key": "preflight-session-key",
            "session_id": "preflight-session-a",
            "correlation_id": "preflight-correlation",
        },
        probe_dir,
        timeout_seconds=timeout_seconds,
    )
    session_mismatch = apply_gateway_event(
        {
            "type": "compression_result",
            "session_key": "preflight-session-key",
            "observed_session_id": "preflight-session-b",
            "correlation_id": "preflight-correlation",
        },
        probe_dir,
        timeout_seconds=timeout_seconds,
    )
    checks.append(
        _check(
            "session_guard",
            session_lock.ok
            and session_mismatch.ok is False
            and session_mismatch.failure_class == "implicit_session_switch",
        )
    )
    return checks

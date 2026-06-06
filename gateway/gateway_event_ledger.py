"""Internal JSON ledger implementation for Hermes gateway events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from gateway.gateway_event_contract import (
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
_IS_WINDOWS = os.name == "nt"
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


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
            action = _apply_validated_event(validated_event_type, event, state)
            validate_gateway_action(action)
            _write_state_atomic(state_path, state)
        return GatewayEventResult(ok=True, event_type=validated_event_type, action=action)
    except GatewayEventContractError as exc:
        return _failure(exc.failure_class, exc.reason, event_type=event_type)
    except OSError:
        return _failure("gateway_event_state_io_failed", "state ledger IO failed", event_type=event_type)
    except (TypeError, ValueError, json.JSONDecodeError):
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
        with tempfile.TemporaryDirectory(dir=Path(state_dir)) as probe_dir:
            checks.extend(_run_preflight_event_checks(Path(probe_dir), lock_timeout))
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
    key = f'{record["inbound_id_hash"]}:{record["message_id_hash"]}'
    inbounds = state["inbounds"]
    duplicate = key in inbounds
    if duplicate:
        record = dict(inbounds[key])
    else:
        inbounds[key] = record
    return {
        "type": "inbound_admission",
        "decision": "continue",
        "duplicate": duplicate,
        "record": record,
    }


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
    indexed_delivery = state["feishu_message_index"].get(message_id)
    if indexed_delivery is not None and indexed_delivery != delivery_id:
        raise GatewayEventContractError(
            "delivery_message_id_conflict", "delivery message id conflict"
        )
    record["status"] = "sent"
    record["updated_at"] = event["timestamp"]
    record["feishu_message_id"] = message_id
    state["feishu_message_index"][message_id] = delivery_id
    return {"type": "delivery_record", "record": dict(record)}


def _apply_delivery_failed(event: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    delivery_id = str(event["delivery_id"])
    record = state["deliveries"].get(delivery_id)
    if record is None:
        record = _minimal_delivery_record(delivery_id, event["timestamp"])
        state["deliveries"][delivery_id] = record
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
    if record.get("status") not in {"pending", "unknown"}:
        raise GatewayEventContractError(
            "invalid_delivery_state_transition", "invalid delivery state transition"
        )
    message_id = None
    if "message_id" in event:
        raw_message_id = event["message_id"]
        if not isinstance(raw_message_id, str) or not raw_message_id:
            raise GatewayEventContractError(
                "invalid_gateway_event_contract",
                "message_id is missing or invalid",
            )
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
    ack_event_id = str(event["ack_event_id"])
    indexed_message_id = state["ack_event_index"].get(ack_event_id)
    if indexed_message_id is not None and indexed_message_id != message_id:
        raise GatewayEventContractError("ack_event_id_conflict", "ack event id conflict")
    if record.get("ack_event_id") is not None and record["ack_event_id"] != ack_event_id:
        raise GatewayEventContractError("ack_event_id_conflict", "ack event id conflict")
    record["status"] = "acked"
    record["updated_at"] = event["timestamp"]
    record["ack_event_id"] = ack_event_id
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


def _read_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return _empty_state()
    with state_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise _state_schema_error()
    state = _empty_state()
    for key in (
        "inbounds",
        "deliveries",
        "delivery_identity_index",
        "feishu_message_index",
        "ack_event_index",
    ):
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise _state_schema_error()
        state[key] = value
    _validate_persisted_inbound_records(state["inbounds"])
    _validate_persisted_delivery_records(state["deliveries"])
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
    for record in inbounds.values():
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


def _fnv1a64(value: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{digest:016x}"


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
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
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
        "message_id": "preflight-feishu-message",
        "timestamp": 3,
    }
    lifecycle_ok = apply_gateway_event(
        pending, probe_dir, timeout_seconds=timeout_seconds
    ).ok and apply_gateway_event(sent, probe_dir, timeout_seconds=timeout_seconds).ok
    checks.append(_check("delivery_lifecycle", lifecycle_ok))

    ack = {
        "type": "feishu_ack",
        "message_id": "preflight-feishu-message",
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

    unsupported = apply_gateway_event(
        {"type": "task_status"}, probe_dir, timeout_seconds=timeout_seconds
    )
    checks.append(_check("session_guard", unsupported.ok is False))
    return checks

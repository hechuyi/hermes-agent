"""Subprocess adapter for the hermes-tools gateway-event contract."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.redact import redact_sensitive_text


DEFAULT_HERMES_TOOLS_BINARY = "hermes-tools"
DEFAULT_TIMEOUT_SECONDS = 10
_ASYNC_APPLY_MAX_CONCURRENCY = 4
_ASYNC_APPLY_SEMAPHORE = threading.BoundedSemaphore(_ASYNC_APPLY_MAX_CONCURRENCY)

_SUPPORTED_ACTION_TYPES = frozenset(
    {
        "delivery_record",
        "stale_pending_alert",
        "session_state",
        "status_card",
        "inbound_admission",
    }
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])/(?:Users|home|var|tmp|private|Volumes|srv|etc)/[^\s\"'`,;)]*"
)
_URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>]+")
_MAX_DIAGNOSTIC_CHARS = 1200
_SAFE_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SAFE_EVENT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_STABLE_SANITIZED_HASH_RE = re.compile(r"^fnv1a64:[a-f0-9]{16}$")
_MAX_PREFLIGHT_FEISHU_CONTENT_CHARS = 32768
_APPLY_SUCCESS_KEYS = frozenset({"ok", "event_type", "action"})
_APPLY_ERROR_KEYS = frozenset({"ok", "event_type", "error"})
_APPLY_ERROR_OBJECT_KEYS = frozenset({"reason", "message"})
_PREFLIGHT_SUCCESS_KEYS = frozenset(
    {"ok", "state_dir_writable", "checks", "feishu_request"}
)
_PREFLIGHT_FAILURE_KEYS = frozenset(
    {
        "ok",
        "state_dir_writable",
        "checks",
        "failure_class",
        "failure_detail",
        "feishu_request",
    }
)
_INBOUND_ADMISSION_ACTION_KEYS = frozenset(
    {"type", "decision", "duplicate", "record"}
)
_INBOUND_ADMISSION_RECORD_KEYS = frozenset(
    {"inbound_id_hash", "message_id_hash", "message_type", "first_seen_at"}
)
_DELIVERY_RECORD_ACTION_KEYS = frozenset({"type", "record"})
_DELIVERY_RECORD_KEYS = frozenset(
    {
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
)
_DELIVERY_STATUSES = frozenset({"pending", "sent", "failed", "acked"})
_PREFLIGHT_CHECK_KEYS = frozenset({"name", "ok", "detail"})
_PREFLIGHT_FEISHU_REQUEST_KEYS = frozenset(
    {"operation", "method", "path", "params", "body"}
)
_PREFLIGHT_FEISHU_PARAMS_KEYS = frozenset({"receive_id_type"})
_PREFLIGHT_FEISHU_BODY_REQUIRED_KEYS = frozenset(
    {"receive_id", "msg_type", "content"}
)
_PREFLIGHT_FEISHU_BODY_OPTIONAL_KEYS = frozenset({"uuid"})
_PREFLIGHT_FEISHU_RECEIVE_ID_TYPES = frozenset(
    {"open_id", "union_id", "user_id", "email", "chat_id"}
)


@dataclass(frozen=True)
class HermesToolsGatewayEventResult:
    ok: bool
    event_type: str | None = None
    action: dict[str, Any] | None = None
    failure_class: str | None = None
    reason: str | None = None
    diagnostics: str = ""


def apply_gateway_event(
    event: Mapping[str, Any],
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = DEFAULT_TIMEOUT_SECONDS,
    binary: str = DEFAULT_HERMES_TOOLS_BINARY,
) -> HermesToolsGatewayEventResult:
    """Apply one gateway event through hermes-tools.

    The event payload is only sent to hermes-tools on stdin. It is not copied
    into diagnostics, because event payloads may contain user text or platform
    identifiers that are not safe to surface.
    """
    state_dir_path = Path(state_dir)
    try:
        event_input = json.dumps(event, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return _failure(
            "gateway_event_input_json_error",
            "event payload is not JSON serializable",
            state_dir=state_dir_path,
        )

    result = _run_hermes_tools(
        [
            binary,
            "gateway-event",
            "apply",
            "--state-dir",
            str(state_dir_path),
        ],
        state_dir_path,
        timeout_seconds=timeout_seconds,
        input_text=event_input,
    )
    if isinstance(result, HermesToolsGatewayEventResult):
        return result

    completed = result
    envelope_text = completed.stdout if completed.returncode == 0 else completed.stderr
    envelope = _load_envelope(envelope_text)
    if envelope is None and completed.returncode != 0:
        envelope = _load_envelope(completed.stdout)

    if envelope is None:
        if completed.returncode != 0:
            return _failure(
                "hermes_tools_nonzero_exit",
                "hermes-tools exited nonzero",
                diagnostics=_completed_diagnostics(completed),
                state_dir=state_dir_path,
            )
        return _failure(
            "hermes_tools_invalid_json",
            "invalid hermes-tools JSON envelope",
            diagnostics=_completed_diagnostics(completed),
            state_dir=state_dir_path,
        )

    if envelope.get("ok") is False:
        return _failure_from_apply_error_envelope(envelope, completed, state_dir_path)

    if completed.returncode != 0:
        return _failure(
            "hermes_tools_nonzero_exit",
            "hermes-tools exited nonzero",
            diagnostics=_completed_diagnostics(completed),
            state_dir=state_dir_path,
        )

    return _success_from_apply_envelope(envelope, state_dir_path)


async def apply_gateway_event_async(
    event: Mapping[str, Any],
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = DEFAULT_TIMEOUT_SECONDS,
    binary: str = DEFAULT_HERMES_TOOLS_BINARY,
) -> HermesToolsGatewayEventResult:
    """Apply one gateway event without blocking the current event loop."""
    state_dir_path = Path(state_dir)
    if not _ASYNC_APPLY_SEMAPHORE.acquire(blocking=False):
        event_type = _string_or_none(event.get("type"))
        if event_type is not None and not _is_safe_event_type(event_type):
            event_type = None
        return _failure(
            "hermes_tools_async_saturated",
            "async apply worker capacity exhausted",
            event_type=event_type,
            diagnostics=f"max_concurrency={_ASYNC_APPLY_MAX_CONCURRENCY}",
            state_dir=state_dir_path,
        )
    worker = asyncio.create_task(
        asyncio.to_thread(
            apply_gateway_event,
            event,
            state_dir_path,
            timeout_seconds=timeout_seconds,
            binary=binary,
        )
    )
    try:
        return await asyncio.shield(worker)
    finally:
        if worker.done():
            _ASYNC_APPLY_SEMAPHORE.release()
        else:
            worker.add_done_callback(lambda _worker: _ASYNC_APPLY_SEMAPHORE.release())


def preflight_gateway_event(
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = DEFAULT_TIMEOUT_SECONDS,
    binary: str = DEFAULT_HERMES_TOOLS_BINARY,
) -> HermesToolsGatewayEventResult:
    """Run hermes-tools gateway-event preflight for a durable state directory."""
    state_dir_path = Path(state_dir)
    result = _run_hermes_tools(
        [
            binary,
            "gateway-event",
            "preflight",
            "--state-dir",
            str(state_dir_path),
        ],
        state_dir_path,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(result, HermesToolsGatewayEventResult):
        return result

    completed = result
    envelope = _load_envelope(completed.stdout)
    if envelope is None:
        if completed.returncode != 0:
            return _failure(
                "hermes_tools_nonzero_exit",
                "hermes-tools exited nonzero",
                diagnostics=_completed_diagnostics(completed),
                state_dir=state_dir_path,
            )
        return _failure(
            "hermes_tools_invalid_json",
            "invalid hermes-tools JSON envelope",
            diagnostics=_completed_diagnostics(completed),
            state_dir=state_dir_path,
        )

    if envelope.get("ok") is False:
        if not _has_only_keys(envelope, _PREFLIGHT_FAILURE_KEYS):
            return _invalid_envelope(
                "preflight failure envelope has unsupported fields",
                state_dir_path,
            )
        if _validated_preflight_checks(envelope.get("checks")) is None:
            return _invalid_envelope(
                "preflight envelope has malformed checks",
                state_dir_path,
            )
        if _validated_feishu_request(envelope.get("feishu_request")) is _INVALID:
            return _invalid_envelope(
                "preflight envelope has malformed feishu_request",
                state_dir_path,
            )
        failure_class = _validated_failure_class(
            envelope.get("failure_class"),
            fallback="hermes_tools_preflight_failed",
        )
        return _failure(
            failure_class,
            failure_class,
            event_type="preflight",
            diagnostics=f"preflight_failure failure_class={failure_class}",
            state_dir=state_dir_path,
        )

    if completed.returncode != 0:
        return _failure(
            "hermes_tools_nonzero_exit",
            "hermes-tools exited nonzero",
            diagnostics=_completed_diagnostics(completed),
            state_dir=state_dir_path,
        )

    if envelope.get("ok") is not True:
        return _invalid_envelope("preflight envelope missing ok=true", state_dir_path)

    if not _has_only_keys(envelope, _PREFLIGHT_SUCCESS_KEYS):
        return _invalid_envelope(
            "preflight envelope has unsupported fields",
            state_dir_path,
        )

    checks = _validated_preflight_checks(envelope.get("checks"))
    feishu_request = _validated_feishu_request(envelope.get("feishu_request"))
    if (
        not isinstance(envelope.get("state_dir_writable"), bool)
        or checks is None
        or feishu_request is _INVALID
    ):
        return _invalid_envelope(
            "preflight envelope missing expected report fields",
            state_dir_path,
        )

    return HermesToolsGatewayEventResult(
        ok=True,
        event_type="preflight",
        action={
            "type": "preflight_report",
            "state_dir_writable": envelope.get("state_dir_writable"),
            "checks": checks,
            "feishu_request": feishu_request,
        },
    )


def _run_hermes_tools(
    cmd: list[str],
    state_dir: Path,
    *,
    timeout_seconds: int | float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str] | HermesToolsGatewayEventResult:
    if timeout_seconds <= 0:
        return _failure(
            "hermes_tools_invalid_timeout",
            "timeout must be positive",
            state_dir=state_dir,
        )

    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout_seconds,
        "check": False,
    }
    if input_text is not None:
        kwargs["input"] = input_text

    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return _failure(
            "hermes_tools_missing",
            "hermes-tools executable not found",
            diagnostics="binary=hermes-tools not found",
            state_dir=state_dir,
        )
    except subprocess.TimeoutExpired:
        return _failure(
            "hermes_tools_timeout",
            "subprocess timeout",
            diagnostics=f"timeout_seconds={timeout_seconds}",
            state_dir=state_dir,
        )
    except OSError as exc:
        return _failure(
            "hermes_tools_subprocess_error",
            "hermes-tools subprocess error",
            diagnostics=exc.__class__.__name__,
            state_dir=state_dir,
        )


def _success_from_apply_envelope(
    envelope: dict[str, Any],
    state_dir: Path,
) -> HermesToolsGatewayEventResult:
    if envelope.get("ok") is not True:
        return _invalid_envelope("apply envelope missing ok=true", state_dir)

    if not _has_only_keys(envelope, _APPLY_SUCCESS_KEYS):
        return _invalid_envelope("apply envelope has unsupported fields", state_dir)

    event_type = _string_or_none(envelope.get("event_type"))
    action = envelope.get("action")
    if event_type is None or not _is_safe_event_type(event_type) or not isinstance(
        action, dict
    ):
        return _invalid_envelope("apply envelope missing event_type/action", state_dir)

    action_type = _string_or_none(action.get("type"))
    if action_type not in _SUPPORTED_ACTION_TYPES:
        return _failure(
            "unsupported_action",
            "unsupported action",
            event_type=event_type,
            diagnostics=(
                "unsupported action "
                f"type_present={str(action_type is not None).lower()}"
            ),
            state_dir=state_dir,
        )

    validated_action = _validated_action(action)
    if validated_action is None:
        return _invalid_envelope("apply envelope has malformed action", state_dir)

    return HermesToolsGatewayEventResult(
        ok=True,
        event_type=event_type,
        action=validated_action,
    )


def _failure_from_apply_error_envelope(
    envelope: dict[str, Any],
    completed: subprocess.CompletedProcess[str],
    state_dir: Path,
) -> HermesToolsGatewayEventResult:
    error = envelope.get("error")
    if (
        not _has_only_keys(envelope, _APPLY_ERROR_KEYS)
        or not isinstance(error, dict)
        or not _has_only_keys(error, _APPLY_ERROR_OBJECT_KEYS)
    ):
        return _invalid_envelope(
            "apply error envelope missing error object",
            state_dir,
            diagnostics=_completed_diagnostics(completed),
        )

    event_type = _string_or_none(envelope.get("event_type"))
    if event_type is not None and not _is_safe_event_type(event_type):
        return _invalid_envelope(
            "apply error envelope has malformed event_type",
            state_dir,
            diagnostics=_completed_diagnostics(completed),
        )

    reason = _validated_failure_class(
        error.get("reason"),
        fallback="hermes_tools_apply_failed",
    )
    return _failure(
        reason,
        reason,
        event_type=event_type,
        diagnostics=f"apply_error failure_class={reason} {_completed_diagnostics(completed)}",
        state_dir=state_dir,
    )


def _load_envelope(text: str | None) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _invalid_envelope(
    reason: str,
    state_dir: Path,
    *,
    diagnostics: str = "",
) -> HermesToolsGatewayEventResult:
    return _failure(
        "hermes_tools_invalid_envelope",
        reason,
        diagnostics=diagnostics,
        state_dir=state_dir,
    )


def _failure(
    failure_class: str,
    reason: str,
    *,
    event_type: str | None = None,
    diagnostics: str = "",
    state_dir: Path,
) -> HermesToolsGatewayEventResult:
    return HermesToolsGatewayEventResult(
        ok=False,
        event_type=event_type,
        failure_class=_sanitize_text(failure_class, state_dir),
        reason=_sanitize_text(reason, state_dir),
        diagnostics=_sanitize_text(diagnostics, state_dir),
    )


def _completed_diagnostics(completed: subprocess.CompletedProcess[str]) -> str:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    parts = [
        f"returncode={completed.returncode}",
        f"stdout_bytes={_byte_count(stdout)}",
        f"stdout_lines={_line_count(stdout)}",
        f"stdout_json={_json_status(stdout)}",
        f"stderr_bytes={_byte_count(stderr)}",
        f"stderr_lines={_line_count(stderr)}",
        f"stderr_json={_json_status(stderr)}",
    ]
    return " ".join(parts)


class _InvalidSentinel:
    pass


_INVALID = _InvalidSentinel()


def _validated_action(action: dict[str, Any]) -> dict[str, Any] | None:
    action_type = _string_or_none(action.get("type"))
    if action_type == "delivery_record":
        if not _has_exact_keys(action, _DELIVERY_RECORD_ACTION_KEYS):
            return None
        record = _validated_delivery_record(action.get("record"))
        if record is None:
            return None
        return {"type": action_type, "record": record}

    if action_type in {"stale_pending_alert", "session_state", "status_card"}:
        if not _has_only_keys(action, {"type"}):
            return None
        return {"type": action_type}

    if action_type == "inbound_admission":
        if not _has_exact_keys(action, _INBOUND_ADMISSION_ACTION_KEYS):
            return None
        if action.get("decision") != "continue":
            return None
        duplicate = action.get("duplicate")
        record = _validated_inbound_admission_record(action.get("record"))
        if not isinstance(duplicate, bool) or record is None:
            return None
        return {
            "type": action_type,
            "decision": "continue",
            "duplicate": duplicate,
            "record": record,
        }

    return None


def _validated_delivery_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _has_exact_keys(value, _DELIVERY_RECORD_KEYS):
        return None

    delivery_id = _validated_identifier_field(value.get("delivery_id"))
    inbound_id = _validated_identifier_field(value.get("inbound_id"))
    target = _validated_identifier_field(value.get("target"))
    session_id = _validated_identifier_field(value.get("session_id"))
    correlation_id = _validated_identifier_field(value.get("correlation_id"))
    status = _string_or_none(value.get("status"))
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    feishu_message_id = _validated_optional_identifier_field(
        value.get("feishu_message_id")
    )
    failure_class = _validated_optional_failure_class_field(value.get("failure_class"))
    ack_event_id = _validated_optional_identifier_field(value.get("ack_event_id"))

    if (
        delivery_id is None
        or inbound_id is None
        or target is None
        or session_id is None
        or correlation_id is None
        or status not in _DELIVERY_STATUSES
        or not _is_json_int(created_at)
        or not _is_json_int(updated_at)
        or feishu_message_id is _INVALID
        or failure_class is _INVALID
        or ack_event_id is _INVALID
    ):
        return None

    return {
        "delivery_id": delivery_id,
        "inbound_id": inbound_id,
        "target": target,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "feishu_message_id": feishu_message_id,
        "failure_class": failure_class,
        "ack_event_id": ack_event_id,
    }


def _validated_inbound_admission_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _has_exact_keys(
        value, _INBOUND_ADMISSION_RECORD_KEYS
    ):
        return None

    inbound_id_hash = _string_or_none(value.get("inbound_id_hash"))
    message_id_hash = _string_or_none(value.get("message_id_hash"))
    message_type = _validated_identifier_field(value.get("message_type"))
    first_seen_at = value.get("first_seen_at")
    if (
        inbound_id_hash is None
        or message_id_hash is None
        or not _STABLE_SANITIZED_HASH_RE.fullmatch(inbound_id_hash)
        or not _STABLE_SANITIZED_HASH_RE.fullmatch(message_id_hash)
        or message_type is None
        or not _is_json_int(first_seen_at)
    ):
        return None

    return {
        "inbound_id_hash": inbound_id_hash,
        "message_id_hash": message_id_hash,
        "message_type": message_type,
        "first_seen_at": first_seen_at,
    }


def _validated_preflight_checks(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    checks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not _has_only_keys(item, _PREFLIGHT_CHECK_KEYS):
            return None
        name = item.get("name")
        ok = item.get("ok")
        detail = item.get("detail")
        if not isinstance(name, str) or not _is_safe_identifier(name):
            return None
        if not isinstance(ok, bool):
            return None
        if detail is not None and not isinstance(detail, str):
            return None
        checks.append({"name": name, "ok": ok})
    return checks


def _validated_feishu_request(value: Any) -> dict[str, Any] | None | _InvalidSentinel:
    if value is None:
        return None
    if not isinstance(value, dict) or not _has_exact_keys(
        value, _PREFLIGHT_FEISHU_REQUEST_KEYS
    ):
        return _INVALID
    if (
        value.get("operation") != "send_interactive_message"
        or value.get("method") != "POST"
        or value.get("path") != "/open-apis/im/v1/messages"
    ):
        return _INVALID

    params = value.get("params")
    if not isinstance(params, dict) or not _has_exact_keys(
        params, _PREFLIGHT_FEISHU_PARAMS_KEYS
    ):
        return _INVALID
    receive_id_type = params.get("receive_id_type")
    if receive_id_type not in _PREFLIGHT_FEISHU_RECEIVE_ID_TYPES:
        return _INVALID

    body = value.get("body")
    if not isinstance(body, dict):
        return _INVALID
    body_keys = set(body.keys())
    if (
        not _PREFLIGHT_FEISHU_BODY_REQUIRED_KEYS.issubset(body_keys)
        or not body_keys.issubset(
            _PREFLIGHT_FEISHU_BODY_REQUIRED_KEYS
            | _PREFLIGHT_FEISHU_BODY_OPTIONAL_KEYS
        )
    ):
        return _INVALID

    receive_id = _validated_identifier_field(body.get("receive_id"))
    content = body.get("content")
    uuid = _validated_optional_identifier_field(body.get("uuid"))
    if (
        body.get("msg_type") != "interactive"
        or receive_id is None
        or not _is_valid_preflight_card_content(content)
        or uuid is _INVALID
    ):
        return _INVALID

    sanitized_body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": content,
    }
    if uuid is not None:
        sanitized_body["uuid"] = uuid
    return {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": receive_id_type},
        "body": sanitized_body,
    }


def _is_valid_preflight_card_content(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PREFLIGHT_FEISHU_CONTENT_CHARS
    ):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _validated_failure_class(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_CLASS_RE.fullmatch(value):
        return value
    return fallback


def _validated_identifier_field(value: Any) -> str | None:
    if isinstance(value, str) and _is_safe_identifier(value):
        return value
    return None


def _validated_optional_identifier_field(value: Any) -> str | None | _InvalidSentinel:
    if value is None:
        return None
    identifier = _validated_identifier_field(value)
    return identifier if identifier is not None else _INVALID


def _validated_optional_failure_class_field(value: Any) -> str | None | _InvalidSentinel:
    if value is None:
        return None
    if isinstance(value, str) and _SAFE_CLASS_RE.fullmatch(value):
        return value
    return _INVALID


def _has_only_keys(value: Mapping[str, Any], allowed: frozenset[str] | set[str]) -> bool:
    return set(value.keys()).issubset(allowed)


def _has_exact_keys(value: Mapping[str, Any], expected: frozenset[str] | set[str]) -> bool:
    return set(value.keys()) == set(expected)


def _is_safe_event_type(value: str) -> bool:
    return bool(_SAFE_EVENT_TYPE_RE.fullmatch(value))


def _is_safe_identifier(value: str) -> bool:
    return bool(_SAFE_IDENTIFIER_RE.fullmatch(value))


def _is_json_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _byte_count(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def _line_count(value: str) -> int:
    return len(value.splitlines()) if value else 0


def _json_status(value: str) -> str:
    if not value or not value.strip():
        return "empty"
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return "invalid"
    if isinstance(decoded, dict):
        return "object"
    if isinstance(decoded, list):
        return "array"
    if isinstance(decoded, str):
        return "string"
    if isinstance(decoded, bool):
        return "bool"
    if decoded is None:
        return "null"
    if isinstance(decoded, (int, float)):
        return "number"
    return "unknown"


def _sanitize_text(text: str | None, state_dir: Path) -> str:
    if not text:
        return ""
    sanitized = redact_sensitive_text(str(text), force=True)

    replacements = {str(state_dir), str(state_dir.expanduser())}
    try:
        replacements.add(str(state_dir.resolve()))
    except OSError:
        pass
    replacements.add(str(Path.home()))

    home = str(Path.home())
    for value in sorted((item for item in replacements if item), key=len, reverse=True):
        placeholder = "<home>" if value == home else "<state-dir>"
        sanitized = sanitized.replace(value, placeholder)

    sanitized = _URL_RE.sub("<url>", sanitized)
    sanitized = _ABSOLUTE_PATH_RE.sub("<path>", sanitized)
    if len(sanitized) > _MAX_DIAGNOSTIC_CHARS:
        sanitized = sanitized[:_MAX_DIAGNOSTIC_CHARS] + "...[truncated]"
    return sanitized


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

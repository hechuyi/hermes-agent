"""Subprocess adapter for the hermes-tools gateway-event contract."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.redact import redact_sensitive_text


DEFAULT_HERMES_TOOLS_BINARY = "hermes-tools"
DEFAULT_TIMEOUT_SECONDS = 10

_SUPPORTED_ACTION_TYPES = frozenset(
    {
        "delivery_record",
        "stale_pending_alert",
        "session_state",
        "status_card",
    }
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])/(?:Users|home|var|tmp|private|Volumes|srv|etc)/[^\s\"'`,;)]*"
)
_URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>]+")
_MAX_DIAGNOSTIC_CHARS = 1200


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
        failure_class = _string_or_none(envelope.get("failure_class"))
        failure_class = failure_class or "hermes_tools_preflight_failed"
        detail = _string_or_none(envelope.get("failure_detail")) or failure_class
        return _failure(
            failure_class,
            failure_class,
            event_type="preflight",
            diagnostics=detail,
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

    if not isinstance(envelope.get("state_dir_writable"), bool) or not isinstance(
        envelope.get("checks"), list
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
            "checks": envelope.get("checks"),
            "feishu_request": envelope.get("feishu_request"),
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

    event_type = _string_or_none(envelope.get("event_type"))
    action = envelope.get("action")
    if event_type is None or not isinstance(action, dict):
        return _invalid_envelope("apply envelope missing event_type/action", state_dir)

    action_type = _string_or_none(action.get("type"))
    if action_type not in _SUPPORTED_ACTION_TYPES:
        return _failure(
            "unsupported_action",
            "unsupported action",
            event_type=event_type,
            diagnostics=f"unsupported action type={action_type or '<missing>'}",
            state_dir=state_dir,
        )

    return HermesToolsGatewayEventResult(
        ok=True,
        event_type=event_type,
        action=dict(action),
    )


def _failure_from_apply_error_envelope(
    envelope: dict[str, Any],
    completed: subprocess.CompletedProcess[str],
    state_dir: Path,
) -> HermesToolsGatewayEventResult:
    error = envelope.get("error")
    if not isinstance(error, dict):
        return _invalid_envelope(
            "apply error envelope missing error object",
            state_dir,
            diagnostics=_completed_diagnostics(completed),
        )

    reason = _string_or_none(error.get("reason"))
    message = _string_or_none(error.get("message")) or reason
    return _failure(
        reason or "hermes_tools_apply_failed",
        reason or "hermes-tools apply failed",
        event_type=_string_or_none(envelope.get("event_type")),
        diagnostics=message,
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
    parts = [f"returncode={completed.returncode}"]
    if completed.stdout:
        parts.append(f"stdout={completed.stdout}")
    if completed.stderr:
        parts.append(f"stderr={completed.stderr}")
    return " ".join(parts)


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

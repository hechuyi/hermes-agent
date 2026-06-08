"""Out-of-band guard for legacy Feishu surfaces."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Iterator

from gateway.gateway_event_contract import GatewayEventResult


LEGACY_FEISHU_BROKER_DENIAL_REASON = "feishu_legacy_tool_requires_broker"

_BROKER_CONTEXT: ContextVar["FeishuBrokerContext | None"] = ContextVar(
    "feishu_legacy_broker_context",
    default=None,
)
_CONTRACT_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_BROKER_GRANT_HANDLE_RE = re.compile(r"^broker_grant_handle:sha256:[a-f0-9]{64}$")
_BROKER_ACTION_ID_RE = re.compile(r"^broker_action:sha256:[a-f0-9]{64}$")
_ROUTE_SNAPSHOT_KEY_RE = re.compile(r"^route_snapshot:sha256:[a-f0-9]{64}$")


@dataclass(frozen=True)
class FeishuBrokerContext:
    grant_handle: str
    action_id: str
    contract_hash: str
    route_partition_key: str


@contextmanager
def feishu_broker_context(
    grant_handle: str,
    *,
    action_id: str,
    contract_hash: str,
    route_partition_key: str,
) -> Iterator[FeishuBrokerContext]:
    context = FeishuBrokerContext(
        grant_handle=_validated_prefixed_digest(
            "grant_handle",
            grant_handle,
            _BROKER_GRANT_HANDLE_RE,
            "broker_grant_handle:sha256:<64 lowercase hex>",
        ),
        action_id=_validated_prefixed_digest(
            "action_id",
            action_id,
            _BROKER_ACTION_ID_RE,
            "broker_action:sha256:<64 lowercase hex>",
        ),
        contract_hash=_validated_contract_hash(contract_hash),
        route_partition_key=_validated_prefixed_digest(
            "route_partition_key",
            route_partition_key,
            _ROUTE_SNAPSHOT_KEY_RE,
            "route_snapshot:sha256:<64 lowercase hex>",
        ),
    )
    token = _BROKER_CONTEXT.set(context)
    try:
        yield context
    finally:
        _BROKER_CONTEXT.reset(token)


def current_feishu_broker_context() -> FeishuBrokerContext | None:
    return _BROKER_CONTEXT.get()


def require_feishu_broker_context(
    kind: str,
    name: str,
    args: object = None,
) -> tuple[bool, str]:
    del kind, name, args
    if current_feishu_broker_context() is None:
        return False, LEGACY_FEISHU_BROKER_DENIAL_REASON
    return True, ""


def audit_feishu_legacy_tool_denial(
    *,
    surface: str,
    tool: str,
) -> GatewayEventResult:
    """Persist a sanitized legacy-tool denial event through the gateway ledger."""

    state_dir = _legacy_audit_state_dir()
    if state_dir is None:
        return GatewayEventResult(
            ok=False,
            event_type="feishu_legacy_tool_denied",
            failure_class="gateway_event_state_dir_missing",
            reason="gateway event state dir missing",
        )

    event = {
        "type": "feishu_legacy_tool_denied",
        "timestamp": time.time(),
        "correlation_id": _legacy_audit_correlation_id(),
        "event_hash": _legacy_audit_event_hash(surface, tool),
        "legacy_tool_hash": _fnv1a64(f"{surface}:{tool}:denied"),
        "tool": tool,
        "surface": surface,
        "failure_class": LEGACY_FEISHU_BROKER_DENIAL_REASON,
    }
    try:
        from gateway.gateway_event_ledger import apply_gateway_event

        return apply_gateway_event(event, state_dir)
    except Exception as exc:
        return GatewayEventResult(
            ok=False,
            event_type="feishu_legacy_tool_denied",
            failure_class="gateway_event_apply_failed",
            reason=type(exc).__name__,
        )


def _validated_contract_hash(value: str) -> str:
    if not isinstance(value, str) or _CONTRACT_HASH_RE.fullmatch(value) is None:
        raise ValueError("contract_hash must be sha256:<64 lowercase hex>")
    return value


def _legacy_audit_state_dir() -> Path | None:
    value = (
        os.getenv("HERMES_GATEWAY_EVENT_STATE_DIR", "")
        or os.getenv("HERMES_TOOLS_STATE_DIR", "")
    )
    if not value:
        return None
    return Path(value)


def _legacy_audit_correlation_id() -> str:
    return "feishu-legacy-denial"


def _legacy_audit_event_hash(surface: str, tool: str) -> str:
    value = os.getenv("HERMES_FEISHU_LEGACY_AUDIT_EVENT_HASH", "")
    if re.fullmatch(r"^sha256:[a-f0-9]{64}$", value):
        return value
    digest = hashlib.sha256(f"feishu_legacy_tool_denied:{surface}:{tool}".encode()).hexdigest()
    return f"sha256:{digest}"


def _fnv1a64(value: str) -> str:
    h = 0xCBF29CE484222325
    for byte in value.encode("utf-8", "surrogatepass"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{h:016x}"


def _validated_prefixed_digest(
    field: str,
    value: str,
    pattern: re.Pattern[str],
    expected: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} must be {expected}")
    return value

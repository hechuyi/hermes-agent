"""Out-of-band guard for legacy Feishu surfaces."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Iterator


LEGACY_FEISHU_BROKER_DENIAL_REASON = "feishu_legacy_tool_requires_broker"

_BROKER_CONTEXT: ContextVar["FeishuBrokerContext | None"] = ContextVar(
    "feishu_legacy_broker_context",
    default=None,
)
_CONTRACT_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RAW_MARKERS = (
    "token",
    "secret",
    "private_key",
    "open_id",
    "user_id",
    "union_id",
    "file_path",
    "path",
    "content",
)


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
        grant_handle=_validated_safe_atom("grant_handle", grant_handle),
        action_id=_validated_safe_atom("action_id", action_id),
        contract_hash=_validated_contract_hash(contract_hash),
        route_partition_key=_validated_safe_atom(
            "route_partition_key",
            route_partition_key,
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


def _validated_contract_hash(value: str) -> str:
    if not isinstance(value, str) or _CONTRACT_HASH_RE.fullmatch(value) is None:
        raise ValueError("contract_hash must be sha256:<64 lowercase hex>")
    return value


def _validated_safe_atom(field: str, value: str) -> str:
    if not isinstance(value, str) or _SAFE_ATOM_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sanitized safe atom")
    normalized = value.lower()
    if any(marker in normalized for marker in _RAW_MARKERS):
        raise ValueError(f"{field} contains an unsafe raw marker")
    return value

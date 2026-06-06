"""Compatibility facade for the internal Hermes gateway-event ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from gateway.gateway_event_contract import (
    GatewayEventResult,
    HermesToolsGatewayEventResult,
    validate_feishu_request_descriptor,
    validate_gateway_action,
)
from gateway.gateway_event_ledger import (
    apply_gateway_event as _apply_gateway_event,
    preflight_gateway_event,
)


def apply_gateway_event(
    event: Mapping[str, Any],
    state_dir: str | Path,
    *,
    timeout_seconds: int | float = 10,
    binary: str | None = None,
) -> GatewayEventResult:
    return _apply_gateway_event(
        event,
        state_dir,
        timeout_seconds=timeout_seconds,
        binary=binary,
    )


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


def _validated_action(action: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        return validate_gateway_action(action)
    except (TypeError, ValueError):
        return None


def _validated_feishu_request(descriptor: Mapping[str, Any]) -> dict[str, Any] | None:
    return validate_feishu_request_descriptor(descriptor)


__all__ = [
    "GatewayEventResult",
    "HermesToolsGatewayEventResult",
    "apply_gateway_event",
    "apply_gateway_event_async",
    "preflight_gateway_event",
]

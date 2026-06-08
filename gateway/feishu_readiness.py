"""Pure Feishu Package A readiness classification primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


FEISHU_PACKAGE_A_LEGACY_SURFACES: tuple[str, ...] = (
    "doc",
    "drive",
    "comment",
    "descriptor",
    "status-card",
    "gateway-exec-approval",
    "update-prompt",
    "generic-card",
    "reaction",
)

_UNKNOWN_DELIVERY_STATES = frozenset({"unknown", "unknown_delivery_state"})


@dataclass(frozen=True)
class FeishuReadinessEvidence:
    contract_hash: str | None = None
    observed_route_snapshot_hash: str | None = None
    expected_route_snapshot_hash: str | None = None
    object_action_requested: bool = False
    capability_granted: bool = False
    delivery_state: str | None = None
    redaction_failure: bool = False
    legacy_denial_reasons: tuple[str, ...] = ()
    checked_legacy_surfaces: tuple[str, ...] = ()
    denial_audit_available: bool = True


@dataclass(frozen=True)
class FeishuReadinessResult:
    status: str
    failure_class: str | None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def classify_feishu_package_a_readiness(
    evidence: FeishuReadinessEvidence,
) -> FeishuReadinessResult:
    blockers: list[str] = []
    warnings: list[str] = []

    if not evidence.contract_hash:
        blockers.append("feishu_contract_missing")

    if not evidence.expected_route_snapshot_hash:
        blockers.append("feishu_expected_route_snapshot_missing")

    if _route_snapshot_mismatch(evidence):
        blockers.append("feishu_route_snapshot_mismatch")

    if evidence.object_action_requested and not evidence.capability_granted:
        blockers.append("feishu_capability_missing")

    if _is_unknown_delivery_state(evidence.delivery_state):
        warnings.append("unknown_delivery_state")

    if evidence.redaction_failure:
        blockers.append("feishu_redaction_failed")

    warnings.extend(_stable_unique(evidence.legacy_denial_reasons))

    blockers.extend(
        f"feishu_legacy_surface_unchecked:{surface}"
        for surface in FEISHU_PACKAGE_A_LEGACY_SURFACES
        if surface not in set(evidence.checked_legacy_surfaces)
    )

    if not evidence.denial_audit_available:
        blockers.append("feishu_denial_audit_unavailable")

    blocker_tuple = tuple(blockers)
    warning_tuple = tuple(_stable_unique(warnings))
    if blocker_tuple:
        return FeishuReadinessResult(
            status="not_ready",
            failure_class=_base_failure_class(blocker_tuple[0]),
            blockers=blocker_tuple,
            warnings=warning_tuple,
        )
    if warning_tuple:
        return FeishuReadinessResult(
            status="degraded",
            failure_class=_base_failure_class(warning_tuple[0]),
            warnings=warning_tuple,
        )
    return FeishuReadinessResult(status="ready", failure_class=None)


def summarize_feishu_audit_readiness(
    events: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    *,
    expected_route_snapshot_hash: str | None = None,
    checked_legacy_surfaces: tuple[str, ...] = (),
    denial_audit_available: bool = True,
) -> FeishuReadinessEvidence:
    contract_hash: str | None = None
    observed_route_snapshot_hash: str | None = None
    object_action_requested = False
    capability_granted = False
    delivery_state: str | None = "known"
    redaction_failure = False
    legacy_denial_reasons: list[str] = []

    for event in events:
        event_type = event.get("type")
        if event_type == "feishu_contract_observed":
            contract_hash = _optional_string(event.get("contract_hash")) or contract_hash
            observed_route_snapshot_hash = (
                _optional_string(event.get("route_snapshot_hash"))
                or observed_route_snapshot_hash
            )
        elif event_type == "feishu_action_requested":
            object_action_requested = True
        elif event_type == "feishu_capability_granted":
            capability_granted = True
        elif event_type == "feishu_capability_denied":
            capability_granted = False
        elif event_type == "unknown_delivery_state":
            delivery_state = "unknown_delivery_state"
        elif event_type == "feishu_tool_result_redacted":
            if event.get("failure_class") == "feishu_redaction_failed":
                redaction_failure = True
        elif event_type in {
            "feishu_legacy_tool_denied",
            "feishu_legacy_descriptor_denied",
        }:
            failure_class = _optional_string(event.get("failure_class"))
            if failure_class:
                legacy_denial_reasons.append(failure_class)

    return FeishuReadinessEvidence(
        contract_hash=contract_hash,
        observed_route_snapshot_hash=observed_route_snapshot_hash,
        expected_route_snapshot_hash=expected_route_snapshot_hash,
        object_action_requested=object_action_requested,
        capability_granted=capability_granted,
        delivery_state=delivery_state,
        redaction_failure=redaction_failure,
        legacy_denial_reasons=tuple(_stable_unique(legacy_denial_reasons)),
        checked_legacy_surfaces=checked_legacy_surfaces,
        denial_audit_available=denial_audit_available,
    )


def _route_snapshot_mismatch(evidence: FeishuReadinessEvidence) -> bool:
    if evidence.expected_route_snapshot_hash is None:
        return False
    return evidence.observed_route_snapshot_hash != evidence.expected_route_snapshot_hash


def _is_unknown_delivery_state(value: str | None) -> bool:
    return value in _UNKNOWN_DELIVERY_STATES


def _base_failure_class(value: str) -> str:
    return value.split(":", 1)[0]


def _stable_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

"""Offline Feishu render and action intent contracts.

This module is deliberately pure: it describes sanitized render/action plans
and validates fail-closed preconditions without calling Feishu APIs, SDKs, or
business tools.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gateway.feishu_contracts import FeishuContractError, feishu_contract_hash


_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_FALLBACK_ACTIONS = frozenset({"preserve", "replace", "drop"})
_SUPPORTED_PART_TYPES = frozenset(
    {
        "post",
        "markdown",
        "plain_text",
        "table",
        "code_block",
        "link",
        "image",
        "file",
        "card",
        "button",
    }
)
_ACTION_PART_TYPES = frozenset({"card", "button"})
_ATTACHMENT_PART_TYPES = frozenset({"image", "file"})
_RENDER_MODES = frozenset({"offline_snapshot"})
_ATTACHMENT_SOURCE_CLASSES = frozenset(
    {"upload", "generated", "cached", "object_store", "sanitized_reference"}
)
_RAW_TOOL_KEYS = frozenset(
    {
        "args",
        "argument",
        "arguments",
        "body",
        "method",
        "path",
        "rawargs",
        "rawarguments",
        "rawbody",
        "rawpath",
        "rawtoolarguments",
        "toolargs",
        "toolarguments",
        "toolbody",
        "toolmethod",
        "toolpath",
    }
)
_SENSITIVE_METADATA_MARKERS = frozenset(
    {
        "token",
        "secret",
        "privatekey",
        "rawmessage",
        "documentcontent",
        "filepath",
        "openid",
        "userid",
        "unionid",
    }
)


@dataclass(frozen=True)
class FeishuActionContract:
    action_kind: str
    action_digest: str | None
    same_operator_scope: bool
    expires_at: str | None
    route_snapshot_hash: str | None
    payload_hash: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    contract_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_hash",
            _stable_hash(
                _action_contract_payload(self),
                domain="feishu.action_contract",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class RenderPlanPart:
    part_type: str
    payload_hash: str | None = None
    content_hash: str | None = None
    provenance_hash: str | None = None
    fallback_action: str | None = None
    chunk_group: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    source_class: str | None = None
    action_digest: str | None = None
    route_snapshot_hash: str | None = None
    same_operator_scope: bool | None = None
    expires_at: str | None = None
    action_contract: FeishuActionContract | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    part_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "part_hash",
            _stable_hash(
                _render_part_payload(self),
                domain="feishu.render_plan_part",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class RenderPlan:
    target_ref_hash: str
    object_ref_hash: str | None
    render_mode: str
    parts: tuple[RenderPlanPart, ...]
    schema_version: int = 1
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", _part_tuple(self.parts))
        valid, failure_class = _validate_render_plan_fields(self)
        if not valid:
            raise FeishuContractError(
                "invalid Feishu render plan",
                failure_class=failure_class or "feishu_render_plan_invalid",
            )
        object.__setattr__(
            self,
            "plan_hash",
            _stable_hash(
                {
                    "target_ref_hash": self.target_ref_hash,
                    "object_ref_hash": self.object_ref_hash,
                    "render_mode": self.render_mode,
                    "parts": tuple(part.part_hash for part in self.parts),
                    "schema_version": self.schema_version,
                },
                domain="feishu.render_plan",
                schema_version=self.schema_version,
            ),
        )


def validate_render_part(part: RenderPlanPart) -> tuple[bool, str | None]:
    if not isinstance(part, RenderPlanPart):
        return False, "feishu_render_part_invalid"
    if part.part_type not in _SUPPORTED_PART_TYPES:
        return False, "feishu_render_part_type_invalid"
    if part.fallback_action not in _FALLBACK_ACTIONS:
        return False, "feishu_render_fallback_invalid"
    if _contains_raw_tool_material(part.metadata):
        return False, "feishu_action_raw_tool_material"
    if not _chunk_is_coherent(part):
        return False, "feishu_render_chunk_invalid"
    if part.part_type in _ATTACHMENT_PART_TYPES:
        attachment_valid, attachment_failure = _validate_attachment_part(part)
        if not attachment_valid:
            return False, attachment_failure
    hash_valid, hash_failure = _validate_render_part_hash_fields(part)
    if not hash_valid:
        return False, hash_failure
    if part.part_type in _ACTION_PART_TYPES:
        if part.action_contract is None:
            return False, "feishu_action_contract_missing"
        contract_valid, contract_failure = validate_action_contract(part.action_contract)
        if not contract_valid:
            return False, contract_failure
        sibling_valid, sibling_failure = _validate_action_sibling_fields(part)
        if not sibling_valid:
            return False, sibling_failure
    return True, None


def validate_render_plan(plan: RenderPlan) -> tuple[bool, str | None]:
    if not isinstance(plan, RenderPlan):
        return False, "feishu_render_plan_invalid"
    fields_valid, fields_failure = _validate_render_plan_fields(plan)
    if not fields_valid:
        return False, fields_failure
    for part in plan.parts:
        part_valid, part_failure = validate_render_part(part)
        if not part_valid:
            return False, part_failure
    return True, None


def validate_action_contract(
    contract: FeishuActionContract,
) -> tuple[bool, str | None]:
    if not isinstance(contract, FeishuActionContract):
        return False, "feishu_action_contract_invalid"
    if _contains_raw_tool_material(contract.metadata):
        return False, "feishu_action_raw_tool_material"
    if contract.action_digest is None:
        return False, "feishu_action_digest_missing"
    if not _is_hash(contract.action_digest):
        return False, "feishu_action_digest_invalid"
    if contract.same_operator_scope is not True:
        return False, "feishu_action_scope_not_same_operator"
    if contract.expires_at is None:
        return False, "feishu_action_expiry_missing"
    if not isinstance(contract.expires_at, str) or not contract.expires_at:
        return False, "feishu_action_expiry_invalid"
    if contract.route_snapshot_hash is None:
        return False, "feishu_action_route_missing"
    if not _is_hash(contract.route_snapshot_hash):
        return False, "feishu_action_route_invalid"
    if contract.payload_hash is None:
        return False, "feishu_action_payload_hash_missing"
    if not _is_hash(contract.payload_hash):
        return False, "feishu_action_payload_hash_invalid"
    return True, None


def _validate_attachment_part(part: RenderPlanPart) -> tuple[bool, str | None]:
    if not isinstance(part.source_class, str) or not part.source_class:
        return False, "feishu_render_attachment_source_missing"
    if part.source_class not in _ATTACHMENT_SOURCE_CLASSES:
        return False, "feishu_render_attachment_source_invalid"
    if part.provenance_hash is None:
        return False, "feishu_render_attachment_provenance_missing"
    if not _is_hash(part.provenance_hash):
        return False, "feishu_render_attachment_provenance_invalid"
    return True, None


def _validate_render_part_hash_fields(
    part: RenderPlanPart,
) -> tuple[bool, str | None]:
    hash_fields = (
        ("payload_hash", part.payload_hash, "feishu_render_payload_hash_invalid"),
        ("content_hash", part.content_hash, "feishu_render_content_hash_invalid"),
        (
            "provenance_hash",
            part.provenance_hash,
            "feishu_render_provenance_hash_invalid",
        ),
        ("action_digest", part.action_digest, "feishu_action_sibling_digest_invalid"),
        (
            "route_snapshot_hash",
            part.route_snapshot_hash,
            "feishu_action_sibling_route_invalid",
        ),
    )
    for _field_name, value, failure_class in hash_fields:
        if value is not None and not _is_hash(value):
            return False, failure_class
    return True, None


def _validate_action_sibling_fields(part: RenderPlanPart) -> tuple[bool, str | None]:
    contract = part.action_contract
    if contract is None:
        return False, "feishu_action_contract_missing"
    comparisons = (
        (part.action_digest, contract.action_digest),
        (part.route_snapshot_hash, contract.route_snapshot_hash),
        (part.same_operator_scope, contract.same_operator_scope),
        (part.expires_at, contract.expires_at),
    )
    for sibling_value, contract_value in comparisons:
        if sibling_value is not None and sibling_value != contract_value:
            return False, "feishu_action_sibling_mismatch"
    return True, None


def _validate_render_plan_fields(plan: RenderPlan) -> tuple[bool, str | None]:
    if not _is_hash(plan.target_ref_hash):
        return False, "feishu_render_target_ref_invalid"
    if plan.object_ref_hash is not None and not _is_hash(plan.object_ref_hash):
        return False, "feishu_render_object_ref_invalid"
    if plan.render_mode not in _RENDER_MODES:
        return False, "feishu_render_mode_invalid"
    if not _chunk_groups_are_complete(plan.parts):
        return False, "feishu_render_chunk_group_incomplete"
    return True, None


def _chunk_groups_are_complete(parts: Sequence[RenderPlanPart]) -> bool:
    groups: dict[str, dict[str, Any]] = {}
    for part in parts:
        if part.chunk_group is None:
            continue
        if (
            not _is_hash(part.chunk_group)
            or not isinstance(part.chunk_index, int)
            or isinstance(part.chunk_index, bool)
            or not isinstance(part.chunk_count, int)
            or isinstance(part.chunk_count, bool)
        ):
            return False
        group = groups.setdefault(
            part.chunk_group,
            {"count": part.chunk_count, "indexes": set()},
        )
        if group["count"] != part.chunk_count:
            return False
        if part.chunk_index in group["indexes"]:
            return False
        group["indexes"].add(part.chunk_index)
    for group in groups.values():
        count = group["count"]
        if count <= 0 or group["indexes"] != set(range(count)):
            return False
    return True


def _chunk_is_coherent(part: RenderPlanPart) -> bool:
    chunk_values = (part.chunk_group, part.chunk_index, part.chunk_count)
    if all(value is None for value in chunk_values):
        return True
    if any(value is None for value in chunk_values):
        return False
    if not _is_hash(part.chunk_group):
        return False
    if (
        not isinstance(part.chunk_index, int)
        or isinstance(part.chunk_index, bool)
        or not isinstance(part.chunk_count, int)
        or isinstance(part.chunk_count, bool)
    ):
        return False
    return 0 <= part.chunk_index < part.chunk_count


def _contains_raw_tool_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _normalized_key(key) in _RAW_TOOL_KEYS:
                return True
            if _contains_raw_tool_material(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return any(_contains_raw_tool_material(item) for item in value)
    return False


def _contains_sensitive_metadata_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_raw_metadata_key(key):
                return True
            if _contains_sensitive_metadata_key(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return any(_contains_sensitive_metadata_key(item) for item in value)
    return False


def _is_sensitive_raw_metadata_key(key: str) -> bool:
    comparable = _normalized_key(key)
    for marker in _SENSITIVE_METADATA_MARKERS:
        if marker in comparable and not comparable.endswith("hash"):
            return True
    return False


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _part_tuple(parts: Sequence[RenderPlanPart]) -> tuple[RenderPlanPart, ...]:
    if not isinstance(parts, Sequence) or isinstance(parts, (bytes, bytearray, str)):
        raise FeishuContractError(
            "render plan parts must be a sequence",
            failure_class="feishu_render_parts_invalid",
        )
    part_tuple = tuple(parts)
    if not part_tuple:
        raise FeishuContractError(
            "render plan parts must not be empty",
            failure_class="feishu_render_parts_missing",
        )
    for part in part_tuple:
        if not isinstance(part, RenderPlanPart):
            raise FeishuContractError(
                "render plan parts must be RenderPlanPart instances",
                failure_class="feishu_render_part_invalid",
            )
    return part_tuple


def _render_part_payload(part: RenderPlanPart) -> dict[str, Any]:
    return {
        "part_type": part.part_type,
        "payload_hash": part.payload_hash,
        "content_hash": part.content_hash,
        "provenance_hash": part.provenance_hash,
        "fallback_action": part.fallback_action,
        "chunk_group": part.chunk_group,
        "chunk_index": part.chunk_index,
        "chunk_count": part.chunk_count,
        "source_class": part.source_class,
        "action_digest": part.action_digest,
        "route_snapshot_hash": part.route_snapshot_hash,
        "same_operator_scope": part.same_operator_scope,
        "expires_at": part.expires_at,
        "action_contract_hash": (
            part.action_contract.contract_hash if part.action_contract else None
        ),
        "metadata_hash": _metadata_hash(part.metadata),
        "schema_version": part.schema_version,
    }


def _action_contract_payload(contract: FeishuActionContract) -> dict[str, Any]:
    return {
        "action_kind": contract.action_kind,
        "action_digest": contract.action_digest,
        "same_operator_scope": contract.same_operator_scope,
        "expires_at": contract.expires_at,
        "route_snapshot_hash": contract.route_snapshot_hash,
        "payload_hash": contract.payload_hash,
        "metadata_hash": _metadata_hash(contract.metadata),
        "schema_version": contract.schema_version,
    }


def _metadata_hash(metadata: Mapping[str, Any]) -> str | None:
    if not metadata:
        return None
    if _contains_raw_tool_material(metadata):
        return feishu_contract_hash(
            {"redaction_failure": "feishu_action_raw_tool_material"},
            domain="feishu.action_plan_metadata",
            version="v1",
            schema_version=1,
        )
    if _contains_sensitive_metadata_key(metadata):
        raise FeishuContractError(
            "sensitive raw metadata is not allowed in Feishu action plans",
            failure_class="feishu_action_sensitive_metadata",
        )
    return feishu_contract_hash(
        metadata,
        domain="feishu.action_plan_metadata",
        version="v1",
        schema_version=1,
    )


def _stable_hash(value: Any, *, domain: str, schema_version: int) -> str:
    return feishu_contract_hash(
        value,
        domain=domain,
        version="v1",
        schema_version=schema_version,
    )

"""Pure Feishu contract primitives.

The helpers in this module intentionally handle only sanitized contract
material. Raw Feishu identifiers, message bodies, document content, file paths,
tokens, and secrets must be represented by stable hashes or typed metadata
before they reach these contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


class FeishuContractError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        failure_class: str = "invalid_feishu_contract",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


_SENSITIVE_KEY_MARKERS = frozenset(
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
_ALLOWED_SENSITIVE_METADATA_KEYS = frozenset({"tokenclass"})
_NORMALIZED_KEY_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_P3_OBJECT_ACTIONS = frozenset(
    {
        "delete",
        "write",
        "update",
        "share",
        "comment",
        "permission_update",
        "doc.delete",
        "doc.write",
        "doc.update",
        "doc.share",
        "doc.comment",
        "doc.permission_update",
    }
)
_OBJECT_AUTHORITY_EVIDENCE_KINDS = frozenset(
    {
        "object_acl",
        "object_capability",
        "broker_object_authority",
    }
)


@dataclass(frozen=True)
class HashedRef:
    kind: str
    value_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_nonempty_string(self.kind, "hashed ref kind")
        _require_sha256_hash(self.value_hash, "hashed ref value_hash")
        _require_schema_version(self.schema_version)


@dataclass(frozen=True)
class ConversationContract:
    platform_account_id: str
    conversation_scope_id: str
    route_partition_key: str
    route_session_key_snapshot: str
    scope_assignment_status: str
    authority_subject_ref: HashedRef | None
    identity_evidence_set: tuple[HashedRef, ...] = ()
    schema_version: int = 1
    contract_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.platform_account_id, "platform_account_id")
        _require_nonempty_string(self.conversation_scope_id, "conversation_scope_id")
        _require_nonempty_string(self.route_partition_key, "route_partition_key")
        _require_nonempty_string(
            self.route_session_key_snapshot,
            "route_session_key_snapshot",
        )
        _require_nonempty_string(
            self.scope_assignment_status,
            "scope_assignment_status",
        )
        _require_optional_hashed_ref(
            self.authority_subject_ref,
            "authority_subject_ref",
        )
        object.__setattr__(
            self,
            "identity_evidence_set",
            _hashed_ref_tuple(self.identity_evidence_set, "identity_evidence_set"),
        )
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "contract_hash",
            feishu_contract_hash(
                _conversation_contract_payload(self),
                domain="feishu.conversation_contract",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class AuthorizationEvidence:
    evidence_kind: str
    authority_subject_ref: HashedRef | None
    route_session_key_snapshot: str
    object_ref: HashedRef | None = None
    scopes: tuple[str, ...] = ()
    token_class: str | None = None
    schema_version: int = 1
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.evidence_kind, "evidence_kind")
        _require_optional_hashed_ref(
            self.authority_subject_ref,
            "authority_subject_ref",
        )
        _require_nonempty_string(
            self.route_session_key_snapshot,
            "route_session_key_snapshot",
        )
        _require_optional_hashed_ref(self.object_ref, "object_ref")
        object.__setattr__(self, "scopes", _scope_tuple(self.scopes))
        if self.token_class is not None:
            _require_nonempty_string(self.token_class, "token_class")
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "evidence_hash",
            feishu_contract_hash(
                _authorization_evidence_payload(self),
                domain="feishu.authorization_evidence",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class ObjectCapabilityGrant:
    contract_hash: str
    evidence_hashes: tuple[str, ...]
    object_type: str
    object_ref: HashedRef
    action: str
    authority_subject_ref: HashedRef
    schema_version: int = 1
    grant_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256_hash(self.contract_hash, "contract_hash")
        object.__setattr__(
            self,
            "evidence_hashes",
            _hash_tuple(self.evidence_hashes, "evidence_hashes"),
        )
        if not self.evidence_hashes:
            raise FeishuContractError("evidence_hashes must not be empty")
        _require_nonempty_string(self.object_type, "object_type")
        _require_hashed_ref(self.object_ref, "object_ref")
        _require_nonempty_string(self.action, "action")
        _require_hashed_ref(self.authority_subject_ref, "authority_subject_ref")
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "grant_hash",
            feishu_contract_hash(
                _object_capability_grant_payload(self),
                domain="feishu.object_capability_grant",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


def canonical_contract_json(value: Any) -> str:
    """Return deterministic JSON for sanitized Feishu contract material."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def feishu_contract_hash(
    value: Any,
    *,
    domain: str,
    version: str,
    schema_version: int = 1,
    algorithm: str = "sha256",
) -> str:
    if algorithm != "sha256":
        raise FeishuContractError(
            "unsupported contract hash algorithm",
            failure_class="unsupported_contract_hash_algorithm",
        )
    if not isinstance(domain, str) or not domain:
        raise FeishuContractError("contract hash domain must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise FeishuContractError("contract hash version must be a non-empty string")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise FeishuContractError("contract schema_version must be an integer")

    envelope = {
        "algorithm": algorithm,
        "domain": unicodedata.normalize("NFC", domain),
        "schema_version": schema_version,
        "value": value,
        "version": unicodedata.normalize("NFC", version),
    }
    canonical = canonical_contract_json(envelope)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def can_issue_object_grant(
    contract: ConversationContract,
    evidence: AuthorizationEvidence | Sequence[AuthorizationEvidence],
    *,
    object_type: str,
    action: str,
) -> tuple[bool, str | None]:
    if not isinstance(contract, ConversationContract):
        return False, "feishu_contract_invalid"
    if contract.scope_assignment_status != "scoped":
        return False, "feishu_scope_not_scoped"
    if contract.authority_subject_ref is None:
        return False, "feishu_authority_subject_missing"
    _require_nonempty_string(object_type, "object_type")
    _require_nonempty_string(action, "action")

    evidences = _evidence_tuple(evidence)
    if not evidences:
        return False, "feishu_authorization_evidence_missing"
    if any(
        item.route_session_key_snapshot != contract.route_session_key_snapshot
        for item in evidences
    ):
        return False, "feishu_route_snapshot_mismatch"
    if all(item.evidence_kind == "discovery" for item in evidences):
        return False, "feishu_discovery_only_evidence"
    if all(_is_app_token_only_evidence(item) for item in evidences):
        return False, "feishu_app_token_only_evidence"
    if _is_p3_object_action(object_type, action) and all(
        item.evidence_kind == "explicit_confirmation" for item in evidences
    ):
        return False, "feishu_p3_requires_object_authority_evidence"

    for item in evidences:
        if not _is_object_authority_evidence(item):
            continue
        if item.authority_subject_ref != contract.authority_subject_ref:
            continue
        if item.object_ref is None:
            continue
        required_scope = f"{object_type}:{action}"
        if required_scope not in item.scopes:
            continue
        return True, None

    if all(
        item.authority_subject_ref is not None
        and item.authority_subject_ref != contract.authority_subject_ref
        for item in evidences
    ):
        return False, "feishu_authority_subject_mismatch"
    return False, "feishu_object_authority_evidence_missing"


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FeishuContractError("contract numeric value must be finite")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_canonical_value(item) for item in value]
    raise FeishuContractError(
        f"unsupported contract value type: {type(value).__name__}",
    )


def _canonical_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise FeishuContractError("contract mapping keys must be strings")
        normalized_key = unicodedata.normalize("NFC", key)
        _reject_sensitive_raw_key(normalized_key)
        if normalized_key in normalized:
            raise FeishuContractError(
                "contract mapping has duplicate keys after Unicode normalization",
            )
        normalized[normalized_key] = _canonical_value(item)
    return normalized


def _reject_sensitive_raw_key(key: str) -> None:
    comparable = _normalized_key_for_policy(key)
    if comparable in _ALLOWED_SENSITIVE_METADATA_KEYS:
        return
    if key.lower().endswith("_hash"):
        return
    for marker in _SENSITIVE_KEY_MARKERS:
        if marker in comparable:
            raise FeishuContractError(
                f"sensitive raw field is not allowed in Feishu contracts: {key}",
                failure_class="sensitive_raw_field",
            )


def _normalized_key_for_policy(key: str) -> str:
    normalized = unicodedata.normalize("NFC", key).lower()
    return _NORMALIZED_KEY_CHARS_RE.sub("", normalized)


def _conversation_contract_payload(contract: ConversationContract) -> dict[str, Any]:
    return {
        "platform_account_id": contract.platform_account_id,
        "conversation_scope_id": contract.conversation_scope_id,
        "route_partition_key": contract.route_partition_key,
        "route_session_key_snapshot": contract.route_session_key_snapshot,
        "scope_assignment_status": contract.scope_assignment_status,
        "authority_subject_ref": contract.authority_subject_ref,
        "identity_evidence_set": contract.identity_evidence_set,
    }


def _authorization_evidence_payload(evidence: AuthorizationEvidence) -> dict[str, Any]:
    return {
        "evidence_kind": evidence.evidence_kind,
        "authority_subject_ref": evidence.authority_subject_ref,
        "route_session_key_snapshot": evidence.route_session_key_snapshot,
        "object_ref": evidence.object_ref,
        "scopes": evidence.scopes,
        "token_class": evidence.token_class,
    }


def _object_capability_grant_payload(grant: ObjectCapabilityGrant) -> dict[str, Any]:
    return {
        "contract_hash": grant.contract_hash,
        "evidence_hashes": grant.evidence_hashes,
        "object_type": grant.object_type,
        "object_ref": grant.object_ref,
        "action": grant.action,
        "authority_subject_ref": grant.authority_subject_ref,
    }


def _evidence_tuple(
    evidence: AuthorizationEvidence | Sequence[AuthorizationEvidence],
) -> tuple[AuthorizationEvidence, ...]:
    if isinstance(evidence, AuthorizationEvidence):
        return (evidence,)
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes, bytearray)):
        items = tuple(evidence)
        for item in items:
            if not isinstance(item, AuthorizationEvidence):
                raise FeishuContractError("evidence must contain AuthorizationEvidence")
        return items
    raise FeishuContractError("evidence must be AuthorizationEvidence")


def _hashed_ref_tuple(value: Any, field_name: str) -> tuple[HashedRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise FeishuContractError(f"{field_name} must be a sequence")
    items = tuple(value)
    for item in items:
        _require_hashed_ref(item, field_name)
    return items


def _hash_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise FeishuContractError(f"{field_name} must be a sequence")
    items = tuple(value)
    for item in items:
        _require_sha256_hash(item, field_name)
    return items


def _scope_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise FeishuContractError("scopes must be a sequence")
    items = tuple(value)
    for item in items:
        _require_nonempty_string(item, "scope")
    return items


def _require_hashed_ref(value: Any, field_name: str) -> None:
    if not isinstance(value, HashedRef):
        raise FeishuContractError(f"{field_name} must be a HashedRef")


def _require_optional_hashed_ref(value: Any, field_name: str) -> None:
    if value is not None:
        _require_hashed_ref(value, field_name)


def _require_sha256_hash(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_HASH_RE.fullmatch(value):
        raise FeishuContractError(f"{field_name} must be a sha256 hash")


def _require_nonempty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise FeishuContractError(f"{field_name} must be a non-empty string")


def _require_schema_version(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FeishuContractError("schema_version must be a positive integer")


def _is_app_token_only_evidence(evidence: AuthorizationEvidence) -> bool:
    return (
        evidence.evidence_kind == "app_token"
        or evidence.token_class == "app_access_token"
    )


def _is_object_authority_evidence(evidence: AuthorizationEvidence) -> bool:
    if evidence.evidence_kind not in _OBJECT_AUTHORITY_EVIDENCE_KINDS:
        return False
    return not _is_app_token_only_evidence(evidence)


def _is_p3_object_action(object_type: str, action: str) -> bool:
    action_key = action.lower()
    typed_action_key = f"{object_type.lower()}.{action_key}"
    return action_key in _P3_OBJECT_ACTIONS or typed_action_key in _P3_OBJECT_ACTIONS

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

from gateway.conversation_scope import ConversationScopeIdentity


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
_NORMALIZED_KEY_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_TOKEN_CLASSES = frozenset(
    {
        "none",
        "app_token",
        "app_access_token",
        "tenant_access_token",
        "user_token",
        "user_access_token",
        "delegated_user_token",
        "system_test_credential",
        "app_owned_object_credential",
    }
)
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
_CONVERSATION_CONTRACT_HASH_DOMAIN = "feishu.conversation_contract"
_CONVERSATION_CONTRACT_HASH_VERSION = "v1"
_AUTHORIZATION_EVIDENCE_KINDS = frozenset(
    {
        "verified_object_acl",
        "user_delegated_credential",
        "admin_policy_grant",
        "app_owned_object",
        "explicit_user_confirmation",
        "system_test_object",
        "app_token_only",
        "discovery_only",
    }
)
_EVIDENCE_STATES = frozenset({"current", "stale", "revoked"})
_OBJECT_AUTHORITY_EVIDENCE_KINDS = frozenset(
    {
        "verified_object_acl",
        "user_delegated_credential",
        "admin_policy_grant",
        "app_owned_object",
        "system_test_object",
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
    tenant_partition_key: str
    app_partition_key: str
    conversation_scope_id: str
    shared_context_scope_id: str
    route_partition_key: str
    route_session_key_snapshot: str
    scope_assignment_status: str
    actor_ref: HashedRef
    authority_subject_ref: HashedRef | None
    session_id: str
    thread_anchor_ref: HashedRef | None
    root_anchor_ref: HashedRef | None
    policy_version: str
    evidence_state: str
    identity_evidence_set: tuple[HashedRef, ...] = ()
    contract_hash_domain: str = _CONVERSATION_CONTRACT_HASH_DOMAIN
    contract_hash_version: str = _CONVERSATION_CONTRACT_HASH_VERSION
    schema_version: int = 1
    contract_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.platform_account_id, "platform_account_id")
        _require_nonempty_string(self.tenant_partition_key, "tenant_partition_key")
        _require_nonempty_string(self.app_partition_key, "app_partition_key")
        _require_nonempty_string(self.conversation_scope_id, "conversation_scope_id")
        _require_nonempty_string(
            self.shared_context_scope_id,
            "shared_context_scope_id",
        )
        _require_nonempty_string(self.route_partition_key, "route_partition_key")
        _require_nonempty_string(
            self.route_session_key_snapshot,
            "route_session_key_snapshot",
        )
        _require_nonempty_string(
            self.scope_assignment_status,
            "scope_assignment_status",
        )
        _require_hashed_ref(self.actor_ref, "actor_ref")
        _require_optional_hashed_ref(
            self.authority_subject_ref,
            "authority_subject_ref",
        )
        _require_nonempty_string(self.session_id, "session_id")
        _require_optional_hashed_ref(self.thread_anchor_ref, "thread_anchor_ref")
        _require_optional_hashed_ref(self.root_anchor_ref, "root_anchor_ref")
        _require_nonempty_string(self.policy_version, "policy_version")
        _require_known_value(self.evidence_state, _EVIDENCE_STATES, "evidence_state")
        object.__setattr__(
            self,
            "identity_evidence_set",
            _hashed_ref_tuple(self.identity_evidence_set, "identity_evidence_set"),
        )
        _require_nonempty_string(self.contract_hash_domain, "contract_hash_domain")
        _require_nonempty_string(self.contract_hash_version, "contract_hash_version")
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "contract_hash",
            feishu_contract_hash(
                _conversation_contract_payload(self),
                domain=self.contract_hash_domain,
                version=self.contract_hash_version,
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
    evidence_state: str = "current"
    schema_version: int = 1
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_known_value(
            self.evidence_kind,
            _AUTHORIZATION_EVIDENCE_KINDS,
            "evidence_kind",
        )
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
        _require_token_class(self.token_class)
        _require_known_value(self.evidence_state, _EVIDENCE_STATES, "evidence_state")
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


def feishu_hashed_ref(
    kind: str,
    value: str | None,
    *,
    schema_version: int = 1,
) -> HashedRef | None:
    if value is None:
        return None
    _require_nonempty_string(kind, "hashed ref kind")
    _require_nonempty_string(value, "hashed ref value")
    _require_schema_version(schema_version)

    payload = {
        "kind": unicodedata.normalize("NFC", kind),
        "schema_version": schema_version,
        "value": unicodedata.normalize("NFC", value),
    }
    digest = hashlib.sha256(
        canonical_contract_json(
            {
                "domain": "feishu.hashed_ref",
                "payload": payload,
                "version": "v1",
            }
        ).encode("utf-8")
    ).hexdigest()
    return HashedRef(
        kind=kind,
        value_hash=f"sha256:{digest}",
        schema_version=schema_version,
    )


def build_feishu_conversation_contract(
    *,
    scope_identity: ConversationScopeIdentity,
    route_partition_key: str,
    route_session_key_snapshot: str,
    scope_assignment_status: str,
    actor_ref: HashedRef,
    authority_subject_ref: HashedRef | None,
    identity_evidence_set: Sequence[HashedRef] = (),
    session_id: str,
    tenant_partition_key: str,
    app_partition_key: str,
    shared_context_scope_id: str | None = None,
    thread_anchor_ref: HashedRef | None = None,
    root_anchor_ref: HashedRef | None = None,
    policy_version: str = "policy:v1",
    evidence_state: str = "current",
) -> ConversationContract:
    if not isinstance(scope_identity, ConversationScopeIdentity):
        raise FeishuContractError(
            "scope_identity must be a ConversationScopeIdentity",
            failure_class="invalid_feishu_scope_identity",
        )
    _require_nonempty_string(scope_identity.platform_account_id, "platform_account_id")
    _require_nonempty_string(scope_identity.id, "conversation_scope_id")

    evidence_tuple = _hashed_ref_tuple(identity_evidence_set, "identity_evidence_set")
    if scope_assignment_status == "scoped":
        if authority_subject_ref is None or not evidence_tuple:
            raise FeishuContractError(
                "scoped Feishu conversation contract requires authority subject and identity evidence",
                failure_class="feishu_scoped_identity_incomplete",
            )

    return ConversationContract(
        platform_account_id=scope_identity.platform_account_id,
        tenant_partition_key=tenant_partition_key,
        app_partition_key=app_partition_key,
        conversation_scope_id=scope_identity.id,
        shared_context_scope_id=shared_context_scope_id
        or f"shared_context:{scope_identity.id}",
        route_partition_key=route_partition_key,
        route_session_key_snapshot=route_session_key_snapshot,
        scope_assignment_status=scope_assignment_status,
        actor_ref=actor_ref,
        authority_subject_ref=authority_subject_ref,
        session_id=session_id,
        thread_anchor_ref=thread_anchor_ref,
        root_anchor_ref=root_anchor_ref,
        identity_evidence_set=evidence_tuple,
        policy_version=policy_version,
        evidence_state=evidence_state,
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
    _require_schema_version(schema_version)

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
    object_ref: HashedRef,
    action: str,
) -> tuple[bool, str | None]:
    if not isinstance(contract, ConversationContract):
        return False, "feishu_contract_invalid"
    if contract.scope_assignment_status != "scoped":
        return False, "feishu_scope_not_scoped"
    if contract.authority_subject_ref is None:
        return False, "feishu_authority_subject_missing"
    _require_nonempty_string(object_type, "object_type")
    _require_hashed_ref(object_ref, "object_ref")
    _require_nonempty_string(action, "action")

    evidences = _evidence_tuple(evidence)
    if not evidences:
        return False, "feishu_authorization_evidence_missing"
    if any(
        item.route_session_key_snapshot != contract.route_session_key_snapshot
        for item in evidences
    ):
        return False, "feishu_route_snapshot_mismatch"
    if contract.evidence_state == "stale":
        return False, "feishu_contract_evidence_stale"
    if contract.evidence_state == "revoked":
        return False, "feishu_contract_evidence_revoked"
    if all(item.evidence_kind == "discovery_only" for item in evidences):
        return False, "feishu_discovery_only_evidence"
    if all(_is_app_token_only_evidence(item) for item in evidences):
        return False, "feishu_app_token_only_evidence"
    if _is_p3_object_action(object_type, action) and all(
        item.evidence_kind == "explicit_user_confirmation" for item in evidences
    ):
        return False, "feishu_p3_requires_object_authority_evidence"

    required_scope = f"{object_type}:{action}"
    saw_authority_subject_mismatch = False
    saw_object_mismatch = False
    saw_revoked = False
    saw_stale = False
    saw_insufficient_scope = False

    for item in evidences:
        if not _is_object_authority_evidence(item):
            continue
        if item.authority_subject_ref != contract.authority_subject_ref:
            saw_authority_subject_mismatch = (
                saw_authority_subject_mismatch or item.authority_subject_ref is not None
            )
            continue
        if item.object_ref is None:
            continue
        if item.object_ref != object_ref:
            saw_object_mismatch = True
            continue
        if item.evidence_state == "revoked":
            saw_revoked = True
            continue
        if item.evidence_state == "stale":
            saw_stale = True
            continue
        if required_scope not in item.scopes:
            saw_insufficient_scope = True
            continue
        return True, None

    if saw_revoked:
        return False, "feishu_authorization_evidence_revoked"
    if saw_stale:
        return False, "feishu_authorization_evidence_stale"
    if saw_insufficient_scope:
        return False, "feishu_object_authority_scope_insufficient"
    if saw_object_mismatch:
        return False, "feishu_object_ref_mismatch"
    if saw_authority_subject_mismatch:
        return False, "feishu_authority_subject_mismatch"
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
        _reject_sensitive_raw_key(normalized_key, item)
        if normalized_key in normalized:
            raise FeishuContractError(
                "contract mapping has duplicate keys after Unicode normalization",
            )
        normalized[normalized_key] = _canonical_value(item)
    return normalized


def _reject_sensitive_raw_key(key: str, value: Any) -> None:
    comparable = _normalized_key_for_policy(key)
    if comparable == "tokenclass":
        _require_token_class(value)
        return
    for marker in _SENSITIVE_KEY_MARKERS:
        if marker in comparable:
            if comparable.endswith("hash"):
                if not isinstance(value, str) or not _SHA256_HASH_RE.fullmatch(value):
                    raise FeishuContractError(
                        f"sensitive hash field must be a sha256 hash: {key}",
                        failure_class="invalid_hashed_sensitive_ref",
                    )
                return
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
        "tenant_partition_key": contract.tenant_partition_key,
        "app_partition_key": contract.app_partition_key,
        "conversation_scope_id": contract.conversation_scope_id,
        "shared_context_scope_id": contract.shared_context_scope_id,
        "route_partition_key": contract.route_partition_key,
        "route_session_key_snapshot": contract.route_session_key_snapshot,
        "scope_assignment_status": contract.scope_assignment_status,
        "actor_ref": contract.actor_ref,
        "authority_subject_ref": contract.authority_subject_ref,
        "session_id": contract.session_id,
        "thread_anchor_ref": contract.thread_anchor_ref,
        "root_anchor_ref": contract.root_anchor_ref,
        "identity_evidence_set": contract.identity_evidence_set,
        "policy_version": contract.policy_version,
        "evidence_state": contract.evidence_state,
        "contract_hash_domain": contract.contract_hash_domain,
        "contract_hash_version": contract.contract_hash_version,
    }


def _authorization_evidence_payload(evidence: AuthorizationEvidence) -> dict[str, Any]:
    return {
        "evidence_kind": evidence.evidence_kind,
        "authority_subject_ref": evidence.authority_subject_ref,
        "route_session_key_snapshot": evidence.route_session_key_snapshot,
        "object_ref": evidence.object_ref,
        "scopes": evidence.scopes,
        "token_class": evidence.token_class,
        "evidence_state": evidence.evidence_state,
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


def _require_known_value(value: Any, allowed: frozenset[str], field_name: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise FeishuContractError(
            f"{field_name} must be one of: {', '.join(sorted(allowed))}",
            failure_class=f"unsupported_{field_name}",
        )


def _require_token_class(value: Any) -> None:
    if not isinstance(value, str) or value not in _TOKEN_CLASSES:
        raise FeishuContractError(
            f"token_class must be one of: {', '.join(sorted(_TOKEN_CLASSES))}",
            failure_class="invalid_feishu_token_class",
        )


def _require_schema_version(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FeishuContractError(
            "schema_version must be a positive integer",
            failure_class="invalid_feishu_schema_version",
        )


def _is_app_token_only_evidence(evidence: AuthorizationEvidence) -> bool:
    return (
        evidence.evidence_kind == "app_token_only"
        or (
            evidence.token_class == "app_access_token"
            and evidence.evidence_kind not in _OBJECT_AUTHORITY_EVIDENCE_KINDS
        )
    )


def _is_object_authority_evidence(evidence: AuthorizationEvidence) -> bool:
    if evidence.evidence_kind not in _OBJECT_AUTHORITY_EVIDENCE_KINDS:
        return False
    return not _is_app_token_only_evidence(evidence)


def _is_p3_object_action(object_type: str, action: str) -> bool:
    action_key = action.lower()
    typed_action_key = f"{object_type.lower()}.{action_key}"
    return action_key in _P3_OBJECT_ACTIONS or typed_action_key in _P3_OBJECT_ACTIONS

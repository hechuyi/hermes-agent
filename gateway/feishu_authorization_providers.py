"""Internal Feishu authorization provider contracts.

This module defines sanitized Package C provider boundary types only. It does
not call Feishu SDKs, perform OAuth, expose model-visible tools, or persist
grants.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from gateway.feishu_contracts import (
    AuthorizationEvidence,
    FeishuContractError,
    HashedRef,
    feishu_contract_hash,
)


_GRANT_MODES = frozenset({"one_time", "short_session"})
_NORMALIZED_KEY_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CLASSIFIER_METADATA_STRING_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_RAW_FEISHU_ID_VALUE_RE = re.compile(
    r"^(?:(?:ou|on|oc|om|u|msg|doccn|shtcn|fldcn|boxcn)[A-Za-z0-9_-]*"
    r"|(?:app_token|file)_[A-Za-z0-9_-]+)$"
)
_PROVIDER_RAW_MARKERS = frozenset(
    {
        "aclresponsebody",
        "documentcontent",
        "feishuobjectid",
        "messagecontent",
        "objectid",
        "objectref",
        "rawacl",
        "rawdocument",
        "rawmessage",
    }
)
_PROVIDER_RAW_VALUE_MARKERS = frozenset(
    {
        "accesstokensecret",
        "authorizationbearer",
        "clientsecret",
        "privatekey",
        "refreshtokensecret",
        "secret",
        "tenantaccesstokensecret",
        "useraccesstokensecret",
    }
)
_ACL_BODY_KEYS = frozenset({"acl", "code", "data", "msg", "permission", "permissions"})
_AUTHORIZATION_EVIDENCE_SOURCE_CLASSES = frozenset(
    {
        "admin_policy_grant",
        "app_owned_object",
        "app_token_only",
        "discovery_only",
        "explicit_user_confirmation",
        "none",
        "system_test_object",
        "user_delegated_credential",
        "verified_object_acl",
    }
)
_CREDENTIAL_FRESHNESS_CLASSES = frozenset({"fresh", "stale", "revoked", "unknown"})
_FRESHNESS_CLASSES = frozenset({"current", "stale", "revoked", "unknown"})
_REACHABILITY_STATES = frozenset({"reachable", "unreachable", "unknown"})
_STABLE_FAILURE_CLASS_PREFIXES = (
    "feishu_provider_",
    "feishu_authorization_",
    "feishu_object_authority_",
    "feishu_contract_",
    "feishu_route_",
    "feishu_p3_",
    "invalid_feishu_",
)
_STABLE_FAILURE_CLASS_VALUES = frozenset(
    {
        "invalid_hashed_sensitive_ref",
        "sensitive_raw_field",
    }
)
_CLASSIFIER_METADATA_KEYS = frozenset(
    {
        "category",
        "class",
        "classifier",
        "credentialfreshness",
        "denialfailureclass",
        "evidenceclass",
        "evidencesourceclass",
        "failureclass",
        "freshnessclass",
        "kind",
        "policyversion",
        "providerclass",
        "providerversion",
        "reachabilitystate",
        "reasonclass",
        "reasoncode",
        "reasonkind",
        "resultclass",
        "revocationreason",
        "sourceclass",
        "sourcekind",
        "sourcetype",
        "state",
        "status",
        "type",
        "version",
    }
)


class AuthorizationProviderError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        failure_class: str = "invalid_feishu_authorization_provider_contract",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


@dataclass(frozen=True)
class AuthorizationProviderRequest:
    contract_hash: str
    route_session_key_snapshot: str
    authority_subject_ref: HashedRef
    object_ref: HashedRef
    object_type: str
    action: str
    requested_scopes: tuple[str, ...]
    policy_version: str
    grant_mode: str
    expires_at: str | None = None
    schema_version: int = 1
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256_hash(
            self.contract_hash,
            "contract_hash",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        _require_nonempty_string(
            self.route_session_key_snapshot,
            "route_session_key_snapshot",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        _require_hashed_ref(
            self.authority_subject_ref,
            "authority_subject_ref",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        _require_hashed_ref(
            self.object_ref,
            "object_ref",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        _require_nonempty_string(
            self.object_type,
            "object_type",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        _require_nonempty_string(
            self.action,
            "action",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        object.__setattr__(
            self,
            "requested_scopes",
            _string_tuple(
                self.requested_scopes,
                "requested_scopes",
                failure_class="invalid_feishu_authorization_provider_request",
            ),
        )
        _require_nonempty_string(
            self.policy_version,
            "policy_version",
            failure_class="invalid_feishu_authorization_provider_request",
        )
        if self.grant_mode not in _GRANT_MODES:
            raise AuthorizationProviderError(
                "grant_mode must be one_time or short_session",
                failure_class="invalid_feishu_authorization_provider_request",
            )
        if self.expires_at is not None:
            _require_nonempty_string(
                self.expires_at,
                "expires_at",
                failure_class="invalid_feishu_authorization_provider_request",
            )
        if self.grant_mode == "one_time" and self.expires_at is not None:
            raise AuthorizationProviderError(
                "one_time grant requests must not carry a session expiry",
                failure_class="invalid_feishu_authorization_provider_request",
            )
        if self.grant_mode == "short_session" and self.expires_at is None:
            raise AuthorizationProviderError(
                "short_session grant requests require expires_at",
                failure_class="invalid_feishu_authorization_provider_request",
            )
        _require_schema_version(
            self.schema_version,
            failure_class="invalid_feishu_authorization_provider_request",
        )
        object.__setattr__(
            self,
            "request_hash",
            feishu_contract_hash(
                _authorization_provider_request_payload(self),
                domain="feishu.authorization_provider_request",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class AuthorizationProviderDecision:
    provider_id: str
    provider_version: str
    policy_version: str
    evidence_source_class: str
    reachability_state: str
    issued_at: str | None
    expires_at: str | None
    freshness_class: str | None
    credential_freshness: str
    acl_complete: bool
    unsupported_scope: str | None = None
    revocation_reason: str | None = None
    denial_failure_class: str | None = None
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _require_classifier_or_hash(self.provider_id, "provider_id"),
        )
        object.__setattr__(
            self,
            "provider_version",
            _require_classifier_or_hash(self.provider_version, "provider_version"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _require_classifier_or_hash(self.policy_version, "policy_version"),
        )
        object.__setattr__(
            self,
            "evidence_source_class",
            _require_known_classifier(
                self.evidence_source_class,
                _AUTHORIZATION_EVIDENCE_SOURCE_CLASSES,
                "evidence_source_class",
            ),
        )
        object.__setattr__(
            self,
            "reachability_state",
            _require_known_classifier(
                self.reachability_state,
                _REACHABILITY_STATES,
                "reachability_state",
            ),
        )
        object.__setattr__(
            self,
            "credential_freshness",
            _require_known_classifier(
                self.credential_freshness,
                _CREDENTIAL_FRESHNESS_CLASSES,
                "credential_freshness",
            ),
        )
        if self.issued_at is not None:
            object.__setattr__(
                self,
                "issued_at",
                _require_utc_timestamp(self.issued_at, "issued_at"),
            )
        if self.expires_at is not None:
            object.__setattr__(
                self,
                "expires_at",
                _require_utc_timestamp(self.expires_at, "expires_at"),
            )
        if self.freshness_class is not None:
            object.__setattr__(
                self,
                "freshness_class",
                _require_known_classifier(
                    self.freshness_class,
                    _FRESHNESS_CLASSES,
                    "freshness_class",
                ),
            )
        if self.issued_at is None and self.expires_at is None and self.freshness_class is None:
            raise AuthorizationProviderError(
                "provider decision requires issued/expires timestamps or freshness class",
                failure_class="invalid_feishu_authorization_provider_decision",
            )
        if self.freshness_class is None and (
            self.issued_at is None or self.expires_at is None
        ):
            raise AuthorizationProviderError(
                "provider decision timestamp freshness requires issued_at and expires_at",
                failure_class="invalid_feishu_authorization_provider_decision",
            )
        if not isinstance(self.acl_complete, bool):
            raise AuthorizationProviderError(
                "acl_complete must be a boolean",
                failure_class="invalid_feishu_authorization_provider_decision",
            )
        if self.unsupported_scope is not None:
            object.__setattr__(
                self,
                "unsupported_scope",
                _require_classifier_or_hash(self.unsupported_scope, "unsupported_scope"),
            )
        if self.revocation_reason is not None:
            object.__setattr__(
                self,
                "revocation_reason",
                _require_classifier_or_hash(self.revocation_reason, "revocation_reason"),
            )
        if self.denial_failure_class is not None:
            object.__setattr__(
                self,
                "denial_failure_class",
                _require_stable_failure_class(
                    self.denial_failure_class,
                    "denial_failure_class",
                    failure_class="invalid_feishu_authorization_provider_decision",
                ),
            )
        _require_schema_version(
            self.schema_version,
            failure_class="invalid_feishu_authorization_provider_decision",
        )
        object.__setattr__(
            self,
            "extra_metadata",
            _sanitized_extra_metadata(self.extra_metadata),
        )
        try:
            decision_hash = feishu_contract_hash(
                _authorization_provider_decision_payload(self),
                domain="feishu.authorization_provider_decision",
                version="v1",
                schema_version=self.schema_version,
            )
        except FeishuContractError as exc:
            raise AuthorizationProviderError(
                exc.reason,
                failure_class=exc.failure_class,
            ) from exc
        object.__setattr__(self, "decision_hash", decision_hash)


@dataclass(frozen=True)
class AuthorizationProviderResult:
    evidence: AuthorizationEvidence | None
    decision: AuthorizationProviderDecision
    failure_class: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorizationProviderDecision):
            raise AuthorizationProviderError(
                "decision must be AuthorizationProviderDecision",
                failure_class="invalid_feishu_authorization_provider_result",
            )
        if self.evidence is not None and not isinstance(self.evidence, AuthorizationEvidence):
            raise AuthorizationProviderError(
                "evidence must be AuthorizationEvidence",
                failure_class="invalid_feishu_authorization_provider_result",
            )
        if self.failure_class is not None:
            object.__setattr__(
                self,
                "failure_class",
                _require_stable_failure_class(
                    self.failure_class,
                    "failure_class",
                    failure_class="invalid_feishu_authorization_provider_result",
                ),
            )
        if self.evidence is None:
            if self.failure_class is None or self.decision.denial_failure_class is None:
                raise AuthorizationProviderError(
                    "denial result requires a stable failure class on result and decision",
                    failure_class="invalid_feishu_authorization_provider_result",
                )
            if self.failure_class != self.decision.denial_failure_class:
                raise AuthorizationProviderError(
                    "result failure class must match decision denial failure class",
                    failure_class="invalid_feishu_authorization_provider_result",
                )
            return
        if self.failure_class is not None or self.decision.denial_failure_class is not None:
            raise AuthorizationProviderError(
                "evidence result must not carry denial failure class",
                failure_class="invalid_feishu_authorization_provider_result",
            )
        _require_current_positive_decision(self.evidence, self.decision)

    @property
    def is_denial(self) -> bool:
        return self.evidence is None


@runtime_checkable
class AuthorizationProviderProtocol(Protocol):
    provider_id: str
    provider_version: str

    def authorize(
        self,
        request: AuthorizationProviderRequest,
    ) -> AuthorizationProviderResult:
        ...


def _authorization_provider_request_payload(
    request: AuthorizationProviderRequest,
) -> dict[str, Any]:
    return {
        "contract_hash": request.contract_hash,
        "route_session_key_snapshot": request.route_session_key_snapshot,
        "authority_subject_ref": request.authority_subject_ref,
        "object_ref": request.object_ref,
        "object_type": request.object_type,
        "action": request.action,
        "requested_scopes": request.requested_scopes,
        "policy_version": request.policy_version,
        "grant_mode": request.grant_mode,
        "expires_at": request.expires_at,
    }


def _authorization_provider_decision_payload(
    decision: AuthorizationProviderDecision,
) -> dict[str, Any]:
    return {
        "provider_id": decision.provider_id,
        "provider_version": decision.provider_version,
        "policy_version": decision.policy_version,
        "evidence_source_class": decision.evidence_source_class,
        "reachability_state": decision.reachability_state,
        "issued_at": decision.issued_at,
        "expires_at": decision.expires_at,
        "freshness_class": decision.freshness_class,
        "credential_freshness": decision.credential_freshness,
        "acl_complete": decision.acl_complete,
        "unsupported_scope": decision.unsupported_scope,
        "revocation_reason": decision.revocation_reason,
        "denial_failure_class": decision.denial_failure_class,
        "extra_metadata": decision.extra_metadata,
    }


def _sanitized_extra_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationProviderError(
            "extra_metadata must be a mapping",
            failure_class="invalid_feishu_authorization_provider_decision",
        )
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise AuthorizationProviderError(
                "extra_metadata keys must be non-empty strings",
                failure_class="invalid_feishu_authorization_provider_decision",
            )
        normalized_key = unicodedata.normalize("NFC", key)
        comparable = _normalized_key_for_policy(normalized_key)
        if any(marker in comparable for marker in _PROVIDER_RAW_MARKERS):
            if comparable.endswith("hash"):
                if not isinstance(item, str) or not _SHA256_HASH_RE.fullmatch(item):
                    raise AuthorizationProviderError(
                        f"sensitive hash field must be a sha256 hash: {key}",
                        failure_class="invalid_hashed_sensitive_ref",
                    )
                sanitized[normalized_key] = unicodedata.normalize("NFC", item)
                continue
            raise AuthorizationProviderError(
                f"raw provider material is not allowed in decision metadata: {key}",
                failure_class="sensitive_raw_field",
            )
        sanitized[normalized_key] = _sanitized_metadata_value(item, comparable)
    return sanitized


def _sanitized_metadata_value(value: Any, comparable_key: str) -> Any:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        _reject_sensitive_raw_metadata_string(normalized, comparable_key)
        return normalized
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        if _looks_like_raw_acl_body(value):
            raise AuthorizationProviderError(
                "raw ACL bodies are not allowed in decision metadata",
                failure_class="sensitive_raw_field",
            )
        return _sanitized_extra_metadata(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_sanitized_metadata_value(item, comparable_key) for item in value)
    raise AuthorizationProviderError(
        f"unsupported decision metadata type: {type(value).__name__}",
        failure_class="invalid_feishu_authorization_provider_decision",
    )


def _normalized_key_for_policy(key: str) -> str:
    normalized = unicodedata.normalize("NFC", key).lower()
    return _NORMALIZED_KEY_CHARS_RE.sub("", normalized)


def _reject_sensitive_raw_metadata_string(value: str, comparable_key: str) -> None:
    if _looks_like_raw_local_path(value):
        raise AuthorizationProviderError(
            "raw local paths are not allowed in decision metadata",
            failure_class="sensitive_raw_field",
        )
    comparable = _normalized_key_for_policy(value)
    if _RAW_FEISHU_ID_VALUE_RE.fullmatch(value):
        raise AuthorizationProviderError(
            "raw Feishu object identifiers are not allowed in decision metadata",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_VALUE_MARKERS):
        raise AuthorizationProviderError(
            "raw provider secrets are not allowed in decision metadata",
            failure_class="sensitive_raw_field",
        )
    if _SHA256_HASH_RE.fullmatch(value):
        return
    if comparable_key in {"denialfailureclass", "failureclass"}:
        _require_stable_failure_class(
            value,
            comparable_key,
            failure_class="invalid_feishu_authorization_provider_decision",
        )
        return
    if (
        comparable_key in _CLASSIFIER_METADATA_KEYS
        and _CLASSIFIER_METADATA_STRING_RE.fullmatch(value)
    ):
        return
    raise AuthorizationProviderError(
        "decision metadata strings must be pre-hashed or stable classifier values",
        failure_class="sensitive_raw_field",
    )


def _require_classifier_or_hash(value: Any, field_name: str) -> str:
    _require_nonempty_string(
        value,
        field_name,
        failure_class="invalid_feishu_authorization_provider_decision",
    )
    normalized = unicodedata.normalize("NFC", value)
    if _looks_like_raw_local_path(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must not contain a raw local path",
            failure_class="sensitive_raw_field",
        )
    comparable = _normalized_key_for_policy(normalized)
    if _RAW_FEISHU_ID_VALUE_RE.fullmatch(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must not contain a raw Feishu identifier",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_VALUE_MARKERS):
        raise AuthorizationProviderError(
            f"{field_name} must not contain raw provider secrets",
            failure_class="sensitive_raw_field",
        )
    if _SHA256_HASH_RE.fullmatch(normalized):
        return normalized
    if _CLASSIFIER_METADATA_STRING_RE.fullmatch(normalized):
        return normalized
    raise AuthorizationProviderError(
        f"{field_name} must be a stable classifier or sha256 hash",
        failure_class="invalid_feishu_authorization_provider_decision",
    )


def _require_known_classifier(
    value: Any,
    allowed: frozenset[str],
    field_name: str,
) -> str:
    _require_nonempty_string(
        value,
        field_name,
        failure_class="invalid_feishu_authorization_provider_decision",
    )
    normalized = unicodedata.normalize("NFC", value)
    if _looks_like_raw_local_path(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must not contain a raw local path",
            failure_class="sensitive_raw_field",
        )
    if not _CLASSIFIER_METADATA_STRING_RE.fullmatch(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must be a supported classifier",
            failure_class="invalid_feishu_authorization_provider_decision",
        )
    if normalized not in allowed:
        raise AuthorizationProviderError(
            f"{field_name} must be a supported classifier",
            failure_class="invalid_feishu_authorization_provider_decision",
        )
    return normalized


def _require_stable_failure_class(
    value: Any,
    field_name: str,
    *,
    failure_class: str,
) -> str:
    _require_nonempty_string(value, field_name, failure_class=failure_class)
    normalized = unicodedata.normalize("NFC", value)
    if not _CLASSIFIER_METADATA_STRING_RE.fullmatch(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must be a stable failure classifier",
            failure_class=failure_class,
        )
    if (
        normalized in _STABLE_FAILURE_CLASS_VALUES
        or normalized.startswith(_STABLE_FAILURE_CLASS_PREFIXES)
    ):
        return normalized
    raise AuthorizationProviderError(
        f"{field_name} must use a stable Feishu failure namespace",
        failure_class=failure_class,
    )


def _require_utc_timestamp(value: Any, field_name: str) -> str:
    _require_nonempty_string(
        value,
        field_name,
        failure_class="invalid_feishu_authorization_provider_decision",
    )
    normalized = unicodedata.normalize("NFC", value)
    if not _UTC_TIMESTAMP_RE.fullmatch(normalized):
        raise AuthorizationProviderError(
            f"{field_name} must be an ISO-8601 UTC timestamp",
            failure_class="invalid_feishu_authorization_provider_decision",
        )
    try:
        datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise AuthorizationProviderError(
            f"{field_name} must be a valid UTC timestamp",
            failure_class="invalid_feishu_authorization_provider_decision",
        ) from exc
    return normalized


def _require_current_positive_decision(
    evidence: AuthorizationEvidence,
    decision: AuthorizationProviderDecision,
) -> None:
    if decision.reachability_state != "reachable":
        raise AuthorizationProviderError(
            "positive authorization requires reachable provider decision",
            failure_class="invalid_feishu_authorization_provider_result",
        )
    if decision.credential_freshness != "fresh" or decision.freshness_class != "current":
        raise AuthorizationProviderError(
            "positive authorization requires current fresh credentials",
            failure_class="invalid_feishu_authorization_provider_result",
        )
    if not decision.acl_complete:
        raise AuthorizationProviderError(
            "positive authorization requires complete ACL evidence",
            failure_class="invalid_feishu_authorization_provider_result",
        )
    if decision.evidence_source_class != evidence.evidence_kind:
        raise AuthorizationProviderError(
            "decision evidence source class must match authorization evidence kind",
            failure_class="invalid_feishu_authorization_provider_result",
        )
    if evidence.evidence_state != "current":
        raise AuthorizationProviderError(
            "positive authorization requires current evidence",
            failure_class="invalid_feishu_authorization_provider_result",
        )
    if decision.unsupported_scope is not None or decision.revocation_reason is not None:
        raise AuthorizationProviderError(
            "positive authorization must not carry denial reason metadata",
            failure_class="invalid_feishu_authorization_provider_result",
        )


def _looks_like_raw_local_path(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    return (
        text.startswith("/")
        or text.startswith("\\\\")
        or _WINDOWS_ABSOLUTE_PATH_RE.match(text) is not None
        or normalized.startswith("~/")
        or normalized.startswith("../")
        or normalized.startswith("./")
        or normalized.startswith("workspace/")
        or "/../" in normalized
    )


def _looks_like_raw_acl_body(value: Mapping[Any, Any]) -> bool:
    keys = {
        _normalized_key_for_policy(key)
        for key in value
        if isinstance(key, str)
    }
    if "data" in keys and keys.intersection({"code", "msg"}):
        return True
    if len(keys.intersection(_ACL_BODY_KEYS)) >= 2:
        return True
    return False


def _string_tuple(
    value: Any,
    field_name: str,
    *,
    failure_class: str,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        raise AuthorizationProviderError(
            f"{field_name} must be a sequence",
            failure_class=failure_class,
        )
    items = tuple(value)
    if not items:
        raise AuthorizationProviderError(
            f"{field_name} must not be empty",
            failure_class=failure_class,
        )
    for item in items:
        _require_nonempty_string(item, field_name, failure_class=failure_class)
    return items


def _require_hashed_ref(value: Any, field_name: str, *, failure_class: str) -> None:
    if not isinstance(value, HashedRef):
        raise AuthorizationProviderError(
            f"{field_name} must be a HashedRef",
            failure_class=failure_class,
        )


def _require_nonempty_string(value: Any, field_name: str, *, failure_class: str) -> None:
    if not isinstance(value, str) or not value:
        raise AuthorizationProviderError(
            f"{field_name} must be a non-empty string",
            failure_class=failure_class,
        )


def _require_schema_version(value: Any, *, failure_class: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AuthorizationProviderError(
            "schema_version must be a positive integer",
            failure_class=failure_class,
        )


def _require_sha256_hash(value: Any, field_name: str, *, failure_class: str) -> None:
    if not isinstance(value, str) or not _SHA256_HASH_RE.fullmatch(value):
        raise AuthorizationProviderError(
            f"{field_name} must be a sha256 hash",
            failure_class=failure_class,
        )


__all__ = [
    "AuthorizationProviderDecision",
    "AuthorizationProviderError",
    "AuthorizationProviderProtocol",
    "AuthorizationProviderRequest",
    "AuthorizationProviderResult",
]

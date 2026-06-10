"""Pure broker policy for issuing Feishu object capability grants.

This module converts sanitized authorization-provider outcomes into internal
object capability grant wrappers.  It does not expose model-visible tools,
call Feishu SDKs or OpenAPI clients, or persist grants.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gateway.feishu_authorization_providers import (
    AuthorizationProviderDecision,
    AuthorizationProviderRequest,
)
from gateway.feishu_contracts import (
    AuthorizationEvidence,
    ConversationContract,
    HashedRef,
    ObjectCapabilityGrant,
    can_issue_object_grant,
    feishu_contract_hash,
)


_GRANT_SEMANTICS = frozenset({"one_time", "short_session"})
_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STABLE_PROVIDER_FAILURE_CLASSES = frozenset(
    {
        "feishu_provider_acl_incomplete",
        "feishu_provider_app_token_unavailable",
        "feishu_provider_authority_mismatch",
        "feishu_provider_credential_unknown",
        "feishu_provider_non_grantable_state",
        "feishu_provider_revoked_credential",
        "feishu_provider_sdk_unreachable",
        "feishu_provider_stale_credential",
        "feishu_provider_unavailable",
        "feishu_provider_unsupported",
        "feishu_provider_unsupported_scope",
        "feishu_provider_user_owned_object",
    }
)


@dataclass(frozen=True)
class BrokerPolicyReplayRecord:
    request_id_hash: str
    payload_hash: str
    provider_decision_hash: str
    route_snapshot_hash: str
    object_ref_hash: str
    object_type: str
    action: str
    grant_semantics: str
    grant_hash: str
    expires_at: str | None
    schema_version: int = 1
    replay_record_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256_hash(self.request_id_hash, "request_id_hash")
        _require_sha256_hash(self.payload_hash, "payload_hash")
        _require_sha256_hash(self.provider_decision_hash, "provider_decision_hash")
        _require_nonempty_string(self.route_snapshot_hash, "route_snapshot_hash")
        _require_sha256_hash(self.object_ref_hash, "object_ref_hash")
        _require_nonempty_string(self.object_type, "object_type")
        _require_nonempty_string(self.action, "action")
        _require_grant_semantics(self.grant_semantics)
        _require_sha256_hash(self.grant_hash, "grant_hash")
        if self.expires_at is not None:
            _require_utc_timestamp(self.expires_at, "expires_at")
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "replay_record_hash",
            feishu_contract_hash(
                _replay_record_payload(self),
                domain="feishu.broker_policy_replay_record",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class BrokerPolicyRequest:
    contract: ConversationContract
    provider_id: str
    object_type: str
    object_ref: HashedRef
    action: str
    requested_scopes: tuple[str, ...]
    grant_semantics: str
    expires_at: str | None
    request_id_hash: str
    payload_hash: str
    prior_replay_record: BrokerPolicyReplayRecord | None = None
    schema_version: int = 1
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ConversationContract):
            raise BrokerPolicyError(
                "contract must be a ConversationContract",
                failure_class="invalid_feishu_broker_policy_request",
            )
        object.__setattr__(self, "provider_id", _classifier_or_hash(self.provider_id))
        _require_nonempty_string(self.object_type, "object_type")
        if not isinstance(self.object_ref, HashedRef):
            raise BrokerPolicyError(
                "object_ref must be a HashedRef",
                failure_class="invalid_feishu_broker_policy_request",
            )
        _require_nonempty_string(self.action, "action")
        object.__setattr__(
            self,
            "requested_scopes",
            _string_tuple(self.requested_scopes, "requested_scopes"),
        )
        _require_grant_semantics(self.grant_semantics)
        if self.expires_at is not None:
            _require_utc_timestamp(self.expires_at, "expires_at")
        if self.grant_semantics == "one_time" and self.expires_at is not None:
            raise BrokerPolicyError(
                "one_time grants must not carry a session expiry",
                failure_class="invalid_feishu_broker_policy_request",
            )
        if self.grant_semantics == "short_session" and self.expires_at is None:
            raise BrokerPolicyError(
                "short_session grants require expires_at",
                failure_class="invalid_feishu_broker_policy_request",
            )
        _require_sha256_hash(self.request_id_hash, "request_id_hash")
        _require_sha256_hash(self.payload_hash, "payload_hash")
        if self.prior_replay_record is not None and not isinstance(
            self.prior_replay_record,
            BrokerPolicyReplayRecord,
        ):
            raise BrokerPolicyError(
                "prior_replay_record must be BrokerPolicyReplayRecord",
                failure_class="invalid_feishu_broker_policy_request",
            )
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "request_hash",
            feishu_contract_hash(
                _broker_policy_request_payload(self),
                domain="feishu.broker_policy_request",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class BrokerPolicyGrant:
    object_capability_grant: ObjectCapabilityGrant
    expires_at: str | None
    route_snapshot_hash: str
    policy_version: str
    grant_semantics: str
    request_id_hash: str
    payload_hash: str
    provider_decision_hash: str
    replay_record: BrokerPolicyReplayRecord
    schema_version: int = 1
    grant_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.object_capability_grant, ObjectCapabilityGrant):
            raise BrokerPolicyError(
                "object_capability_grant must be ObjectCapabilityGrant",
                failure_class="invalid_feishu_broker_policy_grant",
            )
        if self.expires_at is not None:
            _require_utc_timestamp(self.expires_at, "expires_at")
        _require_nonempty_string(self.route_snapshot_hash, "route_snapshot_hash")
        _require_nonempty_string(self.policy_version, "policy_version")
        _require_grant_semantics(self.grant_semantics)
        _require_sha256_hash(self.request_id_hash, "request_id_hash")
        _require_sha256_hash(self.payload_hash, "payload_hash")
        _require_sha256_hash(self.provider_decision_hash, "provider_decision_hash")
        if not isinstance(self.replay_record, BrokerPolicyReplayRecord):
            raise BrokerPolicyError(
                "replay_record must be BrokerPolicyReplayRecord",
                failure_class="invalid_feishu_broker_policy_grant",
            )
        _require_schema_version(self.schema_version)
        object.__setattr__(
            self,
            "grant_hash",
            feishu_contract_hash(
                _broker_policy_grant_payload(self),
                domain="feishu.broker_policy_grant",
                version="v1",
                schema_version=self.schema_version,
            ),
        )


@dataclass(frozen=True)
class BrokerPolicyDecision:
    grant: BrokerPolicyGrant | None = None
    failure_class: str | None = None
    denial_reason_class: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.grant is not None and self.failure_class is not None:
            raise BrokerPolicyError(
                "broker policy decision cannot contain grant and failure",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        if self.grant is None and self.failure_class is None:
            raise BrokerPolicyError(
                "broker policy decision requires grant or failure",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        if self.grant is not None and not isinstance(self.grant, BrokerPolicyGrant):
            raise BrokerPolicyError(
                "grant must be BrokerPolicyGrant",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        if self.failure_class is not None:
            _require_nonempty_string(self.failure_class, "failure_class")
        if self.denial_reason_class is not None:
            _require_nonempty_string(self.denial_reason_class, "denial_reason_class")
        if self.failure_class is None and self.denial_reason_class is not None:
            raise BrokerPolicyError(
                "grant decisions must not carry denial reason",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        _require_schema_version(self.schema_version)


class BrokerPolicyError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        failure_class: str = "invalid_feishu_broker_policy",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


def issue_object_capability_grant(
    request: BrokerPolicyRequest,
    registry: Mapping[str, Any] | None,
    *,
    now: datetime,
    policy_version: str,
) -> BrokerPolicyDecision:
    if not isinstance(request, BrokerPolicyRequest):
        return _deny("invalid_feishu_broker_policy_request")
    if not isinstance(now, datetime):
        return _normalized_deny("feishu_broker_policy_invalid_now")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    if not isinstance(policy_version, str) or not policy_version:
        return _normalized_deny("feishu_broker_policy_invalid_policy_version")
    if request.contract.policy_version != policy_version:
        return _normalized_deny("feishu_contract_policy_version_mismatch")
    if (
        request.grant_semantics == "short_session"
        and request.expires_at is not None
        and _parse_utc(request.expires_at) <= now
    ):
        return _normalized_deny("feishu_broker_policy_expired_grant_request")

    replay_failure = _replay_failure(request)
    if replay_failure is not None:
        return _normalized_deny(replay_failure)

    if request.contract.authority_subject_ref is None:
        return _deny("feishu_authority_subject_missing")
    provider = _provider_for(request.provider_id, registry)
    if provider is None:
        return _deny("feishu_authorization_provider_missing")
    if getattr(provider, "provider_id", None) != request.provider_id:
        return _normalized_deny("feishu_provider_identity_mismatch")

    provider_request = AuthorizationProviderRequest(
        contract_hash=request.contract.contract_hash,
        route_session_key_snapshot=request.contract.route_session_key_snapshot,
        authority_subject_ref=request.contract.authority_subject_ref,
        object_ref=request.object_ref,
        object_type=request.object_type,
        action=request.action,
        requested_scopes=request.requested_scopes,
        policy_version=policy_version,
        grant_mode=request.grant_semantics,
        expires_at=request.expires_at,
    )
    try:
        result = provider.authorize(provider_request)
    except Exception as exc:  # pragma: no cover - defensive boundary guard.
        return _normalized_deny(_exception_failure_class(exc))

    provider_failure = _provider_failure(
        result,
        now=now,
        policy_version=policy_version,
        provider_id=request.provider_id,
    )
    if provider_failure is not None:
        failure_class, denial_reason_class = provider_failure
        return _deny(failure_class, denial_reason_class=denial_reason_class)
    evidence = getattr(result, "evidence", None)
    if evidence is None:
        return _deny("feishu_authorization_evidence_missing")
    if not isinstance(evidence, AuthorizationEvidence):
        return _deny("feishu_authorization_evidence_missing")
    provider_decision = result.decision
    if provider_decision.evidence_source_class != evidence.evidence_kind:
        return _normalized_deny("feishu_provider_decision_evidence_mismatch")

    allowed, failure_class = can_issue_object_grant(
        request.contract,
        evidence,
        object_type=request.object_type,
        object_ref=request.object_ref,
        action=request.action,
    )
    if not allowed:
        return _deny(failure_class or "feishu_broker_policy_denied")

    grant = ObjectCapabilityGrant(
        contract_hash=request.contract.contract_hash,
        evidence_hashes=(evidence.evidence_hash,),
        object_type=request.object_type,
        object_ref=request.object_ref,
        action=request.action,
        authority_subject_ref=request.contract.authority_subject_ref,
    )
    replay_record = BrokerPolicyReplayRecord(
        request_id_hash=request.request_id_hash,
        payload_hash=request.payload_hash,
        provider_decision_hash=provider_decision.decision_hash,
        route_snapshot_hash=request.contract.route_session_key_snapshot,
        object_ref_hash=request.object_ref.value_hash,
        object_type=request.object_type,
        action=request.action,
        grant_semantics=request.grant_semantics,
        grant_hash=grant.grant_hash,
        expires_at=request.expires_at,
    )
    return BrokerPolicyDecision(
        grant=BrokerPolicyGrant(
            object_capability_grant=grant,
            expires_at=request.expires_at,
            route_snapshot_hash=request.contract.route_session_key_snapshot,
            policy_version=policy_version,
            grant_semantics=request.grant_semantics,
            request_id_hash=request.request_id_hash,
            payload_hash=request.payload_hash,
            provider_decision_hash=provider_decision.decision_hash,
            replay_record=replay_record,
        )
    )


def _provider_failure(
    result: Any,
    *,
    now: datetime,
    policy_version: str,
    provider_id: str,
) -> tuple[str, str] | None:
    decision = getattr(result, "decision", None)
    if not isinstance(decision, AuthorizationProviderDecision):
        return (
            "feishu_broker_policy_denied",
            "feishu_authorization_provider_decision_missing",
        )
    if decision.provider_id != provider_id:
        return "feishu_broker_policy_denied", "feishu_provider_identity_mismatch"
    failure_class = getattr(result, "failure_class", None)
    if failure_class is not None:
        if failure_class not in _STABLE_PROVIDER_FAILURE_CLASSES:
            return (
                "feishu_broker_policy_denied",
                "feishu_authorization_provider_failure_class_invalid",
            )
        return failure_class, failure_class
    if decision.denial_failure_class is not None:
        return decision.denial_failure_class, decision.denial_failure_class
    if decision.revocation_reason is not None:
        return "feishu_broker_policy_denied", "feishu_provider_decision_inconsistent"
    if decision.policy_version != policy_version:
        return "feishu_broker_policy_denied", "feishu_provider_policy_version_mismatch"
    if decision.reachability_state != "reachable":
        return "feishu_provider_sdk_unreachable", "feishu_provider_sdk_unreachable"
    if decision.credential_freshness == "stale" or decision.freshness_class == "stale":
        return "feishu_provider_stale_credential", "feishu_provider_stale_credential"
    if decision.credential_freshness == "revoked" or decision.freshness_class == "revoked":
        return "feishu_provider_revoked_credential", "feishu_provider_revoked_credential"
    if decision.credential_freshness != "fresh" or decision.freshness_class != "current":
        return "feishu_provider_credential_unknown", "feishu_provider_credential_unknown"
    if not decision.acl_complete:
        return "feishu_provider_acl_incomplete", "feishu_provider_acl_incomplete"
    if decision.unsupported_scope is not None:
        return "feishu_provider_unsupported_scope", "feishu_provider_unsupported_scope"
    if decision.expires_at is not None and _parse_utc(decision.expires_at) <= now:
        return "feishu_broker_policy_denied", "feishu_provider_decision_expired"
    return None


def _provider_for(provider_id: str, registry: Mapping[str, Any] | None) -> Any | None:
    if registry is None or not isinstance(registry, Mapping):
        return None
    return registry.get(provider_id)


def _replay_failure(request: BrokerPolicyRequest) -> str | None:
    prior = request.prior_replay_record
    if prior is None or prior.request_id_hash != request.request_id_hash:
        return None
    bound_fields = (
        prior.payload_hash == request.payload_hash,
        prior.route_snapshot_hash == request.contract.route_session_key_snapshot,
        prior.object_ref_hash == request.object_ref.value_hash,
        prior.object_type == request.object_type,
        prior.action == request.action,
    )
    if not all(bound_fields):
        return "feishu_broker_policy_replay_binding_mismatch"
    if prior.grant_semantics == "one_time" or request.grant_semantics == "one_time":
        return "feishu_broker_policy_one_time_reuse"
    return None


def _deny(
    failure_class: str,
    *,
    denial_reason_class: str | None = None,
) -> BrokerPolicyDecision:
    return BrokerPolicyDecision(
        grant=None,
        failure_class=failure_class,
        denial_reason_class=denial_reason_class or failure_class,
    )


def _normalized_deny(denial_reason_class: str) -> BrokerPolicyDecision:
    return BrokerPolicyDecision(
        grant=None,
        failure_class="feishu_broker_policy_denied",
        denial_reason_class=denial_reason_class,
    )


def _parse_utc(value: str) -> datetime:
    _require_utc_timestamp(value, "timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _exception_failure_class(_exc: Exception) -> str:
    return "feishu_provider_authorization_exception"


def _broker_policy_request_payload(request: BrokerPolicyRequest) -> dict[str, Any]:
    return {
        "contract_hash": request.contract.contract_hash,
        "provider_id": request.provider_id,
        "object_type": request.object_type,
        "object_ref": request.object_ref,
        "action": request.action,
        "requested_scopes": request.requested_scopes,
        "grant_semantics": request.grant_semantics,
        "expires_at": request.expires_at,
        "request_id_hash": request.request_id_hash,
        "payload_hash": request.payload_hash,
        "prior_replay_record_hash": (
            None
            if request.prior_replay_record is None
            else request.prior_replay_record.replay_record_hash
        ),
    }


def _broker_policy_grant_payload(grant: BrokerPolicyGrant) -> dict[str, Any]:
    return {
        "object_capability_grant_hash": grant.object_capability_grant.grant_hash,
        "expires_at": grant.expires_at,
        "route_snapshot_hash": grant.route_snapshot_hash,
        "policy_version": grant.policy_version,
        "grant_semantics": grant.grant_semantics,
        "request_id_hash": grant.request_id_hash,
        "payload_hash": grant.payload_hash,
        "provider_decision_hash": grant.provider_decision_hash,
        "replay_record_hash": grant.replay_record.replay_record_hash,
    }


def _replay_record_payload(record: BrokerPolicyReplayRecord) -> dict[str, Any]:
    return {
        "request_id_hash": record.request_id_hash,
        "payload_hash": record.payload_hash,
        "provider_decision_hash": record.provider_decision_hash,
        "route_snapshot_hash": record.route_snapshot_hash,
        "object_ref_hash": record.object_ref_hash,
        "object_type": record.object_type,
        "action": record.action,
        "grant_semantics": record.grant_semantics,
        "grant_hash": record.grant_hash,
        "expires_at": record.expires_at,
    }


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        raise BrokerPolicyError(
            f"{field_name} must be a sequence",
            failure_class="invalid_feishu_broker_policy_request",
        )
    items = tuple(value)
    if not items:
        raise BrokerPolicyError(
            f"{field_name} must not be empty",
            failure_class="invalid_feishu_broker_policy_request",
        )
    for item in items:
        _require_nonempty_string(item, field_name)
    return items


def _classifier_or_hash(value: Any) -> str:
    _require_nonempty_string(value, "provider_id")
    normalized = unicodedata.normalize("NFC", value)
    if _SHA256_HASH_RE.fullmatch(normalized):
        return normalized
    if re.fullmatch(r"^[a-z0-9][a-z0-9._:-]{0,63}$", normalized):
        return normalized
    raise BrokerPolicyError(
        "provider_id must be a stable classifier or sha256 hash",
        failure_class="invalid_feishu_broker_policy_request",
    )


def _require_grant_semantics(value: Any) -> None:
    if value not in _GRANT_SEMANTICS:
        raise BrokerPolicyError(
            "grant_semantics must be one_time or short_session",
            failure_class="invalid_feishu_broker_policy_request",
        )


def _require_utc_timestamp(value: Any, field_name: str) -> None:
    _require_nonempty_string(value, field_name)
    if _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise BrokerPolicyError(
            f"{field_name} must be an ISO-8601 UTC timestamp",
            failure_class="invalid_feishu_broker_policy_request",
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise BrokerPolicyError(
            f"{field_name} must be a valid UTC timestamp",
            failure_class="invalid_feishu_broker_policy_request",
        ) from exc


def _require_nonempty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise BrokerPolicyError(
            f"{field_name} must be a non-empty string",
            failure_class="invalid_feishu_broker_policy_request",
        )


def _require_sha256_hash(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_HASH_RE.fullmatch(value):
        raise BrokerPolicyError(
            f"{field_name} must be a sha256 hash",
            failure_class="invalid_feishu_broker_policy_request",
        )


def _require_schema_version(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BrokerPolicyError(
            "schema_version must be a positive integer",
            failure_class="invalid_feishu_broker_policy_request",
        )


__all__ = [
    "BrokerPolicyDecision",
    "BrokerPolicyError",
    "BrokerPolicyGrant",
    "BrokerPolicyReplayRecord",
    "BrokerPolicyRequest",
    "issue_object_capability_grant",
]

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
    AuthorizationProviderResult,
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
_NORMALIZED_KEY_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ROUTE_SNAPSHOT_HASH_RE = re.compile(
    r"^(?:route_session_snapshot_hash|route_partition_hash):sha256:[a-f0-9]{64}$"
)
_CLASSIFIER_METADATA_STRING_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_RAW_FEISHU_ID_VALUE_RE = re.compile(
    r"^(?:(?:ou|on|oc|om|u|msg|doccn|shtcn|fldcn|boxcn)[A-Za-z0-9_-]*"
    r"|(?:app_token|file)_[A-Za-z0-9_-]+)$"
)
_BROKER_SAFE_CLASSIFIERS = frozenset({"app_token_only"})
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
_PROVIDER_EVIDENCE_SOURCE_CLASSES = frozenset(
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
_PROVIDER_REACHABILITY_STATES = frozenset({"reachable", "unreachable", "unknown"})
_PROVIDER_CREDENTIAL_FRESHNESS_CLASSES = frozenset(
    {"fresh", "stale", "revoked", "unknown"}
)
_PROVIDER_FRESHNESS_CLASSES = frozenset({"current", "stale", "revoked", "unknown"})
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
_AUTHORIZATION_EVIDENCE_STATES = frozenset({"current", "stale", "revoked"})
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
        object.__setattr__(
            self,
            "route_snapshot_hash",
            _route_snapshot_hash(self.route_snapshot_hash, "route_snapshot_hash"),
        )
        _require_sha256_hash(self.object_ref_hash, "object_ref_hash")
        object.__setattr__(
            self,
            "object_type",
            _classifier_or_hash(self.object_type, "object_type"),
        )
        object.__setattr__(self, "action", _classifier_or_hash(self.action, "action"))
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
        object.__setattr__(
            self,
            "provider_id",
            _classifier_or_hash(self.provider_id, "provider_id"),
        )
        object.__setattr__(
            self,
            "object_type",
            _classifier_or_hash(self.object_type, "object_type"),
        )
        if not isinstance(self.object_ref, HashedRef):
            raise BrokerPolicyError(
                "object_ref must be a HashedRef",
                failure_class="invalid_feishu_broker_policy_request",
            )
        object.__setattr__(self, "action", _classifier_or_hash(self.action, "action"))
        object.__setattr__(
            self,
            "requested_scopes",
            _classifier_or_hash_tuple(self.requested_scopes, "requested_scopes"),
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
        object.__setattr__(
            self,
            "route_snapshot_hash",
            _route_snapshot_hash(self.route_snapshot_hash, "route_snapshot_hash"),
        )
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
    audit_event_templates: tuple[dict[str, Any], ...] = ()
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
        if not isinstance(self.audit_event_templates, tuple):
            raise BrokerPolicyError(
                "audit_event_templates must be a tuple",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        for template in self.audit_event_templates:
            if not isinstance(template, dict) or "type" not in template:
                raise BrokerPolicyError(
                    "audit_event_templates must contain event templates",
                    failure_class="invalid_feishu_broker_policy_decision",
                )
        _require_schema_version(self.schema_version)

    def audit_events(self, *, correlation_id: str, timestamp: int | float) -> tuple[dict[str, Any], ...]:
        _require_nonempty_string(correlation_id, "correlation_id")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise BrokerPolicyError(
                "timestamp must be numeric",
                failure_class="invalid_feishu_broker_policy_decision",
            )
        events = []
        for template in self.audit_event_templates:
            event = dict(template)
            event["timestamp"] = timestamp
            event["correlation_id"] = correlation_id
            event["event_hash"] = feishu_contract_hash(
                event,
                domain="feishu.broker_policy_audit_event",
                version="v1",
                schema_version=self.schema_version,
            )
            events.append(event)
        return tuple(events)


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
    def deny(
        failure_class: str,
        *,
        denial_reason_class: str | None = None,
    ) -> BrokerPolicyDecision:
        reason = denial_reason_class or failure_class
        return _deny(
            failure_class,
            denial_reason_class=reason,
            audit_event_templates=_denial_audit_event_templates(
                request,
                failure_class=failure_class,
                denial_reason_class=reason,
                policy_version=policy_version if isinstance(policy_version, str) and policy_version else "unknown",
            ),
        )

    def normalized_deny(denial_reason_class: str) -> BrokerPolicyDecision:
        return deny(
            "feishu_broker_policy_denied",
            denial_reason_class=denial_reason_class,
        )

    if not isinstance(now, datetime):
        return normalized_deny("feishu_broker_policy_invalid_now")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    if not isinstance(policy_version, str) or not policy_version:
        return normalized_deny("feishu_broker_policy_invalid_policy_version")
    if request.contract.policy_version != policy_version:
        return normalized_deny("feishu_contract_policy_version_mismatch")
    try:
        route_snapshot_hash = _route_snapshot_hash(
            request.contract.route_session_key_snapshot,
            "route_session_key_snapshot",
        )
    except BrokerPolicyError as exc:
        return normalized_deny(exc.failure_class)
    if (
        request.grant_semantics == "short_session"
        and request.expires_at is not None
        and _parse_utc(request.expires_at) <= now
    ):
        return normalized_deny("feishu_broker_policy_expired_grant_request")

    replay_failure = _replay_failure(request)
    if replay_failure is not None:
        return normalized_deny(replay_failure)

    if request.contract.authority_subject_ref is None:
        return deny("feishu_authority_subject_missing")
    provider = _provider_for(request.provider_id, registry)
    if provider is None:
        return deny("feishu_authorization_provider_missing")
    if getattr(provider, "provider_id", None) != request.provider_id:
        return normalized_deny("feishu_provider_identity_mismatch")

    provider_request = AuthorizationProviderRequest(
        contract_hash=request.contract.contract_hash,
        route_session_key_snapshot=route_snapshot_hash,
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
        return normalized_deny(_exception_failure_class(exc))

    provider_failure = _provider_failure(
        result,
        now=now,
        policy_version=policy_version,
        provider_id=request.provider_id,
    )
    if provider_failure is not None:
        failure_class, denial_reason_class = provider_failure
        return deny(failure_class, denial_reason_class=denial_reason_class)
    evidence = getattr(result, "evidence", None)
    if evidence is None:
        return deny("feishu_authorization_evidence_missing")
    if not isinstance(evidence, AuthorizationEvidence):
        return deny("feishu_authorization_evidence_missing")
    evidence_failure = _authorization_evidence_failure(evidence)
    if evidence_failure is not None:
        return normalized_deny(evidence_failure)
    provider_decision = result.decision
    if provider_decision.evidence_source_class != evidence.evidence_kind:
        return normalized_deny("feishu_provider_decision_evidence_mismatch")

    allowed, failure_class = can_issue_object_grant(
        request.contract,
        evidence,
        object_type=request.object_type,
        object_ref=request.object_ref,
        action=request.action,
    )
    if not allowed:
        return deny(failure_class or "feishu_broker_policy_denied")

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
        route_snapshot_hash=route_snapshot_hash,
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
            route_snapshot_hash=route_snapshot_hash,
            policy_version=policy_version,
            grant_semantics=request.grant_semantics,
            request_id_hash=request.request_id_hash,
            payload_hash=request.payload_hash,
            provider_decision_hash=provider_decision.decision_hash,
            replay_record=replay_record,
        ),
        audit_event_templates=_grant_audit_event_templates(
            request,
            provider_decision=provider_decision,
            evidence=evidence,
            grant=grant,
            route_snapshot_hash=route_snapshot_hash,
            policy_version=policy_version,
        ),
    )


def _grant_audit_event_templates(
    request: BrokerPolicyRequest,
    *,
    provider_decision: AuthorizationProviderDecision,
    evidence: AuthorizationEvidence,
    grant: ObjectCapabilityGrant,
    route_snapshot_hash: str,
    policy_version: str,
) -> tuple[dict[str, Any], ...]:
    return (
        _provider_decision_audit_event_template(
            request,
            provider_decision=provider_decision,
            route_snapshot_hash=route_snapshot_hash,
            policy_version=policy_version,
        ),
        {
            "type": "feishu_authorization_evidence_observed",
            "authorization_evidence_hash": evidence.evidence_hash,
            "contract_hash": request.contract.contract_hash,
            "route_snapshot_hash": route_snapshot_hash,
            "object_ref_hash": _object_ref_hash(evidence.object_ref, request.object_ref),
            "authority_subject_hash": _authority_subject_hash(
                evidence.authority_subject_ref,
                request,
            ),
            "evidence_source_class": evidence.evidence_kind,
            "evidence_state_class": evidence.evidence_state,
            "policy_version": policy_version,
        },
        {
            "type": "feishu_capability_granted",
            "grant_hash": grant.grant_hash,
            "evidence_hashes": list(grant.evidence_hashes),
            "contract_hash": grant.contract_hash,
            "object_type": grant.object_type,
            "object_ref_hash": grant.object_ref.value_hash,
            "action": grant.action,
            "authority_subject_hash": grant.authority_subject_ref.value_hash,
            "expiry": request.expires_at or "no_expiry",
            "grant_session_class": request.grant_semantics,
            "policy_version": policy_version,
        },
        {
            "type": "feishu_auth_decision",
            "decision_hash": provider_decision.decision_hash,
            "request_hash": request.request_hash,
            "contract_hash": request.contract.contract_hash,
            "decision": "authorized",
            "policy_version": policy_version,
        },
    )


def _provider_decision_audit_event_template(
    request: BrokerPolicyRequest,
    *,
    provider_decision: AuthorizationProviderDecision,
    route_snapshot_hash: str,
    policy_version: str,
) -> dict[str, Any]:
    event = {
        "type": "feishu_authorization_provider_decision",
        "provider_id": provider_decision.provider_id,
        "provider_version": provider_decision.provider_version,
        "evidence_source_class": provider_decision.evidence_source_class,
        "provider_reachability_class": provider_decision.reachability_state,
        "credential_freshness_class": provider_decision.credential_freshness,
        "acl_completeness_class": (
            "complete" if provider_decision.acl_complete else "incomplete"
        ),
        "unsupported_scope_status": (
            "none"
            if provider_decision.unsupported_scope is None
            else "unsupported"
        ),
        "decision_hash": provider_decision.decision_hash,
        "contract_hash": request.contract.contract_hash,
        "route_snapshot_hash": route_snapshot_hash,
        "object_ref_hash": request.object_ref.value_hash,
        "authority_subject_hash": _authority_subject_hash(
            request.contract.authority_subject_ref,
            request,
        ),
        "policy_version": policy_version,
    }
    failure_class = provider_decision.denial_failure_class
    if failure_class is not None:
        event["failure_class"] = failure_class
    if provider_decision.revocation_reason is not None:
        event["revocation_reason_class"] = provider_decision.revocation_reason
    return event


def _denial_audit_event_templates(
    request: BrokerPolicyRequest,
    *,
    failure_class: str,
    denial_reason_class: str,
    policy_version: str,
) -> tuple[dict[str, Any], ...]:
    common = {
        "request_hash": request.request_hash,
        "contract_hash": request.contract.contract_hash,
        "object_ref_hash": request.object_ref.value_hash,
        "action": request.action,
        "failure_class": failure_class,
        "denial_reason_class": denial_reason_class,
        "policy_version": policy_version,
    }
    return (
        {
            "type": "feishu_broker_policy_denied",
            **common,
            "route_snapshot_hash": _audit_route_snapshot_hash(request),
            "authority_subject_hash": _authority_subject_hash(
                request.contract.authority_subject_ref,
                request,
            ),
        },
        {
            "type": "feishu_capability_denied",
            **common,
        },
    )


def _authority_subject_hash(ref: HashedRef | None, request: BrokerPolicyRequest) -> str:
    if ref is not None:
        return ref.value_hash
    return request.request_hash


def _audit_route_snapshot_hash(request: BrokerPolicyRequest) -> str:
    try:
        return _route_snapshot_hash(
            request.contract.route_session_key_snapshot,
            "route_session_key_snapshot",
        )
    except BrokerPolicyError:
        return request.request_hash


def _object_ref_hash(ref: HashedRef | None, fallback: HashedRef) -> str:
    if ref is not None:
        return ref.value_hash
    return fallback.value_hash


def _provider_failure(
    result: Any,
    *,
    now: datetime,
    policy_version: str,
    provider_id: str,
) -> tuple[str, str] | None:
    if not isinstance(result, AuthorizationProviderResult):
        return (
            "feishu_broker_policy_denied",
            "feishu_authorization_provider_result_malformed",
        )
    decision = getattr(result, "decision", None)
    if not isinstance(decision, AuthorizationProviderDecision):
        return (
            "feishu_broker_policy_denied",
            "feishu_authorization_provider_decision_missing",
        )
    decision_fields = _provider_decision_fields(decision)
    if decision_fields is None:
        return (
            "feishu_broker_policy_denied",
            "feishu_authorization_provider_decision_malformed",
        )
    decision_failure = _provider_decision_field_failure(
        decision_fields,
        provider_id=provider_id,
    )
    if decision_failure is not None:
        return "feishu_broker_policy_denied", decision_failure
    if decision_fields["provider_id"] != provider_id:
        return "feishu_broker_policy_denied", "feishu_provider_identity_mismatch"
    failure_class = getattr(result, "failure_class", None)
    if failure_class is not None:
        if failure_class not in _STABLE_PROVIDER_FAILURE_CLASSES:
            return (
                "feishu_broker_policy_denied",
                "feishu_authorization_provider_failure_class_invalid",
            )
        return failure_class, failure_class
    if decision_fields["denial_failure_class"] is not None:
        if decision_fields["denial_failure_class"] not in _STABLE_PROVIDER_FAILURE_CLASSES:
            return (
                "feishu_broker_policy_denied",
                "feishu_authorization_provider_decision_malformed",
            )
        return (
            decision_fields["denial_failure_class"],
            decision_fields["denial_failure_class"],
        )
    if decision_fields["revocation_reason"] is not None:
        return "feishu_broker_policy_denied", "feishu_provider_decision_inconsistent"
    if decision_fields["policy_version"] != policy_version:
        return "feishu_broker_policy_denied", "feishu_provider_policy_version_mismatch"
    if decision_fields["reachability_state"] != "reachable":
        return "feishu_provider_sdk_unreachable", "feishu_provider_sdk_unreachable"
    if (
        decision_fields["credential_freshness"] == "stale"
        or decision_fields["freshness_class"] == "stale"
    ):
        return "feishu_provider_stale_credential", "feishu_provider_stale_credential"
    if (
        decision_fields["credential_freshness"] == "revoked"
        or decision_fields["freshness_class"] == "revoked"
    ):
        return "feishu_provider_revoked_credential", "feishu_provider_revoked_credential"
    if (
        decision_fields["credential_freshness"] != "fresh"
        or decision_fields["freshness_class"] != "current"
    ):
        return "feishu_provider_credential_unknown", "feishu_provider_credential_unknown"
    if not decision_fields["acl_complete"]:
        return "feishu_provider_acl_incomplete", "feishu_provider_acl_incomplete"
    if decision_fields["unsupported_scope"] is not None:
        return "feishu_provider_unsupported_scope", "feishu_provider_unsupported_scope"
    if (
        decision_fields["expires_at"] is not None
        and _parse_utc(decision_fields["expires_at"]) <= now
    ):
        return "feishu_broker_policy_denied", "feishu_provider_decision_expired"
    return None


def _provider_decision_fields(
    decision: AuthorizationProviderDecision,
) -> dict[str, Any] | None:
    try:
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
            "decision_hash": decision.decision_hash,
        }
    except AttributeError:
        return None


def _provider_decision_field_failure(
    decision_fields: Mapping[str, Any],
    *,
    provider_id: str,
) -> str | None:
    if not _is_classifier_or_hash(decision_fields["provider_id"]):
        return "feishu_authorization_provider_decision_malformed"
    if decision_fields["provider_id"] != provider_id:
        return None
    if not _is_classifier_or_hash(decision_fields["provider_version"]):
        return "feishu_authorization_provider_decision_malformed"
    if not _is_classifier_or_hash(decision_fields["policy_version"]):
        return "feishu_authorization_provider_decision_malformed"
    if not _is_known_classifier(
        decision_fields["evidence_source_class"],
        _PROVIDER_EVIDENCE_SOURCE_CLASSES,
    ):
        return "feishu_authorization_provider_decision_malformed"
    if not _is_known_classifier(
        decision_fields["reachability_state"],
        _PROVIDER_REACHABILITY_STATES,
    ):
        return "feishu_authorization_provider_decision_malformed"
    if not _is_known_classifier(
        decision_fields["credential_freshness"],
        _PROVIDER_CREDENTIAL_FRESHNESS_CLASSES,
    ):
        return "feishu_authorization_provider_decision_malformed"
    if decision_fields["freshness_class"] is not None and not _is_known_classifier(
        decision_fields["freshness_class"],
        _PROVIDER_FRESHNESS_CLASSES,
    ):
        return "feishu_authorization_provider_decision_malformed"
    for timestamp_field in ("issued_at", "expires_at"):
        value = decision_fields[timestamp_field]
        if value is not None and not _is_utc_timestamp(value):
            return "feishu_authorization_provider_decision_malformed"
    if not isinstance(decision_fields["acl_complete"], bool):
        return "feishu_authorization_provider_decision_malformed"
    for field_name in ("unsupported_scope", "revocation_reason"):
        value = decision_fields[field_name]
        if value is not None and not _is_classifier_or_hash(value):
            return "feishu_authorization_provider_decision_malformed"
    denial_failure_class = decision_fields["denial_failure_class"]
    if denial_failure_class is not None and (
        not _is_classifier_or_hash(denial_failure_class)
        or denial_failure_class not in _STABLE_PROVIDER_FAILURE_CLASSES
    ):
        return "feishu_authorization_provider_decision_malformed"
    if not _is_sha256_hash(decision_fields["decision_hash"]):
        return "feishu_authorization_provider_decision_malformed"
    return None


def _authorization_evidence_failure(evidence: AuthorizationEvidence) -> str | None:
    try:
        fields = {
            "evidence_kind": evidence.evidence_kind,
            "authority_subject_ref": evidence.authority_subject_ref,
            "route_session_key_snapshot": evidence.route_session_key_snapshot,
            "object_ref": evidence.object_ref,
            "scopes": evidence.scopes,
            "token_class": evidence.token_class,
            "evidence_state": evidence.evidence_state,
            "evidence_hash": evidence.evidence_hash,
        }
    except AttributeError:
        return "feishu_authorization_evidence_malformed"
    if not _is_known_classifier(
        fields["evidence_kind"],
        _AUTHORIZATION_EVIDENCE_KINDS,
    ):
        return "feishu_authorization_evidence_malformed"
    if not _is_optional_hashed_ref(fields["authority_subject_ref"]):
        return "feishu_authorization_evidence_malformed"
    if not _is_route_snapshot_hash(fields["route_session_key_snapshot"]):
        return "feishu_authorization_evidence_malformed"
    if not _is_optional_hashed_ref(fields["object_ref"]):
        return "feishu_authorization_evidence_malformed"
    if not _is_classifier_or_hash_tuple(fields["scopes"]):
        return "feishu_authorization_evidence_malformed"
    if fields["token_class"] is not None and fields["token_class"] not in _TOKEN_CLASSES:
        return "feishu_authorization_evidence_malformed"
    if not _is_known_classifier(
        fields["evidence_state"],
        _AUTHORIZATION_EVIDENCE_STATES,
    ):
        return "feishu_authorization_evidence_malformed"
    if not _is_sha256_hash(fields["evidence_hash"]):
        return "feishu_authorization_evidence_malformed"
    return None


def _is_classifier_or_hash(value: Any) -> bool:
    try:
        _classifier_or_hash(value, "provider_boundary_field")
    except BrokerPolicyError:
        return False
    return True


def _is_classifier_or_hash_tuple(value: Any) -> bool:
    if not isinstance(value, tuple):
        return False
    return all(_is_classifier_or_hash(item) for item in value)


def _is_known_classifier(value: Any, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _is_sha256_hash(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_HASH_RE.fullmatch(value) is not None


def _is_utc_timestamp(value: Any) -> bool:
    try:
        _require_utc_timestamp(value, "provider_boundary_timestamp")
    except BrokerPolicyError:
        return False
    return True


def _is_route_snapshot_hash(value: Any) -> bool:
    try:
        _route_snapshot_hash(value, "provider_boundary_route_snapshot")
    except BrokerPolicyError:
        return False
    return True


def _is_optional_hashed_ref(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, HashedRef):
        return False
    try:
        kind = value.kind
        value_hash = value.value_hash
        schema_version = value.schema_version
    except AttributeError:
        return False
    return (
        _is_classifier_or_hash(kind)
        and _is_sha256_hash(value_hash)
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version >= 1
    )


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
        prior.grant_semantics == request.grant_semantics,
        prior.expires_at == request.expires_at,
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
    audit_event_templates: tuple[dict[str, Any], ...] = (),
) -> BrokerPolicyDecision:
    return BrokerPolicyDecision(
        grant=None,
        failure_class=failure_class,
        denial_reason_class=denial_reason_class or failure_class,
        audit_event_templates=audit_event_templates,
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


def _classifier_or_hash_tuple(value: Any, field_name: str) -> tuple[str, ...]:
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
    return tuple(_classifier_or_hash(item, field_name) for item in items)


def _classifier_or_hash(value: Any, field_name: str) -> str:
    _require_nonempty_string(value, field_name)
    normalized = unicodedata.normalize("NFC", value)
    if _looks_like_raw_local_path(normalized):
        raise BrokerPolicyError(
            f"{field_name} must not contain a raw local path",
            failure_class="sensitive_raw_field",
        )
    comparable = _normalized_key_for_policy(normalized)
    if (
        normalized not in _BROKER_SAFE_CLASSIFIERS
        and _RAW_FEISHU_ID_VALUE_RE.fullmatch(normalized)
    ):
        raise BrokerPolicyError(
            f"{field_name} must not contain a raw Feishu identifier",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_MARKERS):
        raise BrokerPolicyError(
            f"{field_name} must not contain raw provider material",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_VALUE_MARKERS):
        raise BrokerPolicyError(
            f"{field_name} must not contain raw provider secrets",
            failure_class="sensitive_raw_field",
        )
    if _SHA256_HASH_RE.fullmatch(normalized):
        return normalized
    if _CLASSIFIER_METADATA_STRING_RE.fullmatch(normalized):
        return normalized
    raise BrokerPolicyError(
        f"{field_name} must be a stable classifier or sha256 hash",
        failure_class="invalid_feishu_broker_policy_request",
    )


def _route_snapshot_hash(value: Any, field_name: str) -> str:
    _require_nonempty_string(value, field_name)
    normalized = unicodedata.normalize("NFC", value)
    if _ROUTE_SNAPSHOT_HASH_RE.fullmatch(normalized):
        return normalized
    if _looks_like_raw_local_path(normalized):
        raise BrokerPolicyError(
            f"{field_name} must not contain a raw local path",
            failure_class="sensitive_raw_field",
        )
    comparable = _normalized_key_for_policy(normalized)
    if _RAW_FEISHU_ID_VALUE_RE.fullmatch(normalized):
        raise BrokerPolicyError(
            f"{field_name} must not contain a raw Feishu identifier",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_MARKERS):
        raise BrokerPolicyError(
            f"{field_name} must not contain raw provider material",
            failure_class="sensitive_raw_field",
        )
    if any(marker in comparable for marker in _PROVIDER_RAW_VALUE_MARKERS):
        raise BrokerPolicyError(
            f"{field_name} must not contain raw provider secrets",
            failure_class="sensitive_raw_field",
        )
    raise BrokerPolicyError(
        f"{field_name} must be a hashed route snapshot",
        failure_class="invalid_feishu_broker_policy_request",
    )


def _normalized_key_for_policy(key: str) -> str:
    normalized = unicodedata.normalize("NFC", key).lower()
    return _NORMALIZED_KEY_CHARS_RE.sub("", normalized)


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

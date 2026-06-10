import re
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gateway.feishu_authorization_providers import (
    AuthorizationProviderDecision,
    AuthorizationProviderResult,
    FakeAuthorizationProvider,
)
from gateway.feishu_broker_policy import (
    BrokerPolicyDecision,
    BrokerPolicyError,
    BrokerPolicyRequest,
    BrokerPolicyReplayRecord,
    issue_object_capability_grant,
)
from gateway.gateway_event_contract import validate_gateway_event
from gateway.feishu_contracts import (
    AuthorizationEvidence,
    ConversationContract,
    HashedRef,
)
from gateway.feishu_legacy_guard import (
    current_feishu_broker_context,
    current_feishu_object_capability_context,
    feishu_broker_context,
    feishu_object_capability_context,
    require_feishu_broker_context,
    require_feishu_object_capability_context,
)


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_NOW = datetime(2026, 6, 10, 0, 1, tzinfo=UTC)
_CONTRACT_HASH = "sha256:" + "1" * 64
_ROUTE_SNAPSHOT = "route_session_snapshot_hash:sha256:" + "2" * 64
_SUBJECT_REF = HashedRef(kind="feishu_user", value_hash="sha256:" + "b" * 64)
_OTHER_SUBJECT_REF = HashedRef(kind="feishu_user", value_hash="sha256:" + "c" * 64)
_OBJECT_REF = HashedRef(kind="feishu_doc", value_hash="sha256:" + "a" * 64)
_OTHER_OBJECT_REF = HashedRef(kind="feishu_doc", value_hash="sha256:" + "d" * 64)
_REQUEST_ID_HASH = "sha256:" + "3" * 64
_PAYLOAD_HASH = "sha256:" + "4" * 64
_OTHER_PAYLOAD_HASH = "sha256:" + "5" * 64


def _contract(**overrides) -> ConversationContract:
    values = {
        "platform_account_id": "feishu_app:test",
        "tenant_partition_key": "tenant:test",
        "app_partition_key": "app:test",
        "conversation_scope_id": "cs_test",
        "shared_context_scope_id": "shared_context:oc_test",
        "route_partition_key": "route_partition_hash:sha256:" + "6" * 64,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "scope_assignment_status": "scoped",
        "actor_ref": HashedRef(kind="feishu_actor", value_hash="sha256:" + "7" * 64),
        "authority_subject_ref": _SUBJECT_REF,
        "session_id": "session:test",
        "thread_anchor_ref": HashedRef(
            kind="feishu_thread",
            value_hash="sha256:" + "8" * 64,
        ),
        "root_anchor_ref": HashedRef(
            kind="feishu_message",
            value_hash="sha256:" + "9" * 64,
        ),
        "identity_evidence_set": (
            HashedRef(kind="message_actor", value_hash="sha256:" + "e" * 64),
        ),
        "policy_version": "policy:v1",
        "evidence_state": "current",
    }
    values.update(overrides)
    return ConversationContract(**values)


def _request(**overrides) -> BrokerPolicyRequest:
    values = {
        "contract": _contract(),
        "provider_id": "fake_verified_object_acl",
        "object_type": "doc",
        "object_ref": _OBJECT_REF,
        "action": "read",
        "requested_scopes": ("doc:read",),
        "grant_semantics": "one_time",
        "expires_at": None,
        "request_id_hash": _REQUEST_ID_HASH,
        "payload_hash": _PAYLOAD_HASH,
    }
    values.update(overrides)
    return BrokerPolicyRequest(**values)


def _fake_provider(
    evidence_source_class: str = "verified_object_acl",
    **overrides,
) -> FakeAuthorizationProvider:
    values = {
        "provider_id": "fake_verified_object_acl",
        "provider_version": "2026-06-10.fake",
        "evidence_source_class": evidence_source_class,
        "authority_subject_ref": _SUBJECT_REF,
        "object_ref": _OBJECT_REF,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "scopes": ("doc:read", "doc:write", "doc:delete"),
    }
    values.update(overrides)
    return FakeAuthorizationProvider(**values)


def _decision(**overrides) -> AuthorizationProviderDecision:
    values = {
        "provider_id": "custom_provider",
        "provider_version": "2026-06-10.custom",
        "policy_version": "policy:v1",
        "evidence_source_class": "verified_object_acl",
        "reachability_state": "reachable",
        "issued_at": "2026-06-10T00:00:00Z",
        "expires_at": "2026-06-10T00:05:00Z",
        "freshness_class": "current",
        "credential_freshness": "fresh",
        "acl_complete": True,
    }
    values.update(overrides)
    return AuthorizationProviderDecision(**values)


def _malformed_typed_decision(**overrides) -> AuthorizationProviderDecision:
    values = {
        "provider_id": "custom_provider",
        "provider_version": "2026-06-10.custom",
        "policy_version": "policy:v1",
        "evidence_source_class": "verified_object_acl",
        "reachability_state": "reachable",
        "issued_at": "2026-06-10T00:00:00Z",
        "expires_at": "2026-06-10T00:05:00Z",
        "freshness_class": "current",
        "credential_freshness": "fresh",
        "acl_complete": True,
        "unsupported_scope": None,
        "revocation_reason": None,
        "denial_failure_class": None,
        "decision_hash": "sha256:" + "7" * 64,
    }
    values.update(overrides)
    decision = AuthorizationProviderDecision.__new__(AuthorizationProviderDecision)
    for field_name, value in values.items():
        object.__setattr__(decision, field_name, value)
    return decision


def _evidence(**overrides) -> AuthorizationEvidence:
    values = {
        "evidence_kind": "verified_object_acl",
        "authority_subject_ref": _SUBJECT_REF,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "object_ref": _OBJECT_REF,
        "scopes": ("doc:read", "doc:write", "doc:delete"),
        "token_class": "user_access_token",
        "evidence_state": "current",
    }
    values.update(overrides)
    return AuthorizationEvidence(**values)


def _malformed_typed_evidence(**overrides) -> AuthorizationEvidence:
    values = {
        "evidence_kind": "verified_object_acl",
        "authority_subject_ref": _SUBJECT_REF,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "object_ref": _OBJECT_REF,
        "scopes": ("doc:read", "doc:write", "doc:delete"),
        "token_class": "user_access_token",
        "evidence_state": "current",
        "evidence_hash": "sha256:" + "8" * 64,
    }
    values.update(overrides)
    evidence = AuthorizationEvidence.__new__(AuthorizationEvidence)
    for field_name, value in values.items():
        object.__setattr__(evidence, field_name, value)
    return evidence


def _provider_result_invariant_bypass(
    *,
    evidence,
    decision,
    failure_class=None,
) -> AuthorizationProviderResult:
    result = AuthorizationProviderResult.__new__(AuthorizationProviderResult)
    object.__setattr__(result, "evidence", evidence)
    object.__setattr__(result, "decision", decision)
    object.__setattr__(result, "failure_class", failure_class)
    return result


class _StaticResultProvider:
    provider_id = "custom_provider"
    provider_version = "2026-06-10.custom"

    def __init__(self, result) -> None:
        self._result = result

    def authorize(self, request):
        return self._result


class _RaisingProvider:
    provider_id = "custom_provider"
    provider_version = "2026-06-10.custom"

    def authorize(self, request):
        raise RuntimeError("provider boundary failure")


def _issue(request=None, registry=None):
    return issue_object_capability_grant(
        request or _request(),
        registry if registry is not None else {"fake_verified_object_acl": _fake_provider()},
        now=_NOW,
        policy_version="policy:v1",
    )


@pytest.mark.parametrize(
    ("request_overrides", "expected_failure_class"),
    [
        ({"object_type": "doc/path/secret"}, "sensitive_raw_field"),
        ({"action": "read/path"}, "invalid_feishu_broker_policy_request"),
        ({"requested_scopes": ("doc:read/path",)}, "invalid_feishu_broker_policy_request"),
        ({"requested_scopes": ("doc:read scope",)}, "invalid_feishu_broker_policy_request"),
    ],
)
def test_broker_policy_request_rejects_raw_classifier_material(
    request_overrides,
    expected_failure_class,
):
    with pytest.raises(BrokerPolicyError) as exc_info:
        _request(**request_overrides)

    assert exc_info.value.failure_class == expected_failure_class


@pytest.mark.parametrize(
    "request_overrides",
    [
        {"object_type": "doccnrawfeishuobjectid"},
        {"object_type": "app_token_raw_id"},
        {"action": "tenantaccesstokensecret"},
        {"requested_scopes": ("doc:tenantaccesstokensecret",)},
        {"requested_scopes": ("doc:doccnrawfeishuobjectid",)},
    ],
)
def test_broker_policy_request_rejects_sensitive_classifier_material(
    request_overrides,
):
    with pytest.raises(BrokerPolicyError) as exc_info:
        _request(**request_overrides)

    assert exc_info.value.failure_class == "sensitive_raw_field"


@pytest.mark.parametrize(
    "replay_overrides",
    [
        {"object_type": "doccnrawfeishuobjectid"},
        {"action": "tenantaccesstokensecret"},
    ],
)
def test_broker_policy_replay_record_rejects_sensitive_classifier_material(
    replay_overrides,
):
    values = {
        "request_id_hash": _REQUEST_ID_HASH,
        "payload_hash": _PAYLOAD_HASH,
        "provider_decision_hash": "sha256:" + "7" * 64,
        "route_snapshot_hash": _ROUTE_SNAPSHOT,
        "object_ref_hash": _OBJECT_REF.value_hash,
        "object_type": "doc",
        "action": "read",
        "grant_semantics": "one_time",
        "grant_hash": "sha256:" + "6" * 64,
        "expires_at": None,
    }
    values.update(replay_overrides)

    with pytest.raises(BrokerPolicyError) as exc_info:
        BrokerPolicyReplayRecord(**values)

    assert exc_info.value.failure_class == "sensitive_raw_field"


def test_broker_policy_replay_record_rejects_raw_route_snapshot():
    with pytest.raises(BrokerPolicyError) as exc_info:
        BrokerPolicyReplayRecord(
            request_id_hash=_REQUEST_ID_HASH,
            payload_hash=_PAYLOAD_HASH,
            provider_decision_hash="sha256:" + "7" * 64,
            route_snapshot_hash="oc_raw_chat_route",
            object_ref_hash=_OBJECT_REF.value_hash,
            object_type="doc",
            action="read",
            grant_semantics="one_time",
            grant_hash="sha256:" + "6" * 64,
            expires_at=None,
        )

    assert exc_info.value.failure_class == "sensitive_raw_field"


def test_broker_policy_request_classifier_values_still_issue_grant():
    decision = _issue(
        _request(
            object_type="doc",
            action="read",
            requested_scopes=("doc:read",),
        ),
        {"fake_verified_object_acl": _fake_provider(scopes=("doc:read",))},
    )

    assert decision.failure_class is None
    assert decision.grant is not None
    assert decision.grant.object_capability_grant.object_type == "doc"
    assert decision.grant.object_capability_grant.action == "read"


def test_broker_policy_request_keeps_app_token_only_as_safe_classifier():
    request = _request(provider_id="app_token_only")

    assert request.provider_id == "app_token_only"


def test_missing_provider_registry_returns_provider_missing_without_grant():
    decision = _issue(registry={})

    assert decision.grant is None
    assert decision.failure_class == "feishu_authorization_provider_missing"
    assert decision.denial_reason_class == "feishu_authorization_provider_missing"


def test_missing_evidence_returns_evidence_missing_without_grant():
    malformed_result = _provider_result_invariant_bypass(
        evidence=None,
        decision=_decision(),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_authorization_evidence_missing"
    assert decision.denial_reason_class == "feishu_authorization_evidence_missing"


def test_duck_typed_denial_provider_result_is_malformed_without_grant():
    duck_typed_result = SimpleNamespace(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(),
        failure_class=None,
        is_denial=True,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(duck_typed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_result_malformed"
    )


def test_duck_typed_positive_provider_result_without_denial_flag_is_malformed():
    duck_typed_result = SimpleNamespace(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(duck_typed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_result_malformed"
    )


def test_malformed_provider_failure_class_is_normalized_without_leaking_raw_reason():
    malformed_result = _provider_result_invariant_bypass(
        evidence=None,
        decision=_decision(),
        failure_class="tenant_access_token_secret",
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_failure_class_invalid"
    )


def test_malformed_typed_provider_decision_truthy_acl_complete_is_denied():
    malformed_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_malformed_typed_decision(acl_complete="yes"),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_malformed"
    )


def test_malformed_typed_authorization_evidence_unknown_state_is_denied():
    malformed_result = _provider_result_invariant_bypass(
        evidence=_malformed_typed_evidence(evidence_state="unknown"),
        decision=_decision(),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_authorization_evidence_malformed"


def test_malformed_typed_decision_denial_failure_class_does_not_leak_raw_marker():
    malformed_result = _provider_result_invariant_bypass(
        evidence=None,
        decision=_malformed_typed_decision(
            denial_failure_class="tenant_access_token_secret"
        ),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_malformed"
    )
    assert decision.denial_reason_class != "tenant_access_token_secret"


def test_malformed_typed_decision_invalid_expires_at_is_denied_without_raise():
    malformed_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_malformed_typed_decision(expires_at="not-a-timestamp"),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_malformed"
    )


def test_real_provider_denial_result_preserves_stable_provider_failure():
    result = AuthorizationProviderResult(
        evidence=None,
        decision=_decision(
            denial_failure_class="feishu_provider_unsupported_scope",
            unsupported_scope="doc:write",
        ),
        failure_class="feishu_provider_unsupported_scope",
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_provider_unsupported_scope"
    assert decision.denial_reason_class == "feishu_provider_unsupported_scope"


@pytest.mark.parametrize(
    ("provider_overrides", "expected_failure"),
    [
        ({"provider_available": False}, "feishu_provider_unavailable"),
        ({"sdk_reachable": False}, "feishu_provider_sdk_unreachable"),
        ({"app_token_available": False}, "feishu_provider_app_token_unavailable"),
        ({"credential_freshness": "stale"}, "feishu_provider_stale_credential"),
        ({"credential_freshness": "revoked"}, "feishu_provider_revoked_credential"),
        ({"acl_complete": False}, "feishu_provider_acl_incomplete"),
        ({"evidence_source_class": "unsupported_provider"}, "feishu_provider_unsupported"),
    ],
)
def test_provider_denial_states_fail_before_grant_issuance(
    provider_overrides,
    expected_failure,
):
    provider = _fake_provider(**provider_overrides)

    decision = _issue(registry={"fake_verified_object_acl": provider})

    assert decision.grant is None
    assert decision.failure_class == expected_failure
    assert decision.denial_reason_class == expected_failure


def test_provider_unsupported_scope_fails_before_grant_issuance():
    decision = _issue(
        _request(action="write", requested_scopes=("doc:write",)),
        {"fake_verified_object_acl": _fake_provider(scopes=("doc:read",))},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_provider_unsupported_scope"
    assert decision.denial_reason_class == "feishu_provider_unsupported_scope"


@pytest.mark.parametrize(
    ("source_class", "expected_failure"),
    [
        ("app_token_only", "feishu_app_token_only_evidence"),
        ("discovery_only", "feishu_discovery_only_evidence"),
    ],
)
def test_non_object_authority_evidence_is_denied_without_grant(
    source_class,
    expected_failure,
):
    decision = _issue(registry={"fake_verified_object_acl": _fake_provider(source_class)})

    assert decision.grant is None
    assert decision.failure_class == expected_failure
    assert decision.denial_reason_class == expected_failure


def test_confirmation_only_p3_object_action_is_denied_without_grant():
    decision = _issue(
        _request(action="delete", requested_scopes=("doc:delete",)),
        {"fake_verified_object_acl": _fake_provider("explicit_user_confirmation")},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_p3_requires_object_authority_evidence"
    assert decision.denial_reason_class == "feishu_p3_requires_object_authority_evidence"


def test_issued_broker_policy_grant_creates_object_capability_context_only():
    decision = _issue()
    assert decision.grant is not None

    with feishu_object_capability_context(decision.grant) as context:
        allowed, reason = require_feishu_object_capability_context(
            "tool",
            "feishu_doc_read",
        )

        assert allowed is True
        assert reason == ""
        assert current_feishu_object_capability_context() == context
        assert context.grant is decision.grant
        assert context.object_capability_grant is decision.grant.object_capability_grant
        assert current_feishu_broker_context() is None
        assert require_feishu_broker_context("tool", "feishu_doc_read") == (
            False,
            "feishu_legacy_tool_requires_broker",
        )

    assert current_feishu_object_capability_context() is None


def test_card_context_does_not_upgrade_confirmation_only_p3_to_object_authority():
    with feishu_broker_context(
        "broker_grant_handle:sha256:" + ("b" * 64),
        action_id="broker_action:sha256:" + ("c" * 64),
        contract_hash=_CONTRACT_HASH,
        route_partition_key="route_snapshot:sha256:" + ("d" * 64),
    ):
        decision = _issue(
            _request(action="delete", requested_scopes=("doc:delete",)),
            {"fake_verified_object_acl": _fake_provider("explicit_user_confirmation")},
        )

        assert decision.grant is None
        assert decision.failure_class == "feishu_p3_requires_object_authority_evidence"
        assert current_feishu_object_capability_context() is None


@pytest.mark.parametrize(
    ("evidence_overrides", "request_overrides", "contract_overrides", "expected_failure"),
    [
        (
            {"authority_subject_ref": _OTHER_SUBJECT_REF},
            {},
            {},
            "feishu_authority_subject_mismatch",
        ),
        (
            {"object_ref": _OTHER_OBJECT_REF},
            {},
            {},
            "feishu_object_ref_mismatch",
        ),
        (
            {"route_session_key_snapshot": "route_session_snapshot_hash:sha256:" + "f" * 64},
            {},
            {},
            "feishu_route_snapshot_mismatch",
        ),
        ({}, {}, {"evidence_state": "stale"}, "feishu_contract_evidence_stale"),
        ({}, {}, {"evidence_state": "revoked"}, "feishu_contract_evidence_revoked"),
        (
            {},
            {},
            {"scope_assignment_status": "legacy_unscoped"},
            "feishu_scope_not_scoped",
        ),
        (
            {"evidence_state": "stale"},
            {},
            {},
            "feishu_authorization_evidence_stale",
        ),
        (
            {"evidence_state": "revoked"},
            {},
            {},
            "feishu_authorization_evidence_revoked",
        ),
        (
            {
                "evidence_kind": "explicit_user_confirmation",
                "token_class": "user_access_token",
            },
            {},
            {},
            "feishu_object_authority_evidence_missing",
        ),
        (
            {"scopes": ("doc:read",)},
            {"action": "write", "requested_scopes": ("doc:write",)},
            {},
            "feishu_object_authority_scope_insufficient",
        ),
    ],
)
def test_contract_evidence_failures_are_preserved_without_grant(
    evidence_overrides,
    request_overrides,
    contract_overrides,
    expected_failure,
):
    evidence = _evidence(**evidence_overrides)
    result = _provider_result_invariant_bypass(
        evidence=evidence,
        decision=_decision(evidence_source_class=evidence.evidence_kind),
    )
    request = _request(
        provider_id="custom_provider",
        contract=_contract(**contract_overrides),
        **request_overrides,
    )

    decision = _issue(request, {"custom_provider": _StaticResultProvider(result)})

    assert decision.grant is None
    assert decision.failure_class == expected_failure
    assert decision.denial_reason_class == expected_failure
    events = decision.audit_events(
        correlation_id="corr-audit-contract-denial",
        timestamp=1_700_000_107,
    )
    assert [event["type"] for event in events] == [
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]
        assert event["failure_class"] == expected_failure
        assert event["denial_reason_class"] == expected_failure


def test_valid_object_authority_evidence_issues_bound_broker_grant_wrapper():
    decision = _issue()

    assert decision.failure_class is None
    assert decision.denial_reason_class is None
    assert decision.grant is not None
    grant = decision.grant
    assert grant.object_capability_grant.contract_hash == _contract().contract_hash
    assert grant.object_capability_grant.evidence_hashes
    assert grant.object_capability_grant.object_type == "doc"
    assert grant.object_capability_grant.object_ref == _OBJECT_REF
    assert grant.object_capability_grant.action == "read"
    assert grant.object_capability_grant.authority_subject_ref == _SUBJECT_REF
    assert grant.route_snapshot_hash == _ROUTE_SNAPSHOT
    assert grant.policy_version == "policy:v1"
    assert grant.grant_semantics == "one_time"
    assert grant.expires_at is None
    assert grant.request_id_hash == _REQUEST_ID_HASH
    assert grant.payload_hash == _PAYLOAD_HASH
    assert _SHA256_HASH_RE.fullmatch(grant.provider_decision_hash)
    assert grant.replay_record.request_id_hash == _REQUEST_ID_HASH
    assert grant.replay_record.payload_hash == _PAYLOAD_HASH
    assert grant.replay_record.route_snapshot_hash == _ROUTE_SNAPSHOT
    assert grant.replay_record.object_ref_hash == _OBJECT_REF.value_hash
    assert grant.replay_record.object_type == "doc"
    assert grant.replay_record.action == "read"
    assert grant.replay_record.provider_decision_hash == grant.provider_decision_hash
    assert _SHA256_HASH_RE.fullmatch(grant.object_capability_grant.grant_hash)
    assert _SHA256_HASH_RE.fullmatch(grant.grant_hash)


def test_grant_decision_emits_sanitized_audit_events_for_provider_evidence_and_grant():
    decision = _issue()
    assert decision.grant is not None

    events = decision.audit_events(correlation_id="corr-audit-1", timestamp=1_700_000_100)

    event_types = [event["type"] for event in events]
    assert event_types == [
        "feishu_authorization_provider_decision",
        "feishu_authorization_evidence_observed",
        "feishu_capability_granted",
        "feishu_auth_decision",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]
    rendered = repr(events)
    assert "ou_" not in rendered
    assert "oc_" not in rendered
    assert "tenant_access_token" not in rendered
    provider_event = events[0]
    assert provider_event["provider_id"] == "fake_verified_object_acl"
    assert provider_event["provider_reachability_class"] == "reachable"
    assert provider_event["credential_freshness_class"] == "fresh"
    assert provider_event["acl_completeness_class"] == "complete"
    grant_event = events[2]
    assert grant_event["grant_hash"] == decision.grant.object_capability_grant.grant_hash
    assert grant_event["evidence_hashes"] == list(
        decision.grant.object_capability_grant.evidence_hashes
    )
    assert grant_event["object_ref_hash"] == _OBJECT_REF.value_hash
    assert grant_event["authority_subject_hash"] == _SUBJECT_REF.value_hash
    assert grant_event["expiry"] == "no_expiry"
    assert grant_event["grant_session_class"] == "one_time"


def test_user_delegated_credential_grant_audit_events_validate_as_semantic_class():
    provider_result = AuthorizationProviderResult(
        evidence=_evidence(evidence_kind="user_delegated_credential"),
        decision=_decision(evidence_source_class="user_delegated_credential"),
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(provider_result)},
    )
    events = decision.audit_events(
        correlation_id="corr-user-delegated-credential",
        timestamp=1_700_000_109,
    )

    assert decision.grant is not None
    assert [event["type"] for event in events] == [
        "feishu_authorization_provider_decision",
        "feishu_authorization_evidence_observed",
        "feishu_capability_granted",
        "feishu_auth_decision",
    ]
    assert events[0]["evidence_source_class"] == "user_delegated_credential"
    assert events[1]["evidence_source_class"] == "user_delegated_credential"
    for event in events:
        assert validate_gateway_event(event) == event["type"]


def test_malformed_provider_state_emits_denial_audit_without_success_events():
    malformed_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_malformed_typed_decision(acl_complete="yes"),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )
    events = decision.audit_events(correlation_id="corr-audit-2", timestamp=1_700_000_101)

    assert decision.grant is None
    assert [event["type"] for event in events] == [
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]
        assert event["failure_class"] == "feishu_broker_policy_denied"
        assert (
            event["denial_reason_class"]
            == "feishu_authorization_provider_decision_malformed"
        )
    assert "feishu_capability_granted" not in repr(events)


def test_authorization_provider_decision_missing_denial_audit_events_validate():
    result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=None,
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(result)},
    )
    events = decision.audit_events(
        correlation_id="corr-audit-decision-missing",
        timestamp=1_700_000_105,
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_missing"
    )
    assert [event["type"] for event in events] == [
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]
        assert event["failure_class"] == "feishu_broker_policy_denied"
        assert (
            event["denial_reason_class"]
            == "feishu_authorization_provider_decision_missing"
        )


def test_provider_authorization_exception_denial_audit_events_validate():
    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _RaisingProvider()},
    )
    events = decision.audit_events(
        correlation_id="corr-audit-authorization-exception",
        timestamp=1_700_000_106,
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_provider_authorization_exception"
    assert [event["type"] for event in events] == [
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]
        assert event["failure_class"] == "feishu_broker_policy_denied"
        assert event["denial_reason_class"] == "feishu_provider_authorization_exception"


def test_valid_provider_denial_emits_provider_decision_audit_before_denial_events():
    provider_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(
            reachability_state="unreachable",
            credential_freshness="unknown",
            acl_complete=True,
        ),
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(provider_result)},
    )
    events = decision.audit_events(correlation_id="corr-audit-3", timestamp=1_700_000_102)

    assert decision.grant is None
    assert decision.failure_class == "feishu_provider_sdk_unreachable"
    assert decision.denial_reason_class == "feishu_provider_sdk_unreachable"
    assert [event["type"] for event in events] == [
        "feishu_authorization_provider_decision",
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    provider_event = events[0]
    assert validate_gateway_event(provider_event) == provider_event["type"]
    assert provider_event["provider_id"] == "custom_provider"
    assert provider_event["provider_version"] == "2026-06-10.custom"
    assert provider_event["evidence_source_class"] == "verified_object_acl"
    assert provider_event["provider_reachability_class"] == "unreachable"
    assert provider_event["credential_freshness_class"] == "unknown"
    assert provider_event["acl_completeness_class"] == "complete"
    assert provider_event["unsupported_scope_status"] == "none"
    assert provider_event["decision_hash"] == provider_result.decision.decision_hash
    assert provider_event["contract_hash"] == _contract().contract_hash
    assert provider_event["route_snapshot_hash"] == _ROUTE_SNAPSHOT
    assert provider_event["object_ref_hash"] == _OBJECT_REF.value_hash
    assert provider_event["authority_subject_hash"] == _SUBJECT_REF.value_hash
    assert provider_event["policy_version"] == "policy:v1"
    assert provider_event["failure_class"] == "feishu_provider_sdk_unreachable"
    assert provider_event["denial_reason_class"] == "feishu_provider_sdk_unreachable"
    for event in events[1:]:
        assert validate_gateway_event(event) == event["type"]
        assert event["failure_class"] == "feishu_provider_sdk_unreachable"
        assert event["denial_reason_class"] == "feishu_provider_sdk_unreachable"


def test_revoked_provider_denial_emits_traceable_provider_decision_audit():
    provider_result = AuthorizationProviderResult(
        evidence=None,
        decision=_decision(
            evidence_source_class="none",
            reachability_state="reachable",
            issued_at=None,
            expires_at=None,
            freshness_class="revoked",
            credential_freshness="revoked",
            acl_complete=False,
            revocation_reason="credential_revoked",
            denial_failure_class="feishu_provider_revoked_credential",
        ),
        failure_class="feishu_provider_revoked_credential",
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(provider_result)},
    )
    events = decision.audit_events(
        correlation_id="corr-audit-revoked-provider",
        timestamp=1_700_000_108,
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_provider_revoked_credential"
    assert decision.denial_reason_class == "feishu_provider_revoked_credential"
    assert [event["type"] for event in events] == [
        "feishu_authorization_provider_decision",
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    provider_event = events[0]
    assert validate_gateway_event(provider_event) == provider_event["type"]
    assert provider_event["credential_freshness_class"] == "revoked"
    assert provider_event["revocation_reason_class"] == "credential_revoked"
    assert provider_event["failure_class"] == "feishu_provider_revoked_credential"
    assert provider_event["denial_reason_class"] == "feishu_provider_revoked_credential"


def test_revoked_provider_decision_without_reason_is_denied_before_provider_audit():
    provider_result = _provider_result_invariant_bypass(
        evidence=None,
        decision=_decision(
            evidence_source_class="none",
            reachability_state="reachable",
            issued_at=None,
            expires_at=None,
            freshness_class="revoked",
            credential_freshness="revoked",
            acl_complete=False,
            revocation_reason=None,
            denial_failure_class="feishu_provider_revoked_credential",
        ),
        failure_class="feishu_provider_revoked_credential",
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(provider_result)},
    )
    events = decision.audit_events(
        correlation_id="corr-audit-revoked-provider-missing-reason",
        timestamp=1_700_000_110,
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_malformed"
    )
    assert [event["type"] for event in events] == [
        "feishu_broker_policy_denied",
        "feishu_capability_denied",
    ]
    for event in events:
        assert validate_gateway_event(event) == event["type"]


def test_direct_decision_audit_events_reject_raw_template_fields():
    decision = BrokerPolicyDecision(
        failure_class="feishu_broker_policy_denied",
        denial_reason_class="sensitive_raw_field",
        audit_event_templates=(
            {
                "type": "feishu_broker_policy_denied",
                "request_hash": "sha256:" + "8" * 64,
                "contract_hash": _CONTRACT_HASH,
                "route_snapshot_hash": _ROUTE_SNAPSHOT,
                "object_ref_hash": _OBJECT_REF.value_hash,
                "authority_subject_hash": _SUBJECT_REF.value_hash,
                "action": "read",
                "failure_class": "feishu_broker_policy_denied",
                "denial_reason_class": "sensitive_raw_field",
                "policy_version": "policy:v1",
                "raw_acl_json": "{}",
            },
        ),
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        decision.audit_events(correlation_id="corr-audit-raw", timestamp=1_700_000_103)

    assert exc_info.value.failure_class == "invalid_feishu_broker_policy_audit_event"


def test_direct_decision_audit_events_reject_missing_required_c5_fields():
    decision = BrokerPolicyDecision(
        failure_class="feishu_broker_policy_denied",
        denial_reason_class="feishu_provider_acl_incomplete",
        audit_event_templates=(
            {
                "type": "feishu_broker_policy_denied",
                "request_hash": "sha256:" + "8" * 64,
                "contract_hash": _CONTRACT_HASH,
                "route_snapshot_hash": _ROUTE_SNAPSHOT,
                "object_ref_hash": _OBJECT_REF.value_hash,
                "action": "read",
                "failure_class": "feishu_broker_policy_denied",
                "denial_reason_class": "feishu_provider_acl_incomplete",
                "policy_version": "policy:v1",
            },
        ),
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        decision.audit_events(correlation_id="corr-audit-missing", timestamp=1_700_000_104)

    assert exc_info.value.failure_class == "invalid_feishu_broker_policy_audit_event"


@pytest.mark.parametrize("raw_route", ["oc_raw_chat_route", "doccnrawroute"])
def test_raw_contract_route_snapshot_is_denied_without_grant(raw_route):
    decision = _issue(
        _request(contract=replace(_contract(), route_session_key_snapshot=raw_route)),
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "sensitive_raw_field"


def test_broker_grant_hash_changes_when_provider_decision_hash_changes():
    request = _request(provider_id="custom_provider")
    first_result = AuthorizationProviderResult(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(provider_version="2026-06-10.a"),
    )
    second_result = AuthorizationProviderResult(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(provider_version="2026-06-10.b"),
    )

    first = _issue(request, {"custom_provider": _StaticResultProvider(first_result)})
    second = _issue(request, {"custom_provider": _StaticResultProvider(second_result)})

    assert first.grant is not None
    assert second.grant is not None
    assert first.grant.provider_decision_hash == first_result.decision.decision_hash
    assert second.grant.provider_decision_hash == second_result.decision.decision_hash
    assert first.grant.provider_decision_hash != second.grant.provider_decision_hash
    assert first.grant.grant_hash != second.grant.grant_hash


def test_contract_policy_version_mismatch_is_broker_policy_denied_with_reason():
    decision = issue_object_capability_grant(
        _request(),
        {"fake_verified_object_acl": _fake_provider()},
        now=_NOW,
        policy_version="policy:v2",
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_contract_policy_version_mismatch"


def test_provider_policy_version_mismatch_is_broker_policy_denied_with_reason():
    result = AuthorizationProviderResult(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(policy_version="policy:v2"),
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_provider_policy_version_mismatch"


def test_expired_short_session_request_is_denied_before_grant_issuance():
    decision = _issue(
        _request(
            grant_semantics="short_session",
            expires_at="2026-06-10T00:01:00Z",
        ),
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_broker_policy_expired_grant_request"


def test_short_session_grant_records_expiry_and_session_semantics():
    decision = _issue(
        _request(
            grant_semantics="short_session",
            expires_at="2026-06-10T00:03:00Z",
        ),
    )

    assert decision.failure_class is None
    assert decision.grant is not None
    assert decision.grant.grant_semantics == "short_session"
    assert decision.grant.expires_at == "2026-06-10T00:03:00Z"


def test_malformed_positive_provider_decision_with_revocation_is_denied():
    malformed_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=_decision(revocation_reason="credential_revoked"),
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_provider_decision_inconsistent"


def test_malformed_provider_decision_missing_fields_is_denied_without_attribute_error():
    malformed_decision = AuthorizationProviderDecision.__new__(
        AuthorizationProviderDecision
    )
    malformed_result = _provider_result_invariant_bypass(
        evidence=_evidence(scopes=("doc:read",)),
        decision=malformed_decision,
        failure_class=None,
    )

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": _StaticResultProvider(malformed_result)},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert (
        decision.denial_reason_class
        == "feishu_authorization_provider_decision_malformed"
    )


@pytest.mark.parametrize(
    ("provider_id", "decision_id"),
    [
        ("other_provider", "custom_provider"),
        ("custom_provider", "other_provider"),
    ],
)
def test_provider_identity_mismatch_is_denied_without_grant(provider_id, decision_id):
    provider = _StaticResultProvider(
        AuthorizationProviderResult(
            evidence=_evidence(scopes=("doc:read",)),
            decision=_decision(provider_id=decision_id),
        )
    )
    provider.provider_id = provider_id

    decision = _issue(
        _request(provider_id="custom_provider"),
        {"custom_provider": provider},
    )

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_provider_identity_mismatch"


def test_reusing_one_time_request_id_is_normalized_to_broker_policy_denied():
    first = _issue()
    assert first.grant is not None

    second = _issue(_request(prior_replay_record=first.grant.replay_record))

    assert second.grant is None
    assert second.failure_class == "feishu_broker_policy_denied"
    assert second.denial_reason_class == "feishu_broker_policy_one_time_reuse"


def test_extending_short_session_expiry_after_prior_decision_is_replay_mismatch():
    prior = _issue(
        _request(
            grant_semantics="short_session",
            expires_at="2026-06-10T00:03:00Z",
        )
    )
    assert prior.grant is not None

    replay = _issue(
        _request(
            grant_semantics="short_session",
            expires_at="2026-06-10T00:10:00Z",
            prior_replay_record=prior.grant.replay_record,
        )
    )

    assert replay.grant is None
    assert replay.failure_class == "feishu_broker_policy_denied"
    assert (
        replay.denial_reason_class
        == "feishu_broker_policy_replay_binding_mismatch"
    )


def test_changing_grant_semantics_after_prior_decision_is_replay_mismatch():
    prior = _issue(
        _request(
            grant_semantics="short_session",
            expires_at="2026-06-10T00:03:00Z",
        )
    )
    assert prior.grant is not None

    replay = _issue(_request(prior_replay_record=prior.grant.replay_record))

    assert replay.grant is None
    assert replay.failure_class == "feishu_broker_policy_denied"
    assert (
        replay.denial_reason_class
        == "feishu_broker_policy_replay_binding_mismatch"
    )


@pytest.mark.parametrize(
    "request_overrides",
    [
        {"payload_hash": _OTHER_PAYLOAD_HASH},
        {"object_ref": _OTHER_OBJECT_REF},
        {"action": "write", "requested_scopes": ("doc:write",)},
        {
            "contract": replace(
                _contract(),
                route_session_key_snapshot="route_session_snapshot_hash:sha256:" + "f" * 64,
            )
        },
    ],
)
def test_changing_bound_fields_after_prior_decision_is_broker_policy_denied(
    request_overrides,
):
    prior_record = BrokerPolicyReplayRecord(
        request_id_hash=_REQUEST_ID_HASH,
        payload_hash=_PAYLOAD_HASH,
        provider_decision_hash="sha256:" + "7" * 64,
        route_snapshot_hash=_ROUTE_SNAPSHOT,
        object_ref_hash=_OBJECT_REF.value_hash,
        object_type="doc",
        action="read",
        grant_semantics="one_time",
        grant_hash="sha256:" + "6" * 64,
        expires_at=None,
    )

    decision = _issue(_request(prior_replay_record=prior_record, **request_overrides))

    assert decision.grant is None
    assert decision.failure_class == "feishu_broker_policy_denied"
    assert decision.denial_reason_class == "feishu_broker_policy_replay_binding_mismatch"

import re

import pytest

from gateway.feishu_contracts import AuthorizationEvidence, FeishuContractError, HashedRef
from gateway.feishu_authorization_providers import (
    AuthorizationProviderDecision,
    AuthorizationProviderError,
    AuthorizationProviderProtocol,
    AuthorizationProviderRequest,
    AuthorizationProviderResult,
)


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTRACT_HASH = "sha256:" + "1" * 64
_ROUTE_SNAPSHOT = "route_session_snapshot_hash:sha256:" + "2" * 64
_SUBJECT_REF = HashedRef(kind="feishu_user", value_hash="sha256:" + "b" * 64)
_OBJECT_REF = HashedRef(kind="feishu_doc", value_hash="sha256:" + "a" * 64)


def _provider_contract_request(**overrides) -> AuthorizationProviderRequest:
    values = {
        "contract_hash": _CONTRACT_HASH,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "authority_subject_ref": _SUBJECT_REF,
        "object_ref": _OBJECT_REF,
        "object_type": "doc",
        "action": "read",
        "requested_scopes": ("doc:read",),
        "policy_version": "policy:v1",
        "grant_mode": "one_time",
    }
    values.update(overrides)
    return AuthorizationProviderRequest(**values)


def _provider_decision(**overrides) -> AuthorizationProviderDecision:
    values = {
        "provider_id": "fake_acl_provider",
        "provider_version": "2026-06-10",
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
    }
    values.update(overrides)
    return AuthorizationProviderDecision(**values)


def _evidence() -> AuthorizationEvidence:
    return AuthorizationEvidence(
        evidence_kind="verified_object_acl",
        authority_subject_ref=_SUBJECT_REF,
        route_session_key_snapshot=_ROUTE_SNAPSHOT,
        object_ref=_OBJECT_REF,
        scopes=("doc:read", "doc:write"),
        token_class="user_access_token",
        evidence_state="current",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_hash", ""),
        ("route_session_key_snapshot", ""),
        ("authority_subject_ref", None),
        ("object_ref", None),
        ("object_type", ""),
        ("action", ""),
        ("requested_scopes", ()),
        ("policy_version", ""),
        ("grant_mode", ""),
    ],
)
def test_provider_contract_request_requires_scoped_object_authority_fields(field, value):
    with pytest.raises(AuthorizationProviderError) as exc_info:
        _provider_contract_request(**{field: value})

    assert exc_info.value.failure_class == "invalid_feishu_authorization_provider_request"


def test_provider_contract_request_records_one_time_and_short_session_semantics():
    one_time = _provider_contract_request(grant_mode="one_time", expires_at=None)
    short_session = _provider_contract_request(
        grant_mode="short_session",
        expires_at="2026-06-10T00:05:00Z",
    )

    assert one_time.grant_mode == "one_time"
    assert one_time.expires_at is None
    assert short_session.grant_mode == "short_session"
    assert short_session.expires_at == "2026-06-10T00:05:00Z"
    assert _SHA256_HASH_RE.fullmatch(one_time.request_hash)
    assert one_time.request_hash != short_session.request_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"grant_mode": "one_time", "expires_at": "2026-06-10T00:05:00Z"},
        {"grant_mode": "short_session", "expires_at": None},
    ],
)
def test_provider_contract_request_rejects_ambiguous_grant_lifetimes(overrides):
    with pytest.raises(AuthorizationProviderError) as exc_info:
        _provider_contract_request(**overrides)

    assert exc_info.value.failure_class == "invalid_feishu_authorization_provider_request"


def test_provider_contract_request_rejects_unknown_grant_semantics():
    with pytest.raises(AuthorizationProviderError) as exc_info:
        _provider_contract_request(grant_mode="persistent")

    assert exc_info.value.failure_class == "invalid_feishu_authorization_provider_request"


def test_provider_decision_records_required_metadata_and_sanitized_hash():
    decision = _provider_decision()

    assert decision.provider_id == "fake_acl_provider"
    assert decision.provider_version == "2026-06-10"
    assert decision.evidence_source_class == "verified_object_acl"
    assert decision.reachability_state == "reachable"
    assert decision.issued_at == "2026-06-10T00:00:00Z"
    assert decision.expires_at == "2026-06-10T00:05:00Z"
    assert decision.freshness_class == "current"
    assert decision.credential_freshness == "fresh"
    assert decision.acl_complete is True
    assert decision.unsupported_scope is None
    assert decision.revocation_reason is None
    assert decision.denial_failure_class is None
    assert _SHA256_HASH_RE.fullmatch(decision.decision_hash)


def test_provider_decision_denial_records_stable_failure_classes():
    decision = _provider_decision(
        evidence_source_class="none",
        reachability_state="unreachable",
        issued_at=None,
        expires_at=None,
        freshness_class="unknown",
        credential_freshness="unknown",
        acl_complete=False,
        unsupported_scope="doc:write",
        revocation_reason="credential_revoked",
        denial_failure_class="feishu_provider_revoked_credential",
    )

    assert decision.unsupported_scope == "doc:write"
    assert decision.revocation_reason == "credential_revoked"
    assert decision.denial_failure_class == "feishu_provider_revoked_credential"
    assert _SHA256_HASH_RE.fullmatch(decision.decision_hash)


def test_provider_decision_requires_issued_expires_or_freshness_class():
    with pytest.raises(AuthorizationProviderError) as exc_info:
        _provider_decision(issued_at=None, expires_at=None, freshness_class=None)

    assert exc_info.value.failure_class == "invalid_feishu_authorization_provider_decision"


@pytest.mark.parametrize(
    "overrides",
    [
        {"issued_at": "2026-06-10T00:00:00Z", "expires_at": None, "freshness_class": None},
        {"issued_at": None, "expires_at": "2026-06-10T00:05:00Z", "freshness_class": None},
    ],
)
def test_provider_decision_requires_complete_timestamp_freshness_pair(overrides):
    with pytest.raises(AuthorizationProviderError) as exc_info:
        _provider_decision(**overrides)

    assert exc_info.value.failure_class == "invalid_feishu_authorization_provider_decision"


def test_provider_contract_result_is_evidence_plus_current_decision_metadata():
    result = AuthorizationProviderResult(evidence=_evidence(), decision=_provider_decision())

    assert result.evidence == _evidence()
    assert result.decision.denial_failure_class is None
    assert result.failure_class is None
    assert result.is_denial is False


def test_provider_contract_result_denial_is_stable_not_bare_boolean_or_raw_sdk_response():
    denied_decision = _provider_decision(
        evidence_source_class="none",
        reachability_state="unreachable",
        issued_at=None,
        expires_at=None,
        freshness_class="unknown",
        credential_freshness="unknown",
        acl_complete=False,
        denial_failure_class="feishu_provider_sdk_unreachable",
    )
    denied = AuthorizationProviderResult(
        evidence=None,
        decision=denied_decision,
        failure_class="feishu_provider_sdk_unreachable",
    )

    assert denied.is_denial is True
    assert denied.failure_class == "feishu_provider_sdk_unreachable"
    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(evidence=True, decision=_provider_decision())
    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(evidence={"acl": "raw sdk"}, decision=_provider_decision())
    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(evidence=None, decision=True, failure_class="denied")


def test_provider_contract_protocol_returns_result_not_bool_or_sdk_response():
    class FakeProvider:
        provider_id = "fake_acl_provider"
        provider_version = "2026-06-10"

        def authorize(
            self,
            request: AuthorizationProviderRequest,
        ) -> AuthorizationProviderResult:
            assert request.requested_scopes == ("doc:read",)
            return AuthorizationProviderResult(
                evidence=_evidence(),
                decision=_provider_decision(),
            )

    provider = FakeProvider()

    assert isinstance(provider, AuthorizationProviderProtocol)
    assert isinstance(
        provider.authorize(_provider_contract_request()),
        AuthorizationProviderResult,
    )


def test_evidence_hash_stability_provider_wrappers_do_not_change_v1_fixture_hash():
    evidence = _evidence()
    result = AuthorizationProviderResult(evidence=evidence, decision=_provider_decision())

    assert evidence.evidence_hash == (
        "sha256:981c76a85c232ad18f44816f783db6abbe530daafa175b5a20fc4cc3152377dd"
    )
    assert result.evidence.evidence_hash == evidence.evidence_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_token", "tenant_access_token_secret"),
        ("open_id", "ou_raw_feishu_object_id"),
        ("object_id", "doccn_raw_feishu_object_id"),
        ("object_ref", "doccn_raw_feishu_object_ref"),
        ("acl_response_body", {"code": 0, "data": {"permissions": ["read"]}}),
        ("document_content", "raw document body"),
        ("message_content", "raw message body"),
        ("file_path", "/Users/rtoc/Documents/raw-local-path"),
        ("neutral", "tenant_access_token_secret"),
        ("neutral", "ou_raw_feishu_object_id"),
        ("neutral", {"code": 0, "data": {"permissions": ["read"]}}),
        ("neutral", "raw document body"),
        ("neutral", "raw message body"),
        (
            "neutral",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvdV9yYXcifQ.signature",
        ),
        ("neutral", "rtocopaqueaccesstokenvalue20260610"),
        ("neutral", "verified_object_acl"),
        ("neutral", "Q2PlanningDocBudgetNumbers"),
        ("neutral", "PleaseApproveTheVendorInvoice"),
        ("neutral", "/Users/rtoc/Documents/raw-local-path"),
        ("neutral", r"C:\Users\rtoc\Documents\raw-local-path"),
    ],
)
def test_provider_decision_hash_rejects_raw_sensitive_provider_material(field, value):
    with pytest.raises((AuthorizationProviderError, FeishuContractError)):
        _provider_decision(extra_metadata={field: value})


def test_provider_decision_hash_allows_prehashed_unknown_metadata_and_classifier_values():
    decision = _provider_decision(
        extra_metadata={
            "neutral": "sha256:" + "c" * 64,
            "source_class": "verified_object_acl",
            "failure_class": "feishu_provider_sdk_unreachable",
            "policy_version": "policy:v1",
        }
    )

    assert decision.extra_metadata == {
        "neutral": "sha256:" + "c" * 64,
        "source_class": "verified_object_acl",
        "failure_class": "feishu_provider_sdk_unreachable",
        "policy_version": "policy:v1",
    }
    assert _SHA256_HASH_RE.fullmatch(decision.decision_hash)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "tenant_access_token_secret"),
        ("provider_version", "/Users/rtoc/Documents/provider-version.txt"),
        ("policy_version", "Please approve this object"),
        ("evidence_source_class", "verified object acl from finance doc"),
        ("reachability_state", "call SDK and hope"),
        ("credential_freshness", "fresh enough for Bob"),
        ("unsupported_scope", "/Users/rtoc/Documents/raw-scope.txt"),
        ("revocation_reason", "tenant_access_token_secret"),
        ("denial_failure_class", "just some failure"),
    ],
)
def test_provider_decision_top_level_metadata_rejects_raw_or_ad_hoc_content(field, value):
    with pytest.raises(AuthorizationProviderError):
        _provider_decision(**{field: value})


@pytest.mark.parametrize("failure_class", ["just some failure", "random_failure"])
def test_provider_contract_rejects_arbitrary_failure_classes(failure_class):
    with pytest.raises(AuthorizationProviderError):
        _provider_decision(
            evidence_source_class="none",
            reachability_state="unreachable",
            issued_at=None,
            expires_at=None,
            freshness_class="unknown",
            credential_freshness="unknown",
            acl_complete=False,
            denial_failure_class=failure_class,
        )

    decision = _provider_decision(
        evidence_source_class="none",
        reachability_state="unreachable",
        issued_at=None,
        expires_at=None,
        freshness_class="unknown",
        credential_freshness="unknown",
        acl_complete=False,
        denial_failure_class="feishu_provider_sdk_unreachable",
    )

    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(
            evidence=None,
            decision=decision,
            failure_class=failure_class,
        )


def test_provider_contract_allows_stable_package_c_failure_class():
    decision = _provider_decision(
        evidence_source_class="none",
        reachability_state="unreachable",
        issued_at=None,
        expires_at=None,
        freshness_class="unknown",
        credential_freshness="unknown",
        acl_complete=False,
        denial_failure_class="feishu_provider_sdk_unreachable",
    )

    result = AuthorizationProviderResult(
        evidence=None,
        decision=decision,
        failure_class="feishu_provider_sdk_unreachable",
    )

    assert result.is_denial is True
    assert result.failure_class == "feishu_provider_sdk_unreachable"


@pytest.mark.parametrize(
    "decision",
    [
        _provider_decision(reachability_state="unreachable"),
        _provider_decision(acl_complete=False),
        _provider_decision(
            issued_at=None,
            expires_at=None,
            freshness_class="unknown",
            credential_freshness="unknown",
        ),
        _provider_decision(freshness_class="stale", credential_freshness="stale"),
        _provider_decision(evidence_source_class="admin_policy_grant"),
        _provider_decision(denial_failure_class="feishu_provider_sdk_unreachable"),
    ],
)
def test_provider_contract_positive_result_rejects_unusable_decisions(decision):
    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(evidence=_evidence(), decision=decision)


def test_provider_contract_positive_result_rejects_stale_or_failed_result_metadata():
    stale_evidence = AuthorizationEvidence(
        evidence_kind="verified_object_acl",
        authority_subject_ref=_SUBJECT_REF,
        route_session_key_snapshot=_ROUTE_SNAPSHOT,
        object_ref=_OBJECT_REF,
        scopes=("doc:read", "doc:write"),
        token_class="user_access_token",
        evidence_state="stale",
    )

    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(evidence=stale_evidence, decision=_provider_decision())

    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(
            evidence=_evidence(),
            decision=_provider_decision(),
            failure_class="feishu_provider_sdk_unreachable",
        )

import re

import pytest

import gateway.feishu_authorization_providers as provider_module
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
_OTHER_SUBJECT_REF = HashedRef(kind="feishu_user", value_hash="sha256:" + "c" * 64)
_OTHER_OBJECT_REF = HashedRef(kind="feishu_doc", value_hash="sha256:" + "d" * 64)
_SYSTEM_TEST_OBJECT_REF = HashedRef(
    kind="feishu_system_test_object",
    value_hash="sha256:" + "e" * 64,
)


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


def _fake_provider_request(**overrides) -> AuthorizationProviderRequest:
    return _provider_contract_request(**overrides)


def _fake_provider(
    default_evidence_source_class: str,
    **overrides,
):
    values = {
        "provider_id": f"fake_{default_evidence_source_class}",
        "provider_version": "2026-06-10.fake",
        "evidence_source_class": default_evidence_source_class,
        "authority_subject_ref": _SUBJECT_REF,
        "object_ref": _OBJECT_REF,
        "route_session_key_snapshot": _ROUTE_SNAPSHOT,
        "scopes": ("doc:read",),
    }
    values.update(overrides)
    return provider_module.FakeAuthorizationProvider(**values)


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


@pytest.mark.parametrize(
    "failure_class",
    [
        "feishu_provider_tenant_access_token_secret",
        "feishu_provider_ou_1234567890abcdef",
    ],
)
def test_provider_failure_denial_failure_class_rejects_sensitive_classifier_fragments(
    failure_class,
):
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


@pytest.mark.parametrize(
    "failure_class",
    [
        "feishu_provider_tenant_access_token_secret",
        "feishu_provider_ou_1234567890abcdef",
    ],
)
def test_provider_failure_result_failure_class_rejects_sensitive_classifier_fragments(
    failure_class,
):
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
    object.__setattr__(decision, "denial_failure_class", failure_class)

    with pytest.raises(AuthorizationProviderError):
        AuthorizationProviderResult(
            evidence=None,
            decision=decision,
            failure_class=failure_class,
        )


@pytest.mark.parametrize(
    "failure_class",
    [
        "feishu_provider_tenant_access_token_secret",
        "feishu_provider_ou_1234567890abcdef",
    ],
)
def test_provider_failure_extra_metadata_failure_class_rejects_sensitive_classifier_fragments(
    failure_class,
):
    with pytest.raises(AuthorizationProviderError):
        _provider_decision(extra_metadata={"failure_class": failure_class})


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


def test_fake_provider_user_delegated_credential_requires_exact_authority_snapshot_and_scope():
    provider = _fake_provider("user_delegated_credential")

    result = provider.authorize(_fake_provider_request())

    assert result.is_denial is False
    assert result.evidence.evidence_kind == "user_delegated_credential"
    assert result.evidence.authority_subject_ref == _SUBJECT_REF
    assert result.evidence.object_ref == _OBJECT_REF
    assert result.evidence.route_session_key_snapshot == _ROUTE_SNAPSHOT
    assert result.evidence.scopes == ("doc:read",)
    assert result.evidence.evidence_state == "current"
    assert result.decision.evidence_source_class == "user_delegated_credential"
    assert result.decision.extra_metadata["evidence_class"] == "object_authority"
    assert _SHA256_HASH_RE.fullmatch(result.decision.decision_hash)

    mismatched_requests = [
        _fake_provider_request(authority_subject_ref=_OTHER_SUBJECT_REF),
        _fake_provider_request(object_ref=_OTHER_OBJECT_REF),
        _fake_provider_request(
            route_session_key_snapshot="route_session_snapshot_hash:sha256:" + "9" * 64
        ),
        _fake_provider_request(requested_scopes=("doc:write",)),
    ]

    for request in mismatched_requests:
        denied = provider.authorize(request)
        assert denied.is_denial is True
        assert denied.evidence is None


def test_fake_provider_verified_object_acl_denies_incomplete_acl_with_stable_failure_class():
    provider = _fake_provider("verified_object_acl", acl_complete=False)

    result = provider.authorize(_fake_provider_request())

    assert result.is_denial is True
    assert result.failure_class == "feishu_provider_acl_incomplete"
    assert result.decision.denial_failure_class == "feishu_provider_acl_incomplete"
    assert result.decision.extra_metadata["evidence_class"] == "non_grantable_provider_state"


def test_fake_provider_admin_policy_grant_denies_unsupported_scope_with_stable_failure_class():
    provider = _fake_provider(
        "admin_policy_grant",
        scopes=("doc:read",),
    )

    result = provider.authorize(_fake_provider_request(requested_scopes=("doc:write",)))

    assert result.is_denial is True
    assert result.failure_class == "feishu_provider_unsupported_scope"
    assert result.decision.unsupported_scope == "doc:write"
    assert result.decision.extra_metadata["evidence_class"] == "non_grantable_provider_state"


def test_fake_provider_app_owned_object_grants_only_app_owned_object_evidence():
    provider = _fake_provider("app_owned_object", object_owner_class="app_owned")

    result = provider.authorize(_fake_provider_request())

    assert result.is_denial is False
    assert result.evidence.evidence_kind == "app_owned_object"
    assert result.evidence.token_class == "app_owned_object_credential"
    assert result.decision.extra_metadata["evidence_class"] == "object_authority"

    user_owned_provider = _fake_provider(
        "app_owned_object",
        object_owner_class="user_owned",
    )
    denied = user_owned_provider.authorize(_fake_provider_request())

    assert denied.is_denial is True
    assert denied.failure_class == "feishu_provider_user_owned_object"
    assert denied.decision.extra_metadata["evidence_class"] == "non_grantable_provider_state"


def test_fake_provider_explicit_user_confirmation_is_confirmation_not_object_authority():
    provider = _fake_provider("explicit_user_confirmation")

    result = provider.authorize(_fake_provider_request(action="doc.write"))

    assert result.is_denial is False
    assert result.evidence.evidence_kind == "explicit_user_confirmation"
    assert result.decision.evidence_source_class == "explicit_user_confirmation"
    assert result.decision.extra_metadata["evidence_class"] == "confirmation"
    assert result.decision.extra_metadata["object_authority"] is False


@pytest.mark.parametrize(
    ("provider_id", "object_ref"),
    [
        ("fake_system_test_object", _OBJECT_REF),
        ("fake_object_provider", _SYSTEM_TEST_OBJECT_REF),
    ],
)
def test_fake_provider_cannot_issue_system_test_provider_evidence_for_non_test_authority(
    provider_id,
    object_ref,
):
    provider = _fake_provider(
        "system_test_object",
        provider_id=provider_id,
        object_ref=object_ref,
    )

    result = provider.authorize(_fake_provider_request(object_ref=object_ref))

    assert result.is_denial is True
    assert result.evidence is None
    assert result.failure_class == "feishu_provider_unsupported"


def test_system_test_provider_grants_only_system_test_object_refs_and_test_provider_ids():
    provider = provider_module.SystemTestAuthorizationProvider(
        provider_id="system_test_object_provider",
        provider_version="2026-06-10.test",
        object_ref=_SYSTEM_TEST_OBJECT_REF,
        route_session_key_snapshot=_ROUTE_SNAPSHOT,
        scopes=("doc:read",),
    )

    result = provider.authorize(_fake_provider_request(object_ref=_SYSTEM_TEST_OBJECT_REF))

    assert result.is_denial is False
    assert result.evidence.evidence_kind == "system_test_object"
    assert result.evidence.object_ref == _SYSTEM_TEST_OBJECT_REF
    assert result.evidence.token_class == "system_test_credential"
    assert result.decision.extra_metadata["evidence_class"] == "object_authority"

    non_test_object = provider.authorize(_fake_provider_request())
    assert non_test_object.is_denial is True
    assert non_test_object.failure_class == "feishu_provider_unsupported"

    non_test_provider = provider_module.SystemTestAuthorizationProvider(
        provider_id="fake_object_provider",
        provider_version="2026-06-10.test",
        object_ref=_SYSTEM_TEST_OBJECT_REF,
        route_session_key_snapshot=_ROUTE_SNAPSHOT,
        scopes=("doc:read",),
    )
    denied = non_test_provider.authorize(
        _fake_provider_request(object_ref=_SYSTEM_TEST_OBJECT_REF)
    )
    assert denied.is_denial is True
    assert denied.failure_class == "feishu_provider_unsupported"


@pytest.mark.parametrize(
    ("evidence_source_class", "expected_evidence_class", "expected_failure"),
    [
        ("availability_only", "non_grantable_provider_state", "feishu_provider_non_grantable_state"),
        ("sdk_reachable_only", "non_grantable_provider_state", "feishu_provider_non_grantable_state"),
        ("app_token_only", "app_token_only", None),
        ("discovery_only", "discovery_only", None),
    ],
)
def test_fake_provider_non_object_authority_states_are_explicitly_typed(
    evidence_source_class,
    expected_evidence_class,
    expected_failure,
):
    provider = _fake_provider(evidence_source_class)

    result = provider.authorize(_fake_provider_request())

    assert result.decision.extra_metadata["evidence_class"] == expected_evidence_class
    assert result.decision.extra_metadata["object_authority"] is False
    if expected_failure is None:
        assert result.is_denial is False
        assert result.evidence.evidence_kind == evidence_source_class
    else:
        assert result.is_denial is True
        assert result.failure_class == expected_failure


@pytest.mark.parametrize(
    ("provider_overrides", "expected_failure"),
    [
        ({"provider_available": False}, "feishu_provider_unavailable"),
        ({"sdk_reachable": False}, "feishu_provider_sdk_unreachable"),
        ({"app_token_available": False}, "feishu_provider_app_token_unavailable"),
        ({"credential_freshness": "stale"}, "feishu_provider_stale_credential"),
        ({"credential_freshness": "revoked"}, "feishu_provider_revoked_credential"),
        ({"evidence_source_class": "unsupported_provider"}, "feishu_provider_unsupported"),
    ],
)
def test_provider_failure_states_fail_closed_with_stable_failure_classes(
    provider_overrides,
    expected_failure,
):
    provider = _fake_provider("user_delegated_credential", **provider_overrides)

    result = provider.authorize(_fake_provider_request())

    assert result.is_denial is True
    assert result.failure_class == expected_failure
    assert result.decision.denial_failure_class == expected_failure
    assert result.decision.extra_metadata["evidence_class"] == "non_grantable_provider_state"
    assert _SHA256_HASH_RE.fullmatch(result.decision.decision_hash)

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import gateway.feishu_readiness as readiness
import gateway.feishu_smoke as smoke
from gateway.feishu_authorization_providers import FakeAuthorizationProvider
from gateway.feishu_broker_policy import BrokerPolicyRequest, issue_object_capability_grant
from gateway.feishu_contracts import ConversationContract, HashedRef


HASH_A = "sha256:" + ("a" * 64)
HASH_B = "sha256:" + ("b" * 64)
HASH_C = "sha256:" + ("c" * 64)
HASH_D = "sha256:" + ("d" * 64)
HASH_E = "sha256:" + ("e" * 64)
ROUTE_SNAPSHOT = "route_session_snapshot_hash:sha256:" + ("1" * 64)
NOW = datetime(2026, 6, 10, 0, 1, tzinfo=UTC)

REQUIRED_PROVIDER_CATEGORIES = (
    "user_delegated_credential",
    "verified_object_acl",
    "admin_policy_grant",
    "app_owned_object",
    "system_test_object",
    "explicit_user_confirmation",
)


def _contract() -> ConversationContract:
    return ConversationContract(
        platform_account_id="feishu_app:test",
        tenant_partition_key="tenant:test",
        app_partition_key="app:test",
        conversation_scope_id="cs_test",
        shared_context_scope_id="shared_context:oc_test",
        route_partition_key="route_partition_hash:sha256:" + ("6" * 64),
        route_session_key_snapshot=ROUTE_SNAPSHOT,
        scope_assignment_status="scoped",
        actor_ref=HashedRef(kind="feishu_actor", value_hash=HASH_D),
        authority_subject_ref=_subject_ref(),
        session_id="session:test",
        thread_anchor_ref=HashedRef(kind="feishu_thread", value_hash=HASH_E),
        root_anchor_ref=HashedRef(kind="feishu_message", value_hash=HASH_C),
        identity_evidence_set=(
            HashedRef(kind="message_actor", value_hash="sha256:" + ("f" * 64)),
        ),
        policy_version="policy:v1",
        evidence_state="current",
    )


def _subject_ref() -> HashedRef:
    return HashedRef(kind="feishu_user", value_hash=HASH_B)


def _object_ref(kind: str = "feishu_doc") -> HashedRef:
    return HashedRef(kind=kind, value_hash=HASH_A)


def _provider(source_class: str, *, action: str = "read") -> FakeAuthorizationProvider:
    return FakeAuthorizationProvider(
        provider_id=f"fake_{source_class}",
        provider_version="2026-06-10.fake",
        evidence_source_class=source_class,
        authority_subject_ref=_subject_ref(),
        object_ref=_object_ref(),
        route_session_key_snapshot=ROUTE_SNAPSHOT,
        scopes=(f"doc:{action}",),
    )


def _request(source_class: str, *, action: str = "read") -> BrokerPolicyRequest:
    return BrokerPolicyRequest(
        contract=_contract(),
        provider_id=f"fake_{source_class}",
        object_type="doc",
        object_ref=_object_ref(),
        action=action,
        requested_scopes=(f"doc:{action}",),
        grant_semantics="one_time",
        expires_at=None,
        request_id_hash="sha256:" + ("2" * 64),
        payload_hash="sha256:" + ("3" * 64),
    )


def _broker_decision(source_class: str, *, action: str = "read"):
    return issue_object_capability_grant(
        _request(source_class, action=action),
        {f"fake_{source_class}": _provider(source_class, action=action)},
        now=NOW,
        policy_version="policy:v1",
    )


def _audit_event(source_class: str, **overrides) -> dict:
    event = {
        "type": "feishu_authorization_provider_decision",
        "provider_id": f"fake_{source_class}",
        "provider_version": "2026-06-10.fake",
        "evidence_source_class": source_class,
        "provider_reachability_class": "reachable",
        "credential_freshness_class": "fresh",
        "acl_completeness_class": "complete",
        "unsupported_scope_status": "none",
        "decision_hash": HASH_A,
        "contract_hash": HASH_B,
        "route_snapshot_hash": ROUTE_SNAPSHOT,
        "object_ref_hash": HASH_C,
        "authority_subject_hash": HASH_D,
        "policy_version": "policy:v1",
    }
    event.update(overrides)
    return event


def _ready_evidence(**overrides):
    cls = getattr(readiness, "FeishuPackageCReadinessEvidence")
    values = {
        "provider_registry_present": True,
        "provider_categories": REQUIRED_PROVIDER_CATEGORIES,
        "provider_decision_audit_events": tuple(
            _audit_event(category) for category in REQUIRED_PROVIDER_CATEGORIES
        ),
        "broker_policy_decisions": {
            "object_authority": _broker_decision("verified_object_acl"),
            "app_token_only": _broker_decision("app_token_only"),
            "discovery_only": _broker_decision("discovery_only"),
            "confirmation_only": _broker_decision(
                "explicit_user_confirmation",
                action="write",
            ),
        },
    }
    values.update(overrides)
    return cls(**values)


def test_package_c_readiness_fails_closed_when_provider_registry_missing():
    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(provider_registry_present=False)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_authorization_provider_missing"
    assert result.blockers[0] == "feishu_authorization_provider_missing"


@pytest.mark.parametrize("missing_category", REQUIRED_PROVIDER_CATEGORIES)
def test_package_c_readiness_requires_exact_provider_categories(missing_category):
    categories = tuple(
        category
        for category in REQUIRED_PROVIDER_CATEGORIES
        if category != missing_category
    )
    audit_events = tuple(_audit_event(category) for category in categories)

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(
            provider_categories=categories,
            provider_decision_audit_events=audit_events,
        )
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_authorization_provider_category_missing"
    assert result.blockers == (
        f"feishu_authorization_provider_category_missing:{missing_category}",
    )


@pytest.mark.parametrize(
    ("audit_events", "failure_class"),
    [
        ((), "feishu_authorization_provider_decision_audit_missing"),
        (
            (
                _audit_event(
                    "verified_object_acl",
                    credential_freshness_class="stale",
                ),
            ),
            "feishu_provider_stale_credential",
        ),
        (
            (
                _audit_event(
                    "verified_object_acl",
                    acl_completeness_class="incomplete",
                ),
            ),
            "feishu_provider_acl_incomplete",
        ),
        (
            (
                _audit_event(
                    "verified_object_acl",
                    credential_freshness_class="revoked",
                    revocation_reason_class="credential_revoked",
                ),
            ),
            "feishu_provider_revoked_credential",
        ),
        (
            (
                _audit_event(
                    "verified_object_acl",
                    unsupported_scope_status="unsupported",
                ),
            ),
            "feishu_provider_unsupported_scope",
        ),
        (
            (
                _audit_event(
                    "verified_object_acl",
                    provider_reachability_class="unreachable",
                ),
            ),
            "feishu_provider_sdk_unreachable",
        ),
        (
            (
                _audit_event("app_token_only"),
                _audit_event("discovery_only"),
            ),
            "feishu_object_authority_provider_decision_missing",
        ),
    ],
)
def test_package_c_readiness_rejects_bad_provider_decision_audit(
    audit_events,
    failure_class,
):
    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(provider_decision_audit_events=audit_events)
    )

    assert result.status == "not_ready"
    assert result.failure_class == failure_class
    assert result.blockers[0].startswith(failure_class)


def test_package_c_readiness_requires_decision_audit_for_every_required_provider_category():
    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(
            provider_decision_audit_events=(
                _audit_event("verified_object_acl"),
            )
        )
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_authorization_provider_decision_audit_incomplete"
    assert result.blockers == (
        "feishu_authorization_provider_decision_audit_incomplete:admin_policy_grant",
    )


def test_package_c_readiness_rejects_unknown_provider_decision_audit_source():
    audit_events = tuple(
        _audit_event(category) for category in REQUIRED_PROVIDER_CATEGORIES
    ) + (_audit_event("future_live_provider"),)

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(provider_decision_audit_events=audit_events)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_authorization_provider_decision_audit_unknown"
    assert result.blockers == (
        "feishu_authorization_provider_decision_audit_unknown:future_live_provider",
    )


def test_package_c_broker_policy_foundation_classifies_required_fake_decisions():
    result = readiness.classify_feishu_package_c_readiness(_ready_evidence())

    assert result.status == "ready"
    assert result.failure_class is None
    assert result.blockers == ()


@pytest.mark.parametrize(
    "decision_key",
    ["object_authority", "app_token_only", "discovery_only", "confirmation_only"],
)
def test_package_c_broker_policy_foundation_fails_when_required_decision_missing(
    decision_key,
):
    decisions = dict(_ready_evidence().broker_policy_decisions)
    decisions.pop(decision_key)

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(broker_policy_decisions=decisions)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_broker_policy_foundation_missing"
    assert result.blockers == (
        f"feishu_broker_policy_foundation_missing:{decision_key}",
    )


def test_package_c_broker_policy_rejects_app_discovery_and_confirmation_grants():
    decisions = dict(_ready_evidence().broker_policy_decisions)
    decisions["app_token_only"] = decisions["object_authority"]

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(broker_policy_decisions=decisions)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_broker_policy_denial_missing"
    assert result.blockers == ("feishu_broker_policy_denial_missing:app_token_only",)


def test_package_c_broker_policy_rejects_duck_typed_object_authority_decision():
    decisions = dict(_ready_evidence().broker_policy_decisions)
    decisions["object_authority"] = SimpleNamespace(
        grant=SimpleNamespace(object_capability_grant=object()),
        failure_class=None,
    )

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(broker_policy_decisions=decisions)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_broker_policy_foundation_missing"
    assert result.blockers == (
        "feishu_broker_policy_foundation_missing:object_authority",
    )


def test_package_c_broker_policy_rejects_duck_typed_denial_decision():
    decisions = dict(_ready_evidence().broker_policy_decisions)
    decisions["discovery_only"] = SimpleNamespace(
        grant=None,
        failure_class="feishu_discovery_only_denied",
    )

    result = readiness.classify_feishu_package_c_readiness(
        _ready_evidence(broker_policy_decisions=decisions)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_broker_policy_denial_missing"
    assert result.blockers == ("feishu_broker_policy_denial_missing:discovery_only",)


def test_package_c_smoke_accepts_only_fake_and_system_test_provider_fixtures():
    fixture_cls = getattr(smoke, "PackageCSmokeFixture")

    result = smoke.classify_feishu_package_c_smoke_fixtures(
        (
            fixture_cls(
                name="fake_verified_acl",
                provider_category="verified_object_acl",
                fixture_class="fake_provider",
                live_api_invocations=(),
            ),
            fixture_cls(
                name="system_test_object",
                provider_category="system_test_object",
                fixture_class="system_test_provider",
                live_api_invocations=(),
            ),
        )
    )

    assert result.status == "pass"
    assert result.deploy_pass is True
    assert result.failure_class is None


@pytest.mark.parametrize(
    ("fixture_class", "provider_category"),
    [
        ("fake_provider", "future_live_provider"),
        ("fake_provider", "app_token_only"),
        ("system_test_provider", "verified_object_acl"),
    ],
)
def test_package_c_smoke_fixture_category_must_match_package_c_provider_allowlist(
    fixture_class,
    provider_category,
):
    fixture_cls = getattr(smoke, "PackageCSmokeFixture")

    result = smoke.classify_feishu_package_c_smoke_fixtures(
        (
            fixture_cls(
                name="bad_fixture_category",
                provider_category=provider_category,
                fixture_class=fixture_class,
                live_api_invocations=(),
            ),
        )
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_package_c_smoke_fixture_denied"
    assert result.blockers == (
        "feishu_package_c_smoke_fixture_denied:bad_fixture_category",
    )


@pytest.mark.parametrize(
    "live_identifier",
    [
        "feishu.doc.read",
        "feishu.calendar.event.create",
        "feishu.task.create",
        "feishu.approval.instance.get",
        "feishu.base.record.search",
        "feishu.sheets.values.read",
        "feishu.search.query",
        "feishu.admin.user.lookup",
    ],
)
def test_package_c_smoke_denies_live_business_api_invocations(live_identifier):
    fixture_cls = getattr(smoke, "PackageCSmokeFixture")

    result = smoke.classify_feishu_package_c_smoke_fixtures(
        (
            fixture_cls(
                name="bad_live_fixture",
                provider_category="verified_object_acl",
                fixture_class="fake_provider",
                live_api_invocations=(live_identifier,),
            ),
        )
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_package_c_live_api_invocation_denied"
    assert result.blockers == (
        f"feishu_package_c_live_api_invocation_denied:{live_identifier}",
    )

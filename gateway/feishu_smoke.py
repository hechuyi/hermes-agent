"""Pure Feishu smoke evidence classification primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SmokeEvidence:
    status: str
    observed_at: datetime
    ttl_seconds: int
    commit_hash: str
    config_hash: str
    policy_hash: str
    schema_version: int
    app_partition_key: str
    route_snapshot_hash: str
    ack_supported: bool
    failure_class: str | None = None


@dataclass(frozen=True)
class SmokeIdentity:
    current_commit_hash: str
    current_config_hash: str
    current_policy_hash: str
    schema_version: int
    app_partition_key: str
    route_snapshot_hash: str
    ack_supported: bool


@dataclass(frozen=True)
class SmokeReadiness:
    status: str
    deploy_pass: bool
    failure_class: str | None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageCSmokeFixture:
    name: str
    provider_category: str
    fixture_class: str
    live_api_invocations: tuple[str, ...] = ()


def classify_smoke_evidence(
    evidence: SmokeEvidence | None,
    current_identity: SmokeIdentity,
    *,
    now: datetime,
) -> SmokeReadiness:
    if evidence is None:
        return _fail("smoke_evidence_missing")

    mismatch = _identity_mismatch(evidence, current_identity)
    if mismatch is not None:
        return _fail(mismatch)

    if _is_stale(evidence, now):
        return _fail("smoke_evidence_stale")

    if evidence.status == "acked":
        return SmokeReadiness(status="pass", deploy_pass=True, failure_class=None)

    if evidence.status == "sent_ack_not_supported":
        if not current_identity.ack_supported and not evidence.ack_supported:
            return SmokeReadiness(status="pass", deploy_pass=True, failure_class=None)
        return _fail("feishu_ack_support_mismatch")

    if evidence.status == "sent_ack_pending":
        return _degraded("feishu_ack_pending")

    if evidence.status == "sent_ack_timeout":
        return _degraded("feishu_ack_timeout")

    if evidence.status == "failed":
        return _fail(evidence.failure_class or "feishu_smoke_failed")

    return _fail("feishu_smoke_unknown")


def classify_feishu_package_c_smoke_fixtures(
    fixtures: tuple[PackageCSmokeFixture, ...],
) -> SmokeReadiness:
    if not fixtures:
        return _fail("feishu_package_c_smoke_fixture_missing")

    for fixture in fixtures:
        if not _is_allowed_package_c_fixture_category(fixture):
            return SmokeReadiness(
                status="fail",
                deploy_pass=False,
                failure_class="feishu_package_c_smoke_fixture_denied",
                blockers=(f"feishu_package_c_smoke_fixture_denied:{fixture.name}",),
            )
        for identifier in fixture.live_api_invocations:
            if _is_feishu_live_business_invocation(identifier):
                return SmokeReadiness(
                    status="fail",
                    deploy_pass=False,
                    failure_class="feishu_package_c_live_api_invocation_denied",
                    blockers=(
                        f"feishu_package_c_live_api_invocation_denied:{identifier}",
                    ),
                )

    return SmokeReadiness(status="pass", deploy_pass=True, failure_class=None)


def _is_allowed_package_c_fixture_category(fixture: PackageCSmokeFixture) -> bool:
    try:
        from gateway.feishu_readiness import FEISHU_PACKAGE_C_ALLOWED_PROVIDER_CATEGORIES
    except Exception:
        return False

    if fixture.fixture_class == "fake_provider":
        return fixture.provider_category in FEISHU_PACKAGE_C_ALLOWED_PROVIDER_CATEGORIES
    if fixture.fixture_class == "system_test_provider":
        return fixture.provider_category == "system_test_object"
    return False


def _identity_mismatch(
    evidence: SmokeEvidence,
    current_identity: SmokeIdentity,
) -> str | None:
    checks = (
        (
            evidence.commit_hash,
            current_identity.current_commit_hash,
            "smoke_commit_mismatch",
        ),
        (
            evidence.config_hash,
            current_identity.current_config_hash,
            "smoke_config_mismatch",
        ),
        (
            evidence.policy_hash,
            current_identity.current_policy_hash,
            "smoke_policy_mismatch",
        ),
        (
            evidence.schema_version,
            current_identity.schema_version,
            "smoke_schema_mismatch",
        ),
        (
            evidence.app_partition_key,
            current_identity.app_partition_key,
            "smoke_app_mismatch",
        ),
        (
            evidence.route_snapshot_hash,
            current_identity.route_snapshot_hash,
            "smoke_route_mismatch",
        ),
    )
    for observed, current, failure_class in checks:
        if observed != current:
            return failure_class
    return None


def _is_feishu_live_business_invocation(identifier: str) -> bool:
    try:
        from gateway.feishu_readiness import classify_feishu_package_c_tool_identifier
    except Exception:
        return identifier.startswith("feishu")

    identifier_class = classify_feishu_package_c_tool_identifier(identifier)
    return identifier_class in {"denied", "unknown"}


def _is_stale(evidence: SmokeEvidence, now: datetime) -> bool:
    return evidence.ttl_seconds < 0 or (now - evidence.observed_at).total_seconds() > (
        evidence.ttl_seconds
    )


def _fail(failure_class: str) -> SmokeReadiness:
    return SmokeReadiness(
        status="fail",
        deploy_pass=False,
        failure_class=failure_class,
        blockers=(failure_class,),
    )


def _degraded(failure_class: str) -> SmokeReadiness:
    return SmokeReadiness(
        status="degraded",
        deploy_pass=False,
        failure_class=failure_class,
        warnings=(failure_class,),
    )

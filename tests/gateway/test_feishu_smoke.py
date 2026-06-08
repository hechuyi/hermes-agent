from datetime import datetime, timedelta, timezone

import pytest

from gateway.feishu_smoke import (
    SmokeEvidence,
    SmokeIdentity,
    classify_smoke_evidence,
)


HASH_A = "sha256:" + ("a" * 64)
HASH_B = "sha256:" + ("b" * 64)
HASH_C = "sha256:" + ("c" * 64)
HASH_D = "sha256:" + ("d" * 64)
HASH_E = "sha256:" + ("e" * 64)
HASH_F = "sha256:" + ("f" * 64)
NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _identity(**overrides) -> SmokeIdentity:
    values = {
        "current_commit_hash": HASH_A,
        "current_config_hash": HASH_B,
        "current_policy_hash": HASH_C,
        "schema_version": 1,
        "app_partition_key": "app-partition-hash",
        "route_snapshot_hash": HASH_D,
        "ack_supported": True,
    }
    values.update(overrides)
    return SmokeIdentity(**values)


def _evidence(**overrides) -> SmokeEvidence:
    values = {
        "status": "acked",
        "observed_at": NOW - timedelta(seconds=30),
        "ttl_seconds": 300,
        "commit_hash": HASH_A,
        "config_hash": HASH_B,
        "policy_hash": HASH_C,
        "schema_version": 1,
        "app_partition_key": "app-partition-hash",
        "route_snapshot_hash": HASH_D,
        "ack_supported": True,
        "failure_class": None,
    }
    values.update(overrides)
    return SmokeEvidence(**values)


def test_acked_evidence_passes_deploy_gate():
    result = classify_smoke_evidence(_evidence(status="acked"), _identity(), now=NOW)

    assert result.status == "pass"
    assert result.deploy_pass is True
    assert result.failure_class is None
    assert result.blockers == ()
    assert result.warnings == ()


def test_sent_ack_not_supported_passes_only_when_current_config_disables_ack():
    evidence = _evidence(
        status="sent_ack_not_supported",
        ack_supported=False,
    )

    result = classify_smoke_evidence(
        evidence,
        _identity(ack_supported=False),
        now=NOW,
    )

    assert result.status == "pass"
    assert result.deploy_pass is True
    assert result.failure_class is None


def test_sent_ack_not_supported_fails_when_current_config_requires_ack():
    result = classify_smoke_evidence(
        _evidence(status="sent_ack_not_supported", ack_supported=False),
        _identity(ack_supported=True),
        now=NOW,
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_ack_support_mismatch"
    assert result.blockers == ("feishu_ack_support_mismatch",)


def test_sent_ack_pending_is_degraded_and_not_deploy_pass():
    result = classify_smoke_evidence(
        _evidence(status="sent_ack_pending"),
        _identity(),
        now=NOW,
    )

    assert result.status == "degraded"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_ack_pending"
    assert result.blockers == ()
    assert result.warnings == ("feishu_ack_pending",)


def test_sent_ack_timeout_is_degraded_with_stable_failure_class():
    result = classify_smoke_evidence(
        _evidence(status="sent_ack_timeout"),
        _identity(),
        now=NOW,
    )

    assert result.status == "degraded"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_ack_timeout"
    assert result.warnings == ("feishu_ack_timeout",)


def test_failed_evidence_fails_closed_with_original_failure_class():
    result = classify_smoke_evidence(
        _evidence(status="failed", failure_class="feishu_smoke_send_failed"),
        _identity(),
        now=NOW,
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_smoke_send_failed"
    assert result.blockers == ("feishu_smoke_send_failed",)


def test_unknown_evidence_fails_closed():
    result = classify_smoke_evidence(
        _evidence(status="unknown"),
        _identity(),
        now=NOW,
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "feishu_smoke_unknown"
    assert result.blockers == ("feishu_smoke_unknown",)


def test_stale_ttl_invalidates_otherwise_passing_evidence():
    result = classify_smoke_evidence(
        _evidence(observed_at=NOW - timedelta(seconds=301), ttl_seconds=300),
        _identity(),
        now=NOW,
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == "smoke_evidence_stale"
    assert result.blockers == ("smoke_evidence_stale",)


@pytest.mark.parametrize(
    ("evidence_overrides", "identity_overrides", "failure_class"),
    [
        ({"commit_hash": HASH_E}, {}, "smoke_commit_mismatch"),
        ({"config_hash": HASH_E}, {}, "smoke_config_mismatch"),
        ({"policy_hash": HASH_E}, {}, "smoke_policy_mismatch"),
        ({"schema_version": 2}, {}, "smoke_schema_mismatch"),
        ({"app_partition_key": "other-app-hash"}, {}, "smoke_app_mismatch"),
        ({"route_snapshot_hash": HASH_F}, {}, "smoke_route_mismatch"),
    ],
)
def test_identity_mismatch_invalidates_evidence(
    evidence_overrides,
    identity_overrides,
    failure_class,
):
    result = classify_smoke_evidence(
        _evidence(**evidence_overrides),
        _identity(**identity_overrides),
        now=NOW,
    )

    assert result.status == "fail"
    assert result.deploy_pass is False
    assert result.failure_class == failure_class
    assert result.blockers == (failure_class,)

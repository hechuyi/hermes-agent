import pytest

from gateway.feishu_readiness import (
    FEISHU_PACKAGE_A_LEGACY_SURFACES,
    FeishuReadinessEvidence,
    classify_feishu_package_a_readiness,
    summarize_feishu_audit_readiness,
)


HASH_A = "sha256:" + ("a" * 64)
HASH_B = "sha256:" + ("b" * 64)
EVENT_HASH = "fnv1a64:0123456789abcdef"


def _ready_evidence(**overrides) -> FeishuReadinessEvidence:
    values = {
        "contract_hash": HASH_A,
        "observed_route_snapshot_hash": HASH_A,
        "expected_route_snapshot_hash": HASH_A,
        "object_action_requested": False,
        "capability_granted": False,
        "delivery_state": "known",
        "redaction_failure": False,
        "legacy_denial_reasons": (),
        "checked_legacy_surfaces": FEISHU_PACKAGE_A_LEGACY_SURFACES,
        "denial_audit_available": True,
    }
    values.update(overrides)
    return FeishuReadinessEvidence(**values)


def test_missing_contract_fails_closed_before_other_findings():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(
            contract_hash=None,
            observed_route_snapshot_hash=HASH_B,
            expected_route_snapshot_hash=HASH_A,
        )
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_contract_missing"
    assert result.blockers[0] == "feishu_contract_missing"
    assert "feishu_route_snapshot_mismatch" in result.blockers


def test_route_snapshot_mismatch_is_not_ready():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(
            observed_route_snapshot_hash=HASH_B,
            expected_route_snapshot_hash=HASH_A,
        )
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_route_snapshot_mismatch"
    assert result.blockers == ("feishu_route_snapshot_mismatch",)


def test_object_action_without_capability_grant_is_not_ready():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(object_action_requested=True, capability_granted=False)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_capability_missing"
    assert result.blockers == ("feishu_capability_missing",)


@pytest.mark.parametrize("delivery_state", ["unknown", "unknown_delivery_state"])
def test_unknown_delivery_evidence_is_degraded_when_other_blockers_absent(delivery_state):
    result = classify_feishu_package_a_readiness(
        _ready_evidence(delivery_state=delivery_state)
    )

    assert result.status == "degraded"
    assert result.failure_class == "unknown_delivery_state"
    assert result.blockers == ()
    assert result.warnings == ("unknown_delivery_state",)


def test_redaction_failure_is_not_ready():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(redaction_failure=True)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_redaction_failed"
    assert result.blockers == ("feishu_redaction_failed",)


def test_legacy_tool_bypass_denial_is_degraded_without_hiding_audit_evidence():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(
            legacy_denial_reasons=("feishu_legacy_tool_requires_broker",),
        )
    )

    assert result.status == "degraded"
    assert result.failure_class == "feishu_legacy_tool_requires_broker"
    assert result.warnings == ("feishu_legacy_tool_requires_broker",)


def test_unchecked_legacy_surface_fails_closed_by_omission():
    checked = tuple(
        surface
        for surface in FEISHU_PACKAGE_A_LEGACY_SURFACES
        if surface != "reaction"
    )

    result = classify_feishu_package_a_readiness(
        _ready_evidence(checked_legacy_surfaces=checked)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_legacy_surface_unchecked"
    assert result.blockers == ("feishu_legacy_surface_unchecked:reaction",)


def test_denial_audit_write_failure_is_not_ready():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(denial_audit_available=False)
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_denial_audit_unavailable"
    assert result.blockers == ("feishu_denial_audit_unavailable",)


def test_complete_evidence_is_ready():
    result = classify_feishu_package_a_readiness(
        _ready_evidence(object_action_requested=True, capability_granted=True)
    )

    assert result.status == "ready"
    assert result.failure_class is None
    assert result.blockers == ()
    assert result.warnings == ()


def test_missing_expected_route_snapshot_fails_closed():
    evidence = summarize_feishu_audit_readiness(
        (
            {
                "type": "feishu_contract_observed",
                "timestamp": 1,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "contract_hash": HASH_A,
                "route_snapshot_hash": HASH_A,
                "surface": "comment",
            },
            {
                "type": "feishu_action_requested",
                "timestamp": 2,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "action_hash": HASH_A,
                "action": "reply",
                "surface": "comment",
            },
            {
                "type": "feishu_capability_granted",
                "timestamp": 3,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "capability_hash": HASH_A,
                "capability": "comment",
                "surface": "comment",
            },
        ),
        checked_legacy_surfaces=FEISHU_PACKAGE_A_LEGACY_SURFACES,
    )

    result = classify_feishu_package_a_readiness(evidence)

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_expected_route_snapshot_missing"
    assert result.blockers == ("feishu_expected_route_snapshot_missing",)


def test_audit_summary_does_not_retain_raw_input_event_mappings():
    raw_value = "tenant-access-token"
    evidence = summarize_feishu_audit_readiness(
        (
            {
                "type": "feishu_contract_observed",
                "timestamp": 1,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "contract_hash": HASH_A,
                "route_snapshot_hash": HASH_A,
                "token": raw_value,
                "file_path": "/tmp/raw-file",
                "content": "raw document text",
            },
        ),
        expected_route_snapshot_hash=HASH_A,
        checked_legacy_surfaces=FEISHU_PACKAGE_A_LEGACY_SURFACES,
    )
    result = classify_feishu_package_a_readiness(evidence)

    assert raw_value not in repr(evidence)
    assert "/tmp/raw-file" not in repr(evidence)
    assert "raw document text" not in repr(evidence)
    assert raw_value not in repr(result)


def test_audit_events_summarize_into_readiness_without_business_event_fakes():
    events = (
        {
            "type": "feishu_contract_observed",
            "timestamp": 1,
            "correlation_id": "corr-readiness-1",
            "event_hash": EVENT_HASH,
            "contract_hash": HASH_A,
            "route_snapshot_hash": HASH_A,
            "surface": "feishu.comment",
        },
        {
            "type": "feishu_action_requested",
            "timestamp": 2,
            "correlation_id": "corr-readiness-1",
            "event_hash": EVENT_HASH,
            "action_hash": HASH_A,
            "action": "reply",
            "surface": "comment",
        },
        {
            "type": "feishu_capability_granted",
            "timestamp": 3,
            "correlation_id": "corr-readiness-1",
            "event_hash": EVENT_HASH,
            "capability_hash": HASH_A,
            "capability": "comment",
            "surface": "comment",
        },
        {
            "type": "feishu_legacy_tool_denied",
            "timestamp": 4,
            "correlation_id": "corr-readiness-1",
            "event_hash": EVENT_HASH,
            "legacy_tool_hash": HASH_A,
            "tool": "feishu_doc",
            "failure_class": "feishu_legacy_tool_requires_broker",
            "surface": "doc",
        },
    )

    evidence = summarize_feishu_audit_readiness(
        events,
        expected_route_snapshot_hash=HASH_A,
        checked_legacy_surfaces=FEISHU_PACKAGE_A_LEGACY_SURFACES,
    )
    result = classify_feishu_package_a_readiness(evidence)

    assert evidence.contract_hash == HASH_A
    assert evidence.object_action_requested is True
    assert evidence.capability_granted is True
    assert evidence.denial_audit_available is True
    assert result.status == "degraded"
    assert result.failure_class == "feishu_legacy_tool_requires_broker"


def test_audit_summary_fails_closed_when_known_legacy_surface_is_excluded():
    evidence = summarize_feishu_audit_readiness(
        (
            {
                "type": "feishu_contract_observed",
                "timestamp": 1,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "contract_hash": HASH_A,
                "route_snapshot_hash": HASH_A,
                "surface": "comment",
            },
        ),
        expected_route_snapshot_hash=HASH_A,
        checked_legacy_surfaces=("doc", "drive", "comment"),
    )

    result = classify_feishu_package_a_readiness(evidence)

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_legacy_surface_unchecked"
    assert "feishu_legacy_surface_unchecked:descriptor" in result.blockers


def test_audit_summary_detects_redaction_failure_and_unknown_delivery():
    evidence = summarize_feishu_audit_readiness(
        (
            {
                "type": "unknown_delivery_state",
                "timestamp": 1,
                "delivery_id": "delivery-readiness",
                "failure_class": "unknown_delivery_state",
            },
            {
                "type": "feishu_tool_result_redacted",
                "timestamp": 2,
                "correlation_id": "corr-readiness-1",
                "event_hash": EVENT_HASH,
                "result_hash": HASH_A,
                "tool": "feishu_doc",
                "failure_class": "feishu_redaction_failed",
                "surface": "doc",
            },
        ),
        expected_route_snapshot_hash=HASH_A,
        checked_legacy_surfaces=FEISHU_PACKAGE_A_LEGACY_SURFACES,
    )

    result = classify_feishu_package_a_readiness(evidence)

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_contract_missing"
    assert "feishu_redaction_failed" in result.blockers
    assert "unknown_delivery_state" in result.warnings

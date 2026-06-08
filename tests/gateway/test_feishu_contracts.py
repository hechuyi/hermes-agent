import re
from dataclasses import replace

import pytest

from gateway.feishu_contracts import (
    AuthorizationEvidence,
    ConversationContract,
    FeishuContractError,
    HashedRef,
    ObjectCapabilityGrant,
    canonical_contract_json,
    can_issue_object_grant,
    feishu_contract_hash,
)


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_OBJECT_REF = "sha256:" + "a" * 64
_ACTOR_REF = "sha256:" + "b" * 64
_OTHER_ACTOR_REF = "sha256:" + "c" * 64
_EVIDENCE_REF = "sha256:" + "d" * 64


def test_hash_output_uses_sha256_prefix_and_lowercase_hex():
    digest = feishu_contract_hash(
        {"kind": "message", "revision": 1},
        domain="feishu.contract.test",
        version="v1",
    )

    assert _SHA256_HASH_RE.fullmatch(digest)


def test_hash_canonical_json_sorts_mapping_keys_without_reordering_arrays():
    left = {"z": [3, 2, 1], "a": {"b": 2, "a": 1}}
    right = {"a": {"a": 1, "b": 2}, "z": [3, 2, 1]}
    reordered_array = {"a": {"a": 1, "b": 2}, "z": [1, 2, 3]}

    assert canonical_contract_json(left) == canonical_contract_json(right)
    assert feishu_contract_hash(left, domain="feishu.contract.test", version="v1") == (
        feishu_contract_hash(right, domain="feishu.contract.test", version="v1")
    )
    assert feishu_contract_hash(left, domain="feishu.contract.test", version="v1") != (
        feishu_contract_hash(
            reordered_array,
            domain="feishu.contract.test",
            version="v1",
        )
    )


def test_hash_does_not_confuse_missing_and_explicit_null():
    missing = {"object": {"id_hash": "sha256:" + "0" * 64}}
    explicit_null = {
        "object": {
            "id_hash": "sha256:" + "0" * 64,
            "tenant_hash": None,
        }
    }

    assert canonical_contract_json(missing) != canonical_contract_json(explicit_null)
    assert feishu_contract_hash(missing, domain="feishu.contract.test", version="v1") != (
        feishu_contract_hash(explicit_null, domain="feishu.contract.test", version="v1")
    )


def test_hash_normalizes_strings_to_unicode_nfc():
    composed = {"title": "Caf\u00e9"}
    decomposed = {"title": "Cafe\u0301"}

    assert canonical_contract_json(composed) == canonical_contract_json(decomposed)
    assert feishu_contract_hash(composed, domain="feishu.contract.test", version="v1") == (
        feishu_contract_hash(decomposed, domain="feishu.contract.test", version="v1")
    )


def test_hash_domain_version_schema_version_and_algorithm_separate_payloads():
    payload = {"object": {"id_hash": "sha256:" + "1" * 64}}
    baseline = feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v1",
        schema_version=1,
        algorithm="sha256",
    )

    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.other",
        version="v1",
        schema_version=1,
        algorithm="sha256",
    )
    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v2",
        schema_version=1,
        algorithm="sha256",
    )
    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v1",
        schema_version=2,
        algorithm="sha256",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_access_token": "raw"},
        {"nested": {"clientSecret": "raw"}},
        {"nested": {"private-key": "raw"}},
        {"nested": {"rawMessage": "raw"}},
        {"nested": {"documentContent": "raw"}},
        {"nested": {"file.path": "/tmp/raw"}},
        {"nested": {"openId": "ou_raw"}},
        {"nested": {"user-id": "u_raw"}},
        {"nested": {"union_id": "on_raw"}},
    ],
)
def test_hash_rejects_sensitive_raw_fields(payload):
    with pytest.raises(FeishuContractError, match="sensitive raw field"):
        feishu_contract_hash(
            payload,
            domain="feishu.contract.test",
            version="v1",
        )


def test_hash_allows_hash_suffixed_sensitive_references_and_token_class_metadata():
    payload = {
        "open_id_hash": "sha256:" + "2" * 64,
        "authorization": {"token_class": "user_access_token"},
        "file_path_hash": "sha256:" + "3" * 64,
    }

    assert _SHA256_HASH_RE.fullmatch(
        feishu_contract_hash(payload, domain="feishu.contract.test", version="v1")
    )


def _ref(kind: str = "feishu_user", digest: str = _ACTOR_REF) -> HashedRef:
    return HashedRef(kind=kind, value_hash=digest)


def _object_ref() -> HashedRef:
    return HashedRef(kind="feishu_doc", value_hash=_OBJECT_REF)


def _contract(**overrides) -> ConversationContract:
    values = {
        "platform_account_id": "feishu_app:test",
        "conversation_scope_id": "cs_test",
        "route_partition_key": "agent:main:feishu:group:oc_test",
        "route_session_key_snapshot": "agent:main:feishu:group:oc_test:u_test",
        "scope_assignment_status": "scoped",
        "authority_subject_ref": _ref(),
        "identity_evidence_set": (
            HashedRef(kind="message_actor", value_hash=_EVIDENCE_REF),
        ),
    }
    values.update(overrides)
    return ConversationContract(**values)


def _evidence(**overrides) -> AuthorizationEvidence:
    values = {
        "evidence_kind": "object_acl",
        "authority_subject_ref": _ref(),
        "route_session_key_snapshot": "agent:main:feishu:group:oc_test:u_test",
        "object_ref": _object_ref(),
        "scopes": ("doc:read", "doc:write"),
        "token_class": "user_access_token",
    }
    values.update(overrides)
    return AuthorizationEvidence(**values)


def test_evidence_contract_dataclasses_produce_stable_hashes():
    subject = _ref()
    contract = _contract(authority_subject_ref=subject)
    evidence = _evidence(authority_subject_ref=subject)
    grant = ObjectCapabilityGrant(
        contract_hash=contract.contract_hash,
        evidence_hashes=(evidence.evidence_hash,),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
        authority_subject_ref=subject,
    )

    assert _SHA256_HASH_RE.fullmatch(contract.contract_hash)
    assert _SHA256_HASH_RE.fullmatch(evidence.evidence_hash)
    assert _SHA256_HASH_RE.fullmatch(grant.grant_hash)
    assert contract == _contract(authority_subject_ref=subject)
    assert evidence == _evidence(authority_subject_ref=subject)


def test_grant_decision_allows_scoped_user_object_authority_evidence():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (True, None)


def test_scope_assignment_status_other_than_scoped_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(scope_assignment_status="legacy_unscoped"),
        _evidence(),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_scope_not_scoped")


def test_grant_route_snapshot_mismatch_denies():
    allowed, failure_class = can_issue_object_grant(
        _contract(route_session_key_snapshot="agent:main:feishu:group:oc_test:u_test"),
        _evidence(route_session_key_snapshot="agent:main:feishu:group:oc_test:u_other"),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_route_snapshot_mismatch")


def test_evidence_app_token_only_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(
            evidence_kind="app_token",
            token_class="app_access_token",
            authority_subject_ref=None,
        ),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_app_token_only_evidence")


def test_evidence_discovery_only_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_kind="discovery", scopes=("doc:metadata",)),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_discovery_only_evidence")


def test_grant_explicit_confirmation_alone_denies_p3_object_actions():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_kind="explicit_confirmation", scopes=()),
        object_type="doc",
        action="delete",
    )

    assert (
        allowed,
        failure_class,
    ) == (False, "feishu_p3_requires_object_authority_evidence")


def test_scope_shared_context_does_not_imply_shared_authority_subject():
    shared_context_contract = replace(
        _contract(),
        authority_subject_ref=None,
        identity_evidence_set=(
            HashedRef(kind="conversation_scope", value_hash="sha256:" + "e" * 64),
        ),
    )

    allowed, failure_class = can_issue_object_grant(
        shared_context_contract,
        _evidence(authority_subject_ref=_ref(digest=_OTHER_ACTOR_REF)),
        object_type="doc",
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_authority_subject_missing")

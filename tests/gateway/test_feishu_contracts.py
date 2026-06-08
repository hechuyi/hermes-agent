import re
from dataclasses import replace

import pytest

from gateway.feishu_contracts import (
    AuthorizationEvidence,
    ConversationContract,
    FeishuContractError,
    HashedRef,
    ObjectCapabilityGrant,
    build_feishu_conversation_contract,
    canonical_contract_json,
    can_issue_object_grant,
    feishu_contract_hash,
    feishu_hashed_ref,
)
from gateway.config import Platform
from gateway.conversation_scope import conversation_identity, route_partition_key
from gateway.session import SessionSource, build_session_key


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_OBJECT_REF = "sha256:" + "a" * 64
_OTHER_OBJECT_REF = "sha256:" + "9" * 64
_ACTOR_REF = "sha256:" + "b" * 64
_OTHER_ACTOR_REF = "sha256:" + "c" * 64
_EVIDENCE_REF = "sha256:" + "d" * 64
_FEISHU_SOURCE_APP_ID = "feishu-source-app-fixture"
_FEISHU_SOURCE_CHAT_ID = "feishu-source-chat-fixture"
_FEISHU_SOURCE_OPEN_ID = "feishu-source-open-fixture"
_FEISHU_SOURCE_UNION_ID = "feishu-source-union-fixture"
_FEISHU_SOURCE_MESSAGE_ID = "feishu-source-message-fixture"


def _fake_feishu_source(**overrides) -> SessionSource:
    values = {
        "platform": Platform.FEISHU,
        "chat_id": _FEISHU_SOURCE_CHAT_ID,
        "chat_type": "group",
        "user_id": _FEISHU_SOURCE_OPEN_ID,
        "user_id_alt": _FEISHU_SOURCE_UNION_ID,
        "message_id": _FEISHU_SOURCE_MESSAGE_ID,
    }
    values.update(overrides)
    return SessionSource(**values)


def _inbound_contract(**overrides) -> ConversationContract:
    source = overrides.pop("source", _fake_feishu_source())
    platform_account_id = overrides.pop("platform_account_id", "feishu_app:test")
    scope_identity = overrides.pop(
        "scope_identity",
        conversation_identity(
            source,
            platform_account_id=platform_account_id,
        ),
    )
    assert scope_identity is not None
    values = {
        "scope_identity": scope_identity,
        "route_partition_key": route_partition_key(source),
        "route_session_key_snapshot": build_session_key(source),
        "scope_assignment_status": "scoped",
        "actor_ref": feishu_hashed_ref("feishu_actor", source.user_id_alt),
        "authority_subject_ref": feishu_hashed_ref("feishu_user", source.user_id_alt),
        "identity_evidence_set": (
            feishu_hashed_ref("feishu_message_actor", source.user_id),
            feishu_hashed_ref("feishu_message_union", source.user_id_alt),
        ),
        "session_id": "session:test",
        "tenant_partition_key": "tenant:test",
        "app_partition_key": "app:test",
        "thread_anchor_ref": feishu_hashed_ref("feishu_message", source.message_id),
    }
    values.update(overrides)
    return build_feishu_conversation_contract(**values)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"open_id_hash": "ou_raw"},
        {"user_id_hash": "user_raw"},
        {"union_id_hash": "union_raw"},
        {"file_path_hash": "/tmp/raw"},
        {"document_content_hash": "plain document text"},
    ],
)
def test_hash_suffixed_sensitive_references_must_be_sha256_hashes(payload):
    with pytest.raises(FeishuContractError) as exc_info:
        feishu_contract_hash(payload, domain="feishu.contract.test", version="v1")

    assert exc_info.value.failure_class == "invalid_hashed_sensitive_ref"


@pytest.mark.parametrize("token_class", ["tenant_token_but_raw", None, 1])
def test_hash_rejects_unknown_token_class_metadata(token_class):
    with pytest.raises(FeishuContractError) as exc_info:
        feishu_contract_hash(
            {"authorization": {"token_class": token_class}},
            domain="feishu.contract.test",
            version="v1",
        )

    assert exc_info.value.failure_class == "invalid_feishu_token_class"


@pytest.mark.parametrize("schema_version", [0, -1, True, False])
def test_hash_requires_positive_schema_version(schema_version):
    with pytest.raises(FeishuContractError, match="schema_version"):
        feishu_contract_hash(
            {"kind": "message"},
            domain="feishu.contract.test",
            version="v1",
            schema_version=schema_version,
        )


def _ref(kind: str = "feishu_user", digest: str = _ACTOR_REF) -> HashedRef:
    return HashedRef(kind=kind, value_hash=digest)


def _object_ref() -> HashedRef:
    return HashedRef(kind="feishu_doc", value_hash=_OBJECT_REF)


def _other_object_ref() -> HashedRef:
    return HashedRef(kind="feishu_doc", value_hash=_OTHER_OBJECT_REF)


def _contract(**overrides) -> ConversationContract:
    values = {
        "platform_account_id": "feishu_app:test",
        "tenant_partition_key": "tenant:test",
        "app_partition_key": "app:test",
        "conversation_scope_id": "cs_test",
        "shared_context_scope_id": "shared_context:oc_test",
        "route_partition_key": "agent:main:feishu:group:oc_test",
        "route_session_key_snapshot": "agent:main:feishu:group:oc_test:u_test",
        "scope_assignment_status": "scoped",
        "actor_ref": _ref(kind="feishu_actor"),
        "authority_subject_ref": _ref(),
        "session_id": "session:test",
        "thread_anchor_ref": HashedRef(kind="feishu_thread", value_hash="sha256:" + "4" * 64),
        "root_anchor_ref": HashedRef(kind="feishu_message", value_hash="sha256:" + "5" * 64),
        "identity_evidence_set": (
            HashedRef(kind="message_actor", value_hash=_EVIDENCE_REF),
        ),
        "policy_version": "policy:v1",
        "evidence_state": "current",
    }
    values.update(overrides)
    return ConversationContract(**values)


def _evidence(**overrides) -> AuthorizationEvidence:
    values = {
        "evidence_kind": "verified_object_acl",
        "authority_subject_ref": _ref(),
        "route_session_key_snapshot": "agent:main:feishu:group:oc_test:u_test",
        "object_ref": _object_ref(),
        "scopes": ("doc:read", "doc:write"),
        "token_class": "user_access_token",
        "evidence_state": "current",
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


@pytest.mark.parametrize("token_class", ["tenant_token_but_raw", None, 1])
def test_authorization_evidence_rejects_unknown_token_class(token_class):
    with pytest.raises(FeishuContractError) as exc_info:
        _evidence(token_class=token_class)

    assert exc_info.value.failure_class == "invalid_feishu_token_class"


def test_grant_decision_allows_scoped_user_object_authority_evidence():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (True, None)


def test_object_mismatch_denies_cross_object_authority_evidence():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(object_ref=_object_ref()),
        object_type="doc",
        object_ref=_other_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_object_ref_mismatch")


def test_full_contract_hash_changes_for_design_partition_and_anchor_dimensions():
    base = _contract()
    variants = [
        _contract(tenant_partition_key="tenant:other"),
        _contract(app_partition_key="app:other"),
        _contract(shared_context_scope_id="shared_context:other"),
        _contract(actor_ref=_ref(kind="feishu_actor", digest=_OTHER_ACTOR_REF)),
        _contract(session_id="session:other"),
        _contract(
            thread_anchor_ref=HashedRef(
                kind="feishu_thread",
                value_hash="sha256:" + "6" * 64,
            ),
        ),
        _contract(
            root_anchor_ref=HashedRef(
                kind="feishu_message",
                value_hash="sha256:" + "7" * 64,
            ),
        ),
        _contract(policy_version="policy:v2"),
        _contract(evidence_state="stale"),
        _contract(contract_hash_domain="feishu.conversation_contract.other"),
        _contract(contract_hash_version="v2"),
    ]

    hashes = {base.contract_hash, *(variant.contract_hash for variant in variants)}

    assert len(hashes) == len(variants) + 1


@pytest.mark.parametrize(
    "evidence_kind",
    [
        "verified_object_acl",
        "user_delegated_credential",
        "admin_policy_grant",
        "app_owned_object",
        "system_test_object",
    ],
)
def test_design_evidence_object_authority_kinds_allow_grant(evidence_kind):
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_kind=evidence_kind),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (True, None)


def test_design_evidence_rejects_unknown_kind():
    with pytest.raises(FeishuContractError, match="evidence_kind"):
        _evidence(evidence_kind="object_acl")


def test_scope_assignment_status_other_than_scoped_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(scope_assignment_status="legacy_unscoped"),
        _evidence(),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_scope_not_scoped")


def test_grant_route_snapshot_mismatch_denies():
    allowed, failure_class = can_issue_object_grant(
        _contract(route_session_key_snapshot="agent:main:feishu:group:oc_test:u_test"),
        _evidence(route_session_key_snapshot="agent:main:feishu:group:oc_test:u_other"),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_route_snapshot_mismatch")


@pytest.mark.parametrize(
    ("evidence_state", "failure_class"),
    [
        ("stale", "feishu_contract_evidence_stale"),
        ("revoked", "feishu_contract_evidence_revoked"),
    ],
)
def test_contract_evidence_state_denies_grant(evidence_state, failure_class):
    allowed, observed = can_issue_object_grant(
        _contract(evidence_state=evidence_state),
        _evidence(),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, observed) == (False, failure_class)


def test_evidence_app_token_only_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(
            evidence_kind="app_token_only",
            token_class="app_access_token",
            authority_subject_ref=None,
        ),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_app_token_only_evidence")


def test_evidence_discovery_only_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_kind="discovery_only", scopes=("doc:metadata",)),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_discovery_only_evidence")


def test_grant_explicit_confirmation_alone_denies_p3_object_actions():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_kind="explicit_user_confirmation", scopes=()),
        object_type="doc",
        object_ref=_object_ref(),
        action="delete",
    )

    assert (
        allowed,
        failure_class,
    ) == (False, "feishu_p3_requires_object_authority_evidence")


def test_scope_shared_context_does_not_imply_shared_authority_subject():
    shared_context_contract = replace(
        _contract(),
        shared_context_scope_id="shared_context:oc_test",
        authority_subject_ref=None,
    )

    allowed, failure_class = can_issue_object_grant(
        shared_context_contract,
        _evidence(authority_subject_ref=_ref(digest=_OTHER_ACTOR_REF)),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_authority_subject_missing")


def test_shared_context_does_not_allow_reusing_another_actor_authority():
    shared_context_contract = replace(
        _contract(),
        shared_context_scope_id="shared_context:oc_test",
        actor_ref=_ref(kind="feishu_actor", digest=_ACTOR_REF),
        authority_subject_ref=_ref(kind="feishu_user", digest=_ACTOR_REF),
    )
    other_actor_evidence = _evidence(
        authority_subject_ref=_ref(kind="feishu_user", digest=_OTHER_ACTOR_REF),
    )

    allowed, failure_class = can_issue_object_grant(
        shared_context_contract,
        other_actor_evidence,
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_authority_subject_mismatch")


def test_stale_authorization_evidence_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_state="stale"),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_authorization_evidence_stale")


def test_revoked_authorization_evidence_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(evidence_state="revoked"),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_authorization_evidence_revoked")


def test_insufficient_scope_object_authority_denies_grant():
    allowed, failure_class = can_issue_object_grant(
        _contract(),
        _evidence(scopes=("doc:read",)),
        object_type="doc",
        object_ref=_object_ref(),
        action="write",
    )

    assert (allowed, failure_class) == (
        False,
        "feishu_object_authority_scope_insufficient",
    )


def test_populate_inbound_contract_includes_scope_route_subject_evidence_and_hash():
    source = _fake_feishu_source()
    scope_identity = conversation_identity(source, platform_account_id="feishu_app:test")
    assert scope_identity is not None

    contract = _inbound_contract(source=source, scope_identity=scope_identity)

    assert contract.platform_account_id == "feishu_app:test"
    assert contract.conversation_scope_id == scope_identity.id
    assert contract.route_partition_key == route_partition_key(source)
    assert contract.route_session_key_snapshot == build_session_key(source)
    assert contract.scope_assignment_status == "scoped"
    assert contract.authority_subject_ref == feishu_hashed_ref(
        "feishu_user",
        _FEISHU_SOURCE_UNION_ID,
    )
    assert contract.identity_evidence_set == (
        feishu_hashed_ref("feishu_message_actor", _FEISHU_SOURCE_OPEN_ID),
        feishu_hashed_ref("feishu_message_union", _FEISHU_SOURCE_UNION_ID),
    )
    assert contract.shared_context_scope_id == f"shared_context:{scope_identity.id}"
    assert _SHA256_HASH_RE.fullmatch(contract.contract_hash)


def test_populate_inbound_contract_hash_changes_when_route_snapshot_changes():
    base = _inbound_contract(
        route_session_key_snapshot="agent:main:feishu:group:fixture-chat:a"
    )
    changed = _inbound_contract(
        route_session_key_snapshot="agent:main:feishu:group:fixture-chat:b"
    )

    assert base.contract_hash != changed.contract_hash


def test_populate_inbound_contract_hash_changes_when_conversation_scope_changes():
    first_source = _fake_feishu_source(chat_id="feishu-source-chat-one")
    second_source = _fake_feishu_source(chat_id="feishu-source-chat-two")

    first = _inbound_contract(source=first_source)
    second = _inbound_contract(source=second_source)

    assert first.conversation_scope_id != second.conversation_scope_id
    assert first.contract_hash != second.contract_hash


def test_populate_inbound_contract_uses_hashed_identity_evidence_not_raw_feishu_ids():
    contract = _inbound_contract()
    identity_payload_json = canonical_contract_json(
        {
            "actor_ref": contract.actor_ref,
            "authority_subject_ref": contract.authority_subject_ref,
            "identity_evidence_set": contract.identity_evidence_set,
            "thread_anchor_ref": contract.thread_anchor_ref,
            "root_anchor_ref": contract.root_anchor_ref,
        }
    )

    assert _FEISHU_SOURCE_APP_ID not in identity_payload_json
    assert _FEISHU_SOURCE_CHAT_ID not in identity_payload_json
    assert _FEISHU_SOURCE_OPEN_ID not in identity_payload_json
    assert _FEISHU_SOURCE_UNION_ID not in identity_payload_json
    assert _FEISHU_SOURCE_MESSAGE_ID not in identity_payload_json
    assert "sha256:" in identity_payload_json


@pytest.mark.parametrize(
    "scope_assignment_status",
    [
        "ambiguous",
        "legacy_unscoped",
        "detached",
        "backfilled",
        "resume_pending",
        "cli_handoff",
        "implicit_switch",
    ],
)
def test_legacy_state_contracts_are_not_authorizable(scope_assignment_status):
    contract = _inbound_contract(scope_assignment_status=scope_assignment_status)

    allowed, failure_class = can_issue_object_grant(
        contract,
        _evidence(route_session_key_snapshot=contract.route_session_key_snapshot),
        object_type="doc",
        object_ref=_object_ref(),
        action="read",
    )

    assert (allowed, failure_class) == (False, "feishu_scope_not_scoped")


@pytest.mark.parametrize(
    "overrides",
    [
        {"authority_subject_ref": None},
        {"identity_evidence_set": ()},
    ],
)
def test_populate_inbound_contract_requires_scoped_identity_for_authorizable_subject(
    overrides,
):
    with pytest.raises(FeishuContractError) as exc_info:
        _inbound_contract(**overrides)

    assert exc_info.value.failure_class == "feishu_scoped_identity_incomplete"

import re

import pytest

from gateway.feishu_action_plan import (
    FeishuActionContract,
    RenderPlan,
    RenderPlanPart,
    validate_action_contract,
    validate_render_plan,
    validate_render_part,
)
from gateway.feishu_contracts import FeishuContractError


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTENT_HASH = "sha256:" + "1" * 64
_PAYLOAD_HASH = "sha256:" + "2" * 64
_PROVENANCE_HASH = "sha256:" + "3" * 64
_ROUTE_HASH = "sha256:" + "4" * 64
_ACTION_DIGEST = "sha256:" + "5" * 64
_PLAN_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def _part(part_type: str, **overrides) -> RenderPlanPart:
    values = {
        "part_type": part_type,
        "content_hash": _CONTENT_HASH,
        "payload_hash": _PAYLOAD_HASH,
        "fallback_action": "preserve",
    }
    values.update(overrides)
    return RenderPlanPart(**values)


def _action(**overrides) -> FeishuActionContract:
    values = {
        "action_kind": "button",
        "action_digest": _ACTION_DIGEST,
        "same_operator_scope": True,
        "expires_at": "2026-06-08T10:30:00Z",
        "route_snapshot_hash": _ROUTE_HASH,
        "payload_hash": _PAYLOAD_HASH,
    }
    values.update(overrides)
    return FeishuActionContract(**values)


def _assert_raises_failure(factory, failure_class: str) -> None:
    with pytest.raises(FeishuContractError) as exc_info:
        factory()

    assert exc_info.value.failure_class == failure_class


def test_plan_hash_is_stable_for_supported_render_part_snapshots():
    parts = (
        _part("post", metadata={"format": "feishu_post"}),
        _part("markdown", metadata={"format": "commonmark"}),
        _part("plain_text", metadata={"format": "text"}),
        _part("table", metadata={"columns_hash": "sha256:" + "6" * 64}),
        _part("code_block", metadata={"language": "python"}),
        _part("link", metadata={"href_hash": "sha256:" + "7" * 64}),
        _part(
            "plain_text",
            chunk_group="sha256:" + "8" * 64,
            chunk_index=0,
            chunk_count=2,
            metadata={"chunk_role": "first"},
        ),
        _part(
            "plain_text",
            chunk_group="sha256:" + "8" * 64,
            chunk_index=1,
            chunk_count=2,
            metadata={"chunk_role": "second"},
        ),
        _part(
            "image",
            source_class="generated",
            provenance_hash=_PROVENANCE_HASH,
            fallback_action="replace",
        ),
        _part(
            "file",
            source_class="upload",
            provenance_hash="sha256:" + "9" * 64,
            fallback_action="drop",
        ),
        _part(
            "card",
            action_contract=_action(action_kind="card"),
            fallback_action="replace",
        ),
        _part("button", action_contract=_action(), fallback_action="drop"),
    )

    plan = RenderPlan(
        target_ref_hash="sha256:" + "a" * 64,
        object_ref_hash="sha256:" + "b" * 64,
        render_mode="offline_snapshot",
        parts=parts,
    )
    equivalent = RenderPlan(
        target_ref_hash="sha256:" + "a" * 64,
        object_ref_hash="sha256:" + "b" * 64,
        render_mode="offline_snapshot",
        parts=parts,
    )

    assert _PLAN_HASH_RE.fullmatch(plan.plan_hash)
    assert plan.plan_hash == equivalent.plan_hash
    assert plan.schema_version == 1
    assert validate_render_plan(plan) == (True, None)


@pytest.mark.parametrize(
    ("metadata", "failure_class"),
    [
        ({"method": "POST"}, "feishu_action_raw_tool_material"),
        ({"open_id": "ou_raw"}, "feishu_action_sensitive_metadata"),
    ],
)
def test_action_contract_rejects_invalid_metadata_before_hashing(
    metadata,
    failure_class,
):
    with pytest.raises(FeishuContractError) as exc_info:
        FeishuActionContract(
            action_kind="button",
            action_digest=_ACTION_DIGEST,
            same_operator_scope=True,
            expires_at="2026-06-08T10:30:00Z",
            route_snapshot_hash=_ROUTE_HASH,
            payload_hash=_PAYLOAD_HASH,
            metadata=metadata,
        )

    assert exc_info.value.failure_class == failure_class


def test_render_part_rejects_raw_content_hash_before_hashing():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="plain_text",
            content_hash="raw content",
            payload_hash=_PAYLOAD_HASH,
            fallback_action="preserve",
        )

    assert exc_info.value.failure_class == "feishu_render_content_hash_invalid"


def test_button_part_rejects_missing_action_contract_before_hashing():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="button",
            content_hash=_CONTENT_HASH,
            payload_hash=_PAYLOAD_HASH,
            fallback_action="drop",
            action_contract=None,
        )

    assert exc_info.value.failure_class == "feishu_action_contract_missing"


def test_attachment_part_rejects_raw_source_class_before_hashing():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="image",
            content_hash=_CONTENT_HASH,
            payload_hash=_PAYLOAD_HASH,
            source_class="/tmp/raw",
            provenance_hash=_PROVENANCE_HASH,
            fallback_action="replace",
        )

    assert exc_info.value.failure_class == "feishu_render_attachment_source_invalid"


def test_render_plan_rejects_nested_invalid_part_before_plan_hashing():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlan(
            target_ref_hash="sha256:" + "a" * 64,
            object_ref_hash="sha256:" + "b" * 64,
            render_mode="offline_snapshot",
            parts=(
                RenderPlanPart(
                    part_type="plain_text",
                    content_hash="raw content",
                    payload_hash=_PAYLOAD_HASH,
                    fallback_action="preserve",
                ),
            ),
        )

    assert exc_info.value.failure_class == "feishu_render_content_hash_invalid"


@pytest.mark.parametrize("part_type", ["card", "button"])
def test_card_and_button_parts_require_opaque_action_contract(part_type):
    _assert_raises_failure(
        lambda: _part(part_type, action_contract=None),
        "feishu_action_contract_missing",
    )


@pytest.mark.parametrize(
    ("overrides", "failure_class"),
    [
        ({"action_digest": None}, "feishu_action_digest_missing"),
        ({"action_digest": "open_form:123"}, "feishu_action_digest_invalid"),
        ({"same_operator_scope": False}, "feishu_action_scope_not_same_operator"),
        ({"expires_at": None}, "feishu_action_expiry_missing"),
        ({"route_snapshot_hash": None}, "feishu_action_route_missing"),
        ({"payload_hash": None}, "feishu_action_payload_hash_missing"),
    ],
)
def test_action_contract_requires_digest_scope_expiry_route_and_payload_hash(
    overrides,
    failure_class,
):
    _assert_raises_failure(lambda: _action(**overrides), failure_class)


@pytest.mark.parametrize(
    "metadata",
    [
        {"tool_args": {"document_token": "doc_raw"}},
        {"tool_method": "POST"},
        {"method": "GET"},
        {"path": "/open-apis/im/v1/messages"},
        {"body": {"content": "raw"}},
        {"raw_tool_arguments": {"path": "/raw"}},
    ],
)
def test_raw_tool_material_is_rejected_from_parts_and_actions(metadata):
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_raw_tool_material",
    )
    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_raw_tool_material",
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"document_token": "doc_raw"},
        {"open_id": "ou_raw"},
        {"file_path": "/tmp/raw-file"},
        {"document_content": "raw document text"},
    ],
)
def test_render_part_rejects_sensitive_metadata_keys(metadata):
    with pytest.raises(FeishuContractError) as exc_info:
        _part("post", metadata=metadata)

    assert exc_info.value.failure_class == "feishu_action_sensitive_metadata"


@pytest.mark.parametrize(
    "metadata",
    [
        {"document_token": "doc_raw"},
        {"open_id": "ou_raw"},
        {"file_path": "/tmp/raw-file"},
        {"document_content": "raw document text"},
    ],
)
def test_action_contract_rejects_sensitive_metadata_keys(metadata):
    with pytest.raises(FeishuContractError) as exc_info:
        _action(metadata=metadata)

    assert exc_info.value.failure_class == "feishu_action_sensitive_metadata"


def test_metadata_hash_does_not_mask_contract_redaction_errors():
    with pytest.raises(FeishuContractError) as exc_info:
        _action(metadata={"open_id_hash": "ou_raw"})

    assert exc_info.value.failure_class == "feishu_action_metadata_hash_invalid"


@pytest.mark.parametrize(
    ("overrides", "failure_class"),
    [
        ({"payload_hash": "raw payload"}, "feishu_render_payload_hash_invalid"),
        ({"content_hash": "raw content"}, "feishu_render_content_hash_invalid"),
        ({"provenance_hash": "raw provenance"}, "feishu_render_provenance_hash_invalid"),
        ({"action_digest": "raw action"}, "feishu_action_sibling_digest_invalid"),
        ({"route_snapshot_hash": "raw route"}, "feishu_action_sibling_route_invalid"),
    ],
)
def test_render_part_hash_fields_must_be_sha256(overrides, failure_class):
    _assert_raises_failure(lambda: _part("post", **overrides), failure_class)


@pytest.mark.parametrize("fallback_action", ["preserve", "replace", "drop"])
def test_fallback_matrix_allows_declared_actions(fallback_action):
    assert validate_render_part(_part("markdown", fallback_action=fallback_action)) == (
        True,
        None,
    )


def test_fallback_matrix_rejects_undeclared_actions():
    _assert_raises_failure(
        lambda: _part("markdown", fallback_action="retry"),
        "feishu_render_fallback_invalid",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_group": "sha256:" + "8" * 64, "chunk_index": 0, "chunk_count": None},
        {"chunk_group": "sha256:" + "8" * 64, "chunk_index": None, "chunk_count": 2},
        {"chunk_group": None, "chunk_index": 0, "chunk_count": 2},
        {"chunk_group": "sha256:" + "8" * 64, "chunk_index": -1, "chunk_count": 2},
        {"chunk_group": "sha256:" + "8" * 64, "chunk_index": 2, "chunk_count": 2},
        {"chunk_group": "chunk-raw", "chunk_index": 0, "chunk_count": 2},
    ],
)
def test_chunk_group_index_and_count_are_coherent(overrides):
    _assert_raises_failure(
        lambda: _part("plain_text", **overrides),
        "feishu_render_chunk_invalid",
    )


@pytest.mark.parametrize("part_type", ["image", "file"])
def test_attachment_parts_require_source_class_and_provenance_hash(part_type):
    _assert_raises_failure(
        lambda: _part(part_type, provenance_hash=_PROVENANCE_HASH),
        "feishu_render_attachment_source_missing",
    )
    _assert_raises_failure(
        lambda: _part(part_type, source_class="generated"),
        "feishu_attachment_provenance_missing",
    )
    _assert_raises_failure(
        lambda: _part(
            part_type,
            source_class="generated",
            provenance_hash="/tmp/raw-file",
        ),
        "feishu_render_attachment_provenance_invalid",
    )


@pytest.mark.parametrize(
    "source_class",
    [
        "generated_asset",
        "user_attachment",
        "/tmp/upload",
        "token_cache",
        "open_id",
        "document_content",
    ],
)
def test_attachment_source_class_is_sanitized_enum(source_class):
    _assert_raises_failure(
        lambda: _part(
            "image",
            source_class=source_class,
            provenance_hash=_PROVENANCE_HASH,
        ),
        "feishu_render_attachment_source_invalid",
    )


@pytest.mark.parametrize(
    ("target_ref_hash", "object_ref_hash", "failure_class"),
    [
        ("oc_raw_chat", "sha256:" + "b" * 64, "feishu_render_target_ref_invalid"),
        ("sha256:" + "a" * 64, "/tmp/raw-object", "feishu_render_object_ref_invalid"),
        ("sha256:" + "a" * 64, "doc_token_raw", "feishu_render_object_ref_invalid"),
    ],
)
def test_render_plan_rejects_raw_target_or_object_refs(
    target_ref_hash,
    object_ref_hash,
    failure_class,
):
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlan(
            target_ref_hash=target_ref_hash,
            object_ref_hash=object_ref_hash,
            render_mode="offline_snapshot",
            parts=(_part("markdown"),),
        )

    assert exc_info.value.failure_class == failure_class


def test_render_plan_rejects_unknown_render_mode():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlan(
            target_ref_hash="sha256:" + "a" * 64,
            object_ref_hash="sha256:" + "b" * 64,
            render_mode="live_send",
            parts=(_part("markdown"),),
        )

    assert exc_info.value.failure_class == "feishu_render_mode_invalid"


@pytest.mark.parametrize(
    "parts",
    [
        (
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=0,
                chunk_count=3,
            ),
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=1,
                chunk_count=3,
            ),
        ),
        (
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=0,
                chunk_count=2,
            ),
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=0,
                chunk_count=2,
            ),
        ),
        (
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=0,
                chunk_count=2,
            ),
            _part(
                "plain_text",
                chunk_group="sha256:" + "8" * 64,
                chunk_index=1,
                chunk_count=3,
            ),
        ),
    ],
)
def test_render_plan_validates_chunk_group_completeness(parts):
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlan(
            target_ref_hash="sha256:" + "a" * 64,
            object_ref_hash="sha256:" + "b" * 64,
            render_mode="offline_snapshot",
            parts=parts,
        )

    assert exc_info.value.failure_class == "feishu_render_chunk_group_incomplete"


@pytest.mark.parametrize(
    ("sibling_overrides", "failure_class"),
    [
        ({"action_digest": "sha256:" + "6" * 64}, "feishu_action_sibling_mismatch"),
        ({"route_snapshot_hash": "sha256:" + "6" * 64}, "feishu_action_sibling_mismatch"),
        ({"same_operator_scope": False}, "feishu_action_sibling_mismatch"),
        ({"expires_at": "2026-06-08T11:00:00Z"}, "feishu_action_sibling_mismatch"),
    ],
)
def test_card_button_action_sibling_fields_must_match_or_be_unset(
    sibling_overrides,
    failure_class,
):
    _assert_raises_failure(
        lambda: _part("button", action_contract=_action(), **sibling_overrides),
        failure_class,
    )


def test_card_button_action_sibling_fields_may_match_nested_contract():
    part = _part(
        "button",
        action_contract=_action(),
        action_digest=_ACTION_DIGEST,
        route_snapshot_hash=_ROUTE_HASH,
        same_operator_scope=True,
        expires_at="2026-06-08T10:30:00Z",
    )

    assert validate_render_part(part) == (True, None)


def test_action_contract_hash_is_opaque_and_stable():
    contract = _action(metadata={"intent_hash": "sha256:" + "c" * 64})
    equivalent = _action(metadata={"intent_hash": "sha256:" + "c" * 64})

    assert validate_action_contract(contract) == (True, None)
    assert _SHA256_HASH_RE.fullmatch(contract.contract_hash)
    assert contract.contract_hash == equivalent.contract_hash


@pytest.mark.parametrize("metadata", ["raw", ["raw"], 1])
def test_non_mapping_metadata_is_rejected_before_hashing(metadata):
    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_metadata_invalid",
    )
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_metadata_invalid",
    )


def test_none_metadata_is_canonicalized_to_empty_mapping():
    action = _action(metadata=None)
    part = _part("post", metadata=None)

    assert action.metadata == {}
    assert part.metadata == {}
    assert validate_action_contract(action) == (True, None)
    assert validate_render_part(part) == (True, None)


def test_external_metadata_mutation_cannot_change_constructed_contracts():
    metadata = {
        "format": "feishu_post",
        "nested": {"intent_hash": "sha256:" + "c" * 64},
        "profiles": [{"render_profile": "compact"}],
    }
    action = _action(metadata=metadata)
    part = _part("post", metadata=metadata)
    action_hash = action.contract_hash
    part_hash = part.part_hash

    metadata["format"] = "mutated"
    metadata["nested"]["intent_hash"] = "raw"
    metadata["profiles"][0]["render_profile"] = "mutated"

    assert action.metadata["format"] == "feishu_post"
    assert action.metadata["nested"]["intent_hash"] == "sha256:" + "c" * 64
    assert action.metadata["profiles"][0]["render_profile"] == "compact"
    assert part.metadata["format"] == "feishu_post"
    assert validate_action_contract(action) == (True, None)
    assert validate_render_part(part) == (True, None)
    assert action.contract_hash == action_hash
    assert part.part_hash == part_hash


@pytest.mark.parametrize(
    "key",
    [
        "message_id",
        "file_id",
        "raw_content",
        "source_path",
        "api_path",
        "http_method",
        "request_body",
        "content",
    ],
)
def test_raw_metadata_keys_are_rejected_recursively(key):
    metadata = {"outer": [{"nested": {key: "raw"}}]}

    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_sensitive_metadata",
    )
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_sensitive_metadata",
    )


def test_metadata_hash_keys_must_have_sha256_values_recursively():
    metadata = {"outer": [{"content_hash": "raw content"}]}

    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_metadata_hash_invalid",
    )
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_metadata_hash_invalid",
    )


def test_unknown_action_kind_is_rejected_before_hashing():
    _assert_raises_failure(
        lambda: _action(action_kind="menu"),
        "feishu_action_kind_invalid",
    )


@pytest.mark.parametrize(
    ("part_type", "action_kind"),
    [("button", "card"), ("card", "button")],
)
def test_card_button_action_kind_must_match_part_type(part_type, action_kind):
    _assert_raises_failure(
        lambda: _part(part_type, action_contract=_action(action_kind=action_kind)),
        "feishu_action_kind_mismatch",
    )


@pytest.mark.parametrize(
    "key",
    [
        "id",
        "object_id",
        "attachment_id",
        "image_id",
        "local_path",
        "object_path",
    ],
)
def test_broad_raw_id_and_path_metadata_keys_are_rejected(key):
    metadata = {"outer": [{key: "raw"}]}

    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_sensitive_metadata",
    )
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_sensitive_metadata",
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"label": "ou_raw"},
        {"label": "/tmp/raw-file"},
        {"label": {"value": "/open-apis/im/v1/messages"}},
        {"label": "raw document text"},
    ],
)
def test_innocuous_metadata_keys_cannot_carry_raw_values(metadata):
    _assert_raises_failure(
        lambda: _action(metadata=metadata),
        "feishu_action_metadata_value_invalid",
    )
    _assert_raises_failure(
        lambda: _part("post", metadata=metadata),
        "feishu_action_metadata_value_invalid",
    )


def test_safe_metadata_schema_examples_still_pass():
    metadata = {
        "format": "feishu_post",
        "language": "python",
        "chunk_role": "first",
        "fallback_profile": "preserve",
        "intent_hash": "sha256:" + "c" * 64,
        "nested": {
            "render_profile": "compact",
            "capability": "preview",
        },
        "profiles": [{"source_type": "generated"}],
    }

    action = _action(metadata=metadata)
    part = _part("post", metadata=metadata)

    assert validate_action_contract(action) == (True, None)
    assert validate_render_part(part) == (True, None)

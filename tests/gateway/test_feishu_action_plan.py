import re

import pytest

from gateway.feishu_action_plan import (
    FeishuActionContract,
    RenderPlan,
    RenderPlanPart,
    validate_action_contract,
    validate_render_part,
)


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
            source_class="generated_asset",
            provenance_hash=_PROVENANCE_HASH,
            fallback_action="replace",
        ),
        _part(
            "file",
            source_class="user_attachment",
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


@pytest.mark.parametrize("part_type", ["card", "button"])
def test_card_and_button_parts_require_opaque_action_contract(part_type):
    part = _part(part_type, action_contract=None)

    assert validate_render_part(part) == (
        False,
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
    contract = _action(**overrides)

    assert validate_action_contract(contract) == (False, failure_class)


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
    part = _part("post", metadata=metadata)
    contract = _action(metadata=metadata)

    assert validate_render_part(part) == (
        False,
        "feishu_action_raw_tool_material",
    )
    assert validate_action_contract(contract) == (
        False,
        "feishu_action_raw_tool_material",
    )


@pytest.mark.parametrize("fallback_action", ["preserve", "replace", "drop"])
def test_fallback_matrix_allows_declared_actions(fallback_action):
    assert validate_render_part(_part("markdown", fallback_action=fallback_action)) == (
        True,
        None,
    )


def test_fallback_matrix_rejects_undeclared_actions():
    assert validate_render_part(_part("markdown", fallback_action="retry")) == (
        False,
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
    assert validate_render_part(_part("plain_text", **overrides)) == (
        False,
        "feishu_render_chunk_invalid",
    )


@pytest.mark.parametrize("part_type", ["image", "file"])
def test_attachment_parts_require_source_class_and_provenance_hash(part_type):
    missing_source = _part(part_type, provenance_hash=_PROVENANCE_HASH)
    missing_provenance = _part(part_type, source_class="generated_asset")
    raw_provenance = _part(
        part_type,
        source_class="generated_asset",
        provenance_hash="/tmp/raw-file",
    )

    assert validate_render_part(missing_source) == (
        False,
        "feishu_render_attachment_source_missing",
    )
    assert validate_render_part(missing_provenance) == (
        False,
        "feishu_render_attachment_provenance_missing",
    )
    assert validate_render_part(raw_provenance) == (
        False,
        "feishu_render_attachment_provenance_invalid",
    )


def test_action_contract_hash_is_opaque_and_stable():
    contract = _action(metadata={"intent_hash": "sha256:" + "c" * 64})
    equivalent = _action(metadata={"intent_hash": "sha256:" + "c" * 64})

    assert validate_action_contract(contract) == (True, None)
    assert _SHA256_HASH_RE.fullmatch(contract.contract_hash)
    assert contract.contract_hash == equivalent.contract_hash

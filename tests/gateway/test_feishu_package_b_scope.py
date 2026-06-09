from __future__ import annotations

from pathlib import Path

import pytest

from gateway.feishu_readiness import (
    FEISHU_PACKAGE_B_ADAPTER_ONLY_CAPABILITIES,
    FEISHU_PACKAGE_B_MODEL_VISIBLE_CAPABILITIES,
    classify_feishu_package_b_tool_identifier,
    classify_feishu_package_b_tool_scope,
    feishu_package_b_model_visible_capabilities,
)


EXPECTED_MODEL_VISIBLE = frozenset(
    {
        "feishu.current.reply.send",
        "feishu.current.reply.edit_bot_owned",
        "feishu.current.card.clarification.create",
        "feishu.current.card.confirmation.create",
        "feishu.current.attachment.echo_provenance",
        "feishu.current.attachment.send_generated",
    }
)

EXPECTED_ADAPTER_ONLY = frozenset(
    {
        "feishu.adapter.inbound.admit_current",
        "feishu.adapter.delivery.record_lifecycle",
        "feishu.adapter.card.resolve_callback",
        "feishu.adapter.provenance.record_inbound_attachment",
        "feishu.adapter.provenance.record_generated_attachment",
        "feishu.adapter.scope.audit",
    }
)


def test_package_b_allowlists_are_exact_capability_identifiers():
    assert FEISHU_PACKAGE_B_MODEL_VISIBLE_CAPABILITIES == EXPECTED_MODEL_VISIBLE
    assert FEISHU_PACKAGE_B_ADAPTER_ONLY_CAPABILITIES == EXPECTED_ADAPTER_ONLY
    assert FEISHU_PACKAGE_B_MODEL_VISIBLE_CAPABILITIES.isdisjoint(
        FEISHU_PACKAGE_B_ADAPTER_ONLY_CAPABILITIES
    )


def test_model_visible_discovery_exposes_only_package_b_current_conversation_ids():
    exposed = feishu_package_b_model_visible_capabilities()

    assert exposed == tuple(sorted(EXPECTED_MODEL_VISIBLE))


@pytest.mark.parametrize("identifier", sorted(EXPECTED_MODEL_VISIBLE))
def test_classifier_marks_exact_package_b_capabilities_model_visible(identifier):
    assert classify_feishu_package_b_tool_identifier(identifier) == "model_visible"


@pytest.mark.parametrize("identifier", sorted(EXPECTED_ADAPTER_ONLY))
def test_classifier_marks_adapter_capabilities_non_model_visible(identifier):
    assert classify_feishu_package_b_tool_identifier(identifier) == "adapter_only"


@pytest.mark.parametrize(
    "identifier",
    [
        "feishu.calendar.event.create",
        "feishu.task.create",
        "feishu.approval.instance.get",
        "feishu.base.record.search",
        "feishu.sheets.values.read",
        "feishu.drive.search",
        "feishu.wiki.search",
        "feishu.cross_chat.send",
        "feishu.admin.user.lookup",
        "feishu.openapi.raw",
        "feishu.file.export.arbitrary",
        "feishu_doc_read",
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
        "feishu_drive_reply_comment",
        "feishu_drive_add_comment",
        "feishu.comment.create",
        "feishu.descriptor.send",
        "feishu.status_card.update",
        "feishu.generic_card.send",
        "feishu.reaction.add",
    ],
)
def test_classifier_denies_package_c_to_g_and_legacy_surfaces(identifier):
    assert classify_feishu_package_b_tool_identifier(identifier) == "denied"


def test_unknown_feishu_identifier_fails_closed_as_scope_creep():
    result = classify_feishu_package_b_tool_scope(
        model_visible_identifiers=EXPECTED_MODEL_VISIBLE
        | {"feishu.current.reply.delete"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_package_b_scope_creep"
    assert result.blockers == (
        "feishu_package_b_scope_creep:feishu.current.reply.delete",
    )


def test_adapter_only_identifier_is_never_model_visible():
    result = classify_feishu_package_b_tool_scope(
        model_visible_identifiers=EXPECTED_MODEL_VISIBLE
        | {"feishu.adapter.delivery.record_lifecycle"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_package_b_scope_creep"
    assert result.blockers == (
        "feishu_package_b_scope_creep:feishu.adapter.delivery.record_lifecycle",
    )


def test_adapter_only_invocation_requires_brokered_scope_check():
    result = classify_feishu_package_b_tool_scope(
        model_visible_identifiers=EXPECTED_MODEL_VISIBLE,
        adapter_invoked_identifiers={"feishu.adapter.scope.audit"},
        adapter_scope_checked=False,
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_adapter_scope_check_missing"
    assert result.blockers == (
        "feishu_adapter_scope_check_missing:feishu.adapter.scope.audit",
    )


def test_adapter_only_invocation_passes_when_scope_check_is_present():
    result = classify_feishu_package_b_tool_scope(
        model_visible_identifiers=EXPECTED_MODEL_VISIBLE,
        adapter_invoked_identifiers=EXPECTED_ADAPTER_ONLY,
        adapter_scope_checked=True,
    )

    assert result.status == "ready"
    assert result.failure_class is None
    assert result.blockers == ()
    assert result.warnings == ()


def test_denied_out_of_scope_identifier_in_model_surface_fails_without_state_change(
    tmp_path: Path,
):
    before = sorted(tmp_path.iterdir())

    result = classify_feishu_package_b_tool_scope(
        model_visible_identifiers=EXPECTED_MODEL_VISIBLE | {"feishu.openapi.raw"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_package_b_scope_creep"
    assert result.blockers == ("feishu_package_b_scope_creep:feishu.openapi.raw",)
    assert sorted(tmp_path.iterdir()) == before


def test_hermes_feishu_toolset_does_not_expose_legacy_doc_drive_tools():
    from toolsets import resolve_toolset

    resolved = set(resolve_toolset("hermes-feishu"))

    assert {
        "feishu_doc_read",
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
        "feishu_drive_reply_comment",
        "feishu_drive_add_comment",
    }.isdisjoint(resolved)
    feishu_scoped = {name for name in resolved if name.startswith("feishu")}
    assert feishu_scoped <= EXPECTED_MODEL_VISIBLE

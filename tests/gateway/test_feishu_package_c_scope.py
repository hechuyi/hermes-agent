from __future__ import annotations

import pytest

import gateway.feishu_readiness as readiness


PACKAGE_B_MODEL_VISIBLE = frozenset(
    {
        "feishu.current.attachment.echo_provenance",
        "feishu.current.attachment.send_generated",
        "feishu.current.card.clarification.create",
        "feishu.current.card.confirmation.create",
        "feishu.current.reply.edit_bot_owned",
        "feishu.current.reply.send",
    }
)

DENIED_BUSINESS_IDENTIFIERS = (
    "feishu_doc_read",
    "feishu_drive_list_comments",
    "feishu_drive_list_comment_replies",
    "feishu_drive_reply_comment",
    "feishu_drive_add_comment",
    "feishu_drive_future_operation",
    "feishu.doc.read",
    "feishu.docs.read",
    "feishu.document.read",
    "feishu.drive.file.get",
    "feishu.wiki.node.get",
    "feishu.calendar.event.create",
    "feishu.task.create",
    "feishu.approval.instance.get",
    "feishu.base.record.search",
    "feishu.sheets.values.read",
    "feishu.search.query",
    "feishu.contact.user.get",
    "feishu.admin.user.lookup",
    "feishu.openapi.raw",
    "feishu.file.export.document",
    "feishu.cross_chat.send",
    "feishu.comment.create",
    "feishu.descriptor.send",
    "feishu.status_card.update",
    "feishu.generic_card.send",
    "feishu.reaction.add",
)


def _model_visible_feishu_tool_names() -> frozenset[str]:
    import model_tools

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-feishu"],
        quiet_mode=True,
    )
    return frozenset(
        tool["function"]["name"]
        for tool in definitions
        if tool["function"]["name"].startswith("feishu")
    )


def _toolset_alias_inventory() -> dict[str, frozenset[str]]:
    from toolsets import resolve_toolset

    return {
        "hermes-feishu": frozenset(resolve_toolset("hermes-feishu")),
        "feishu_doc": frozenset(resolve_toolset("feishu_doc")),
        "feishu_drive": frozenset(resolve_toolset("feishu_drive")),
    }


@pytest.mark.parametrize("identifier", DENIED_BUSINESS_IDENTIFIERS)
def test_package_c_classifier_explicitly_denies_business_identifiers(identifier):
    assert readiness.classify_feishu_package_c_tool_identifier(identifier) == "denied"


def test_package_c_scope_accepts_current_package_b_model_visible_inventory():
    actual = _model_visible_feishu_tool_names()

    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=actual,
        adapter_invoked_identifiers=(),
        quiet_mode_cache_identifiers=actual,
        toolset_aliases=_toolset_alias_inventory(),
        brokered_legacy_text_identifiers={
            "feishu_doc_read",
            "feishu_drive_list_comments",
            "feishu_drive_list_comment_replies",
            "feishu_drive_reply_comment",
            "feishu_drive_add_comment",
        },
    )

    assert actual == PACKAGE_B_MODEL_VISIBLE
    assert result.status == "ready"
    assert result.failure_class is None
    assert result.blockers == ()


@pytest.mark.parametrize(
    "identifier",
    [
        "feishu.doc.read",
        "feishu.search.query",
        "feishu.contact.user.get",
        "feishu_doc_read",
        "feishu_drive_reply_comment",
    ],
)
def test_package_c_scope_denies_business_identifier_in_model_visible_surface(identifier):
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE | {identifier},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_business_tool_surface_denied"
    assert result.blockers == (f"feishu_business_tool_surface_denied:{identifier}",)


def test_package_c_scope_denies_business_identifier_in_adapter_success_surface():
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        adapter_invoked_identifiers={"feishu.calendar.event.create"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_business_tool_surface_denied"
    assert result.blockers == (
        "feishu_business_tool_surface_denied:feishu.calendar.event.create",
    )


def test_package_c_scope_fails_closed_for_unknown_feishu_identifier():
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE
        | {"feishu.future.unclassified"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_package_c_scope_creep"
    assert result.blockers == ("feishu_package_c_scope_creep:feishu.future.unclassified",)


def test_package_c_internal_identifiers_are_adapter_or_readiness_only():
    internal = frozenset(readiness.FEISHU_PACKAGE_C_INTERNAL_IDENTIFIERS)

    adapter_result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        adapter_invoked_identifiers=internal,
    )
    model_result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE | internal,
    )

    assert internal
    assert adapter_result.status == "ready"
    assert model_result.status == "not_ready"
    assert model_result.failure_class == "feishu_package_c_scope_creep"
    assert model_result.blockers == tuple(
        f"feishu_package_c_scope_creep:{identifier}"
        for identifier in sorted(internal)
    )


def test_legacy_feishu_doc_drive_toolsets_remain_visible_as_brokered_legacy_risk_only():
    inventory = _toolset_alias_inventory()

    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        toolset_aliases=inventory,
        brokered_legacy_text_identifiers=inventory["feishu_doc"]
        | inventory["feishu_drive"],
    )

    assert "feishu_doc_read" in inventory["feishu_doc"]
    assert "feishu_drive_reply_comment" in inventory["feishu_drive"]
    assert inventory["hermes-feishu"].isdisjoint(
        inventory["feishu_doc"] | inventory["feishu_drive"]
    )
    assert result.status == "ready"
    assert result.blockers == ()


def test_hermes_feishu_toolset_alias_with_business_identifier_fails_closed():
    inventory = _toolset_alias_inventory()
    inventory["hermes-feishu"] = inventory["hermes-feishu"] | {"feishu.doc.read"}

    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        toolset_aliases=inventory,
        brokered_legacy_text_identifiers=inventory["feishu_doc"]
        | inventory["feishu_drive"],
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_business_tool_surface_denied"
    assert result.blockers == (
        "feishu_business_tool_surface_denied:hermes-feishu:feishu.doc.read",
    )


@pytest.mark.parametrize(
    ("alias", "identifier", "blocker"),
    [
        (
            "feishu_future",
            "feishu.calendar.event.create",
            "feishu_business_tool_surface_denied:feishu_future:feishu.calendar.event.create",
        ),
        (
            "feishu_future",
            "feishu.future.unclassified",
            "feishu_package_c_scope_creep:feishu_future:feishu.future.unclassified",
        ),
    ],
)
def test_non_legacy_toolset_aliases_fail_closed_for_feishu_business_or_unknown(
    alias,
    identifier,
    blocker,
):
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        toolset_aliases={alias: {identifier}},
        brokered_legacy_text_identifiers={
            "feishu_doc_read",
            "feishu_drive_reply_comment",
        },
    )

    assert result.status == "not_ready"
    assert result.failure_class == blocker.split(":", 1)[0]
    assert result.blockers == (blocker,)


def test_legacy_toolset_alias_requires_brokered_legacy_identifier_allowlist():
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        toolset_aliases={"feishu_doc": {"feishu_doc_read", "feishu.doc.read"}},
        brokered_legacy_text_identifiers={"feishu_doc_read"},
    )

    assert result.status == "not_ready"
    assert result.failure_class == "feishu_business_tool_surface_denied"
    assert result.blockers == (
        "feishu_business_tool_surface_denied:feishu_doc:feishu.doc.read",
    )


@pytest.mark.parametrize(
    ("identifier", "failure_class", "blocker"),
    [
        (
            "feishu.calendar.event.create",
            "feishu_business_tool_surface_denied",
            "feishu_business_tool_surface_denied:feishu_doc:feishu.calendar.event.create",
        ),
        (
            "feishu.future.unclassified",
            "feishu_package_c_scope_creep",
            "feishu_package_c_scope_creep:feishu_doc:feishu.future.unclassified",
        ),
    ],
)
def test_legacy_toolset_alias_cannot_expand_brokered_legacy_identifier_allowlist(
    identifier,
    failure_class,
    blocker,
):
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        toolset_aliases={"feishu_doc": {identifier}},
        brokered_legacy_text_identifiers={identifier},
    )

    assert result.status == "not_ready"
    assert result.failure_class == failure_class
    assert result.blockers == (blocker,)


def test_comment_agent_prompt_legacy_mentions_are_not_package_c_success_surface():
    result = readiness.classify_feishu_package_c_scope(
        model_visible_identifiers=PACKAGE_B_MODEL_VISIBLE,
        brokered_legacy_text_identifiers={
            "feishu_doc_read",
            "feishu_drive_list_comments",
            "feishu_drive_reply_comment",
        },
    )

    assert result.status == "ready"
    assert result.failure_class is None

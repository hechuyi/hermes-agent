"""Tests for provider-group folding in interactive model pickers."""

from hermes_cli.models import (
    CANONICAL_PROVIDERS,
    PROVIDER_GROUPS,
    group_providers,
    provider_group_for_slug,
)


def _slugs(rows):
    out = []
    for row in rows:
        if row["kind"] == "single":
            out.append(row["slug"])
        else:
            out.extend(row["members"])
    return out


def test_groups_reference_real_canonical_slugs():
    canonical = {p.slug for p in CANONICAL_PROVIDERS}
    for group_id, (label, members) in PROVIDER_GROUPS.items():
        assert label, f"group {group_id} has empty label"
        assert members, f"group {group_id} has no members"
        for slug in members:
            assert slug in canonical, f"group {group_id} member {slug!r} is not canonical"


def test_reverse_index_matches_groups():
    for group_id, (_label, members) in PROVIDER_GROUPS.items():
        for slug in members:
            assert provider_group_for_slug(slug) == group_id
    assert provider_group_for_slug("openrouter") == ""
    assert provider_group_for_slug("") == ""


def test_multi_member_group_folds_to_one_row():
    rows = group_providers(["minimax", "minimax-oauth", "minimax-cn"])
    assert rows == [
        {
            "kind": "group",
            "group_id": "minimax",
            "label": "MiniMax",
            "members": ["minimax", "minimax-oauth", "minimax-cn"],
        }
    ]


def test_single_present_member_degrades_to_single_row():
    rows = group_providers(["xai"])
    assert rows == [{"kind": "single", "slug": "xai"}]


def test_group_position_follows_first_present_member():
    rows = group_providers(["nous", "minimax", "deepseek", "minimax-cn"])
    assert [(r["kind"], r.get("group_id") or r.get("slug")) for r in rows] == [
        ("single", "nous"),
        ("group", "minimax"),
        ("single", "deepseek"),
    ]
    assert rows[1]["members"] == ["minimax", "minimax-cn"]


def test_group_member_order_follows_declaration():
    rows = group_providers(["minimax-cn", "minimax", "minimax-oauth"])
    assert rows[0]["members"] == ["minimax", "minimax-oauth", "minimax-cn"]


def test_grouping_is_lossless_for_canonical_provider_slugs():
    flat = [p.slug for p in CANONICAL_PROVIDERS]
    rows = group_providers(flat)
    assert set(_slugs(rows)) == set(flat)
    assert len(rows) < len(flat)

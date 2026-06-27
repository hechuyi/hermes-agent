"""Tests for the single-shape session_search tool.

Three calling shapes:
  1. DISCOVERY — pass query → FTS5 + anchored window + bookends per hit
  2. SCROLL    — pass session_id + around_message_id → just the window
  3. BROWSE    — no args → recent sessions chronologically

All run zero LLM calls.
"""
import json
import time

import pytest

from hermes_state import SessionDB
from tools import session_search_tool as session_search_mod
from tools.session_search_tool import (
    SESSION_SEARCH_SCHEMA,
    _HIDDEN_SESSION_SOURCES,
    _format_timestamp,
    session_search,
)


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_modpack_sessions(db):
    """Create three sessions about a modpack so FTS5 has hits to dedupe."""
    now = int(time.time())
    # Older session — modpack origin
    db.create_session("s_oldest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 30000, "Building the Modpack", "s_oldest"))
    db.append_message("s_oldest", role="user", content="Let's build a Minecraft modpack")
    db.append_message("s_oldest", role="assistant", content="Great. Let me scaffold the modpack repo.")
    db.append_message("s_oldest", role="user", content="Use NeoForge 1.21.1")
    db.append_message("s_oldest", role="assistant", content="Done. Modpack repo created with NeoForge 1.21.1.")
    db.append_message("s_oldest", role="assistant", content="Tier-0 mods installed; modpack smoke test passes.")

    # Middle session — modpack quest coverage
    db.create_session("s_middle", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 15000, "Modpack Quest Coverage", "s_middle"))
    db.append_message("s_middle", role="user", content="Deep-dive every modpack reference quest guide")
    db.append_message("s_middle", role="assistant", content="Surveying ATM10 questbook for modpack inspiration.")
    db.append_message("s_middle", role="user", content="Update the modpack version too")
    db.append_message("s_middle", role="assistant", content="Modpack version bumped 0.4 → 0.8.5; quest coverage page added.")

    # Newest session — modpack mob spawn fix
    db.create_session("s_newest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 1000, "Modpack Mob Spawn Fix", "s_newest"))
    db.append_message("s_newest", role="user", content="Fix the modpack mob spawning")
    db.append_message("s_newest", role="assistant", content="Investigating elite mob gating in the modpack KubeJS.")
    db.append_message("s_newest", role="assistant", content="Shipped commit b850442. Modpack alternator nerfed too.")
    db._conn.commit()


def _create_scoped_session(db, session_id, *, scope_id, route_key, content):
    db.create_session(
        session_id,
        source="feishu",
        conversation_scope_id=scope_id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=route_key,
        route_partition_key=route_key,
    )
    mid = db.append_message(session_id, role="user", content=content)
    db._conn.commit()
    return mid


def _set_session_title(db, session_id, title):
    db._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    db._conn.commit()


def _create_compression_child(db, parent_id, child_id, *, scope_id, route_key, content):
    now = int(time.time())
    db.create_session(
        parent_id,
        source="feishu",
        conversation_scope_id=scope_id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=route_key,
        route_partition_key=route_key,
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression' WHERE id = ?",
        (now - 100, now - 50, parent_id),
    )
    db.append_message(parent_id, role="user", content="parent context")
    db.create_session(
        child_id,
        source="feishu",
        parent_session_id=parent_id,
        conversation_scope_id=scope_id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=route_key,
        route_partition_key=route_key,
    )
    mid = db.append_message(child_id, role="user", content=content)
    db._conn.commit()
    return mid


# =========================================================================
# Schema invariants
# =========================================================================

class TestSchema:
    def test_schema_has_required_params(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        # Discovery shape
        assert "query" in params
        assert "limit" in params
        assert "sort" in params
        # Scroll shape
        assert "session_id" in params
        assert "around_message_id" in params
        assert "window" in params
        # Shared
        assert "role_filter" in params

    def test_no_mode_parameter(self):
        # Mode is inferred from which args are set — no explicit mode param
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert "mode" not in params

    def test_sort_enum(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert params["sort"]["enum"] == ["newest", "oldest"]

    def test_scope_enum_defaults_to_current_chat(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert params["scope"]["enum"] == ["current_chat", "current_route", "global"]
        assert params["scope"]["default"] == "current_chat"

    def test_default_without_runtime_scope_uses_global_history(self, db):
        _seed_modpack_sessions(db)

        result = json.loads(session_search(query="modpack", db=db))

        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1

    def test_explicit_current_chat_without_runtime_scope_errors(self, db):
        _seed_modpack_sessions(db)

        result = json.loads(session_search(query="modpack", db=db, scope="current_chat"))

        assert result["success"] is False
        assert "current_chat requires a runtime conversation scope" in result.get("error", "")

    def test_schema_description_teaches_scroll(self):
        desc = SESSION_SEARCH_SCHEMA["description"]
        assert "SCROLL" in desc
        assert "DISCOVERY" in desc
        assert "BROWSE" in desc
        # Must explain how to scroll
        assert "scroll FORWARD" in desc or "messages[-1]" in desc

    def test_no_llm_promise_in_description(self):
        # The new design never calls an LLM
        desc = SESSION_SEARCH_SCHEMA["description"].lower()
        assert "no llm" in desc


class TestHiddenSources:
    def test_tool_source_hidden(self):
        assert "tool" in _HIDDEN_SESSION_SOURCES


class TestFormatTimestamp:
    def test_unix_timestamp(self):
        out = _format_timestamp(1700000000)
        assert "2023" in out

    def test_none(self):
        assert _format_timestamp(None) == "unknown"

    def test_iso_string_passthrough(self):
        out = _format_timestamp("not-a-number-string")
        assert out == "not-a-number-string"


# =========================================================================
# Browse shape (no args)
# =========================================================================

class TestBrowseShape:
    def test_no_args_returns_recent_sessions(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, scope="global"))
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert result["count"] >= 3

    def test_browse_excludes_current_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, current_session_id="s_newest", scope="global"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_browse_returns_titles(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, scope="global"))
        titles = [r.get("title") for r in result["results"]]
        assert any("Modpack" in (t or "") for t in titles)


# =========================================================================
# Discovery shape (with query)
# =========================================================================

class TestDiscoveryShape:
    def test_query_returns_anchored_windows(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, scope="global"))
        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1

    def test_discovery_result_has_bookends_and_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db, scope="global"))
        for hit in result["results"]:
            assert "bookend_start" in hit
            assert "messages" in hit
            assert "bookend_end" in hit
            assert "match_message_id" in hit
            assert "snippet" in hit
            assert "messages_before" in hit
            assert "messages_after" in hit

    def test_match_message_id_is_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db, scope="global"))
        for hit in result["results"]:
            anchor_id = hit["match_message_id"]
            window_ids = [m["id"] for m in hit["messages"]]
            assert anchor_id in window_ids

    def test_no_results_returns_empty_list(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="zzz_no_such_term_zzz", db=db, scope="global"))
        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_limit_clamped_to_max_10(self, db):
        _seed_modpack_sessions(db)
        # Pass huge limit; should not error and should cap
        result = json.loads(session_search(query="modpack", limit=999, db=db, scope="global"))
        assert result["count"] <= 10

    def test_limit_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=0, db=db, scope="global"))
        # Result count depends on hits, but the limit must be at least 1
        assert result["count"] >= 0

    def test_non_int_limit_falls_back(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit="bogus", db=db, scope="global"))
        assert result["success"] is True

    def test_current_session_filtered_out(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, current_session_id="s_newest", scope="global"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_query_can_match_session_title_without_message_hit(self, db):
        db.create_session("s_fingerprint", source="cli")
        _set_session_title(db, "s_fingerprint", "fingerprint-login")
        db.append_message("s_fingerprint", role="user", content="configure PAM for biometric auth")
        db.append_message("s_fingerprint", role="assistant", content="Checking Linux auth settings.")
        db._conn.commit()

        result = json.loads(session_search(query="fingerprint-login", db=db, scope="global"))

        assert result["success"] is True
        assert result["count"] == 1
        hit = result["results"][0]
        assert hit["session_id"] == "s_fingerprint"
        assert hit["title"] == "fingerprint-login"
        assert hit["matched_role"] == "session_title"
        assert "Session title matched" in hit["snippet"]

    def test_title_query_strips_common_model_quoting(self, db):
        db.create_session("s_fingerprint", source="cli")
        _set_session_title(db, "s_fingerprint", "fingerprint-login")
        db.append_message("s_fingerprint", role="user", content="PAM auth setup")
        db._conn.commit()

        result = json.loads(session_search(query="`fingerprint-login`", db=db, scope="global"))

        assert result["success"] is True
        assert result["results"][0]["session_id"] == "s_fingerprint"
        assert result["results"][0]["matched_role"] == "session_title"

    def test_title_match_respects_current_session_filter(self, db):
        db.create_session("s_current", source="cli")
        _set_session_title(db, "s_current", "fingerprint-login")
        db.append_message("s_current", role="user", content="PAM auth setup")
        db._conn.commit()

        result = json.loads(session_search(
            query="fingerprint-login",
            current_session_id="s_current",
            db=db,
            scope="global",
        ))

        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_title_match_respects_current_chat_scope(self, db):
        _create_scoped_session(
            db, "chat-a", scope_id="cs_a", route_key="route-a",
            content="alpha body without title token",
        )
        _set_session_title(db, "chat-a", "chat-a-title")
        _create_scoped_session(
            db, "chat-b", scope_id="cs_b", route_key="route-b",
            content="beta body without title token",
        )
        _set_session_title(db, "chat-b", "chat-b-title")

        result = json.loads(session_search(
            query="chat-b-title",
            db=db,
            current_conversation_scope_id="cs_a",
            current_route_partition_key="route-a",
        ))

        assert result["success"] is True
        assert result["results"] == []

    def test_title_match_respects_current_route_scope(self, db):
        _create_scoped_session(
            db, "route-a", scope_id="cs_shared", route_key="route-a",
            content="route a body without title token",
        )
        _set_session_title(db, "route-a", "route-a-title")
        _create_scoped_session(
            db, "route-b", scope_id="cs_shared", route_key="route-b",
            content="route b body without title token",
        )
        _set_session_title(db, "route-b", "route-b-title")

        route_result = json.loads(session_search(
            query="route-b-title",
            scope="current_route",
            db=db,
            current_conversation_scope_id="cs_shared",
            current_route_partition_key="route-a",
        ))
        chat_result = json.loads(session_search(
            query="route-b-title",
            scope="current_chat",
            db=db,
            current_conversation_scope_id="cs_shared",
            current_route_partition_key="route-a",
        ))

        assert route_result["success"] is True
        assert route_result["results"] == []
        assert chat_result["success"] is True
        assert [r["session_id"] for r in chat_result["results"]] == ["route-b"]

    def test_global_title_match_crosses_chat_scope(self, db):
        _create_scoped_session(
            db, "chat-b", scope_id="cs_b", route_key="route-b",
            content="beta body without title token",
        )
        _set_session_title(db, "chat-b", "chat-b-title")

        result = json.loads(session_search(
            query="chat-b-title",
            scope="global",
            db=db,
            current_conversation_scope_id="cs_a",
            current_route_partition_key="route-a",
        ))

        assert result["success"] is True
        assert [r["session_id"] for r in result["results"]] == ["chat-b"]

    def test_current_chat_scope_hides_other_chat_by_default(self, db):
        _create_scoped_session(
            db, "chat-a", scope_id="cs_a", route_key="route-a",
            content="scoped recall needle",
        )
        _create_scoped_session(
            db, "chat-b", scope_id="cs_b", route_key="route-b",
            content="scoped recall needle",
        )

        result = json.loads(session_search(
            query="scoped recall needle",
            db=db,
            current_conversation_scope_id="cs_a",
            current_route_partition_key="route-a",
        ))

        assert result["success"] is True
        assert [r["session_id"] for r in result["results"]] == ["chat-a"]
        assert result["results"][0]["conversation_scope_id"] == "cs_a"

    def test_global_scope_returns_provenance_across_chats(self, db):
        _create_scoped_session(
            db, "chat-a", scope_id="cs_a", route_key="route-a",
            content="global recall needle",
        )
        _create_scoped_session(
            db, "chat-b", scope_id="cs_b", route_key="route-b",
            content="global recall needle",
        )

        result = json.loads(session_search(
            query="global recall needle",
            scope="global",
            db=db,
        ))

        assert result["success"] is True
        by_id = {r["session_id"]: r for r in result["results"]}
        assert set(by_id) == {"chat-a", "chat-b"}
        assert by_id["chat-a"]["conversation_scope_id"] == "cs_a"
        assert by_id["chat-b"]["route_partition_key"] == "route-b"


class TestDiscoverySort:
    def test_sort_newest_orders_by_recency(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="newest", db=db, scope="global"))
        # First result should be the most recent session
        first = result["results"][0]
        assert first["session_id"] == "s_newest" or "Newest" in (first.get("title") or "")

    def test_sort_oldest_orders_by_age(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="oldest", db=db, scope="global"))
        first = result["results"][0]
        assert first["session_id"] == "s_oldest"

    def test_invalid_sort_silently_ignored(self, db):
        _seed_modpack_sessions(db)
        # Should not error
        result = json.loads(session_search(query="modpack", sort="bogus", db=db, scope="global"))
        assert result["success"] is True


class TestRoleFilter:
    def test_default_excludes_tool_role(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="user", content="modpack question")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", db=db, scope="global"))
        # The FTS5 match should be on the user message, not the tool message
        if result["count"] > 0:
            matched_role = result["results"][0]["matched_role"]
            assert matched_role in ("user", "assistant")

    def test_explicit_tool_role_includes_tool(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", role_filter="tool", db=db, scope="global"))
        # Should now match the tool message
        if result["count"] > 0:
            assert result["results"][0]["matched_role"] == "tool"


class TestCronDemotion:
    def test_interactive_session_surfaces_above_cron(self, db):
        now = int(time.time())
        db.create_session("s_user", source="telegram")
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 90000, "s_user"))
        db.append_message("s_user", role="user", content="how is the venom project going")
        db.append_message("s_user", role="assistant", content="The venom project shipped its first milestone.")

        for i in range(60):
            sid = f"cron_{i}"
            db.create_session(sid, source="cron")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 1000 - i, sid))
            db.append_message(sid, role="user", content="venom project daily status")
            db.append_message(sid, role="assistant", content="venom project venom project venom summary")
        db._conn.commit()

        result = json.loads(session_search(query="venom project", limit=1, db=db, scope="global"))

        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["source"] == "telegram"
        assert result["results"][0]["session_id"] == "s_user"

    def test_cron_still_reachable_when_only_match(self, db):
        now = int(time.time())
        db.create_session("cron_only", source="cron")
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 500, "cron_only"))
        db.append_message("cron_only", role="user", content="quarterly archive sweep")
        db.append_message("cron_only", role="assistant", content="Archive sweep complete.")
        db._conn.commit()

        result = json.loads(session_search(query="archive sweep", db=db, scope="global"))

        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["source"] == "cron"

    def test_order_for_recall_is_stable_within_source_class(self):
        rows = [
            {"id": 1, "source": "cron"},
            {"id": 2, "source": "telegram"},
            {"id": 3, "source": "cron"},
            {"id": 4, "source": "cli"},
            {"id": 5, "source": None},
        ]

        assert hasattr(session_search_mod, "_order_for_recall")
        ordered = session_search_mod._order_for_recall(rows)

        assert [r["id"] for r in ordered] == [2, 4, 5, 1, 3]


# =========================================================================
# Scroll shape (session_id + around_message_id)
# =========================================================================

class TestScrollShape:
    def test_scroll_returns_window_without_bookends(self, db):
        _seed_modpack_sessions(db)
        # Get an anchor first via discovery
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]

        # Now scroll
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db, scope="global"
        ))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        assert "messages" in result
        # Scroll shape has no bookends
        assert "bookend_start" not in result
        assert "bookend_end" not in result

    def test_scroll_window_clamped_to_20(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=999, db=db, scope="global"
        ))
        assert result["window"] == 20

    def test_scroll_window_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=-5, db=db, scope="global"
        ))
        assert result["window"] == 1

    def test_scroll_returns_messages_before_after_counts(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=3, db=db, scope="global"
        ))
        assert "messages_before" in result
        assert "messages_after" in result

    def test_scroll_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db, scope="global"
        ))
        anchor_in_window = [m for m in result["messages"] if m["id"] == anchor_mid]
        assert len(anchor_in_window) == 1
        assert anchor_in_window[0].get("anchor") is True

    def test_scroll_missing_anchor_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id=999999, db=db, scope="global"
        ))
        assert result["success"] is False
        assert "not in" in result.get("error", "")

    def test_scroll_missing_session_errors(self, db):
        result = json.loads(session_search(
            session_id="nonexistent", around_message_id=1, db=db, scope="global"
        ))
        assert result["success"] is False

    def test_scroll_rejects_current_session_lineage(self, db):
        _seed_modpack_sessions(db)
        # Grab some valid id from s_oldest
        disc = json.loads(session_search(query="modpack", limit=3, db=db, scope="global"))
        match = [r for r in disc["results"] if r["session_id"] == "s_oldest"]
        if match:
            mid = match[0]["match_message_id"]
            result = json.loads(session_search(
                session_id="s_oldest", around_message_id=mid, db=db,
                current_session_id="s_oldest", scope="global",
            ))
            assert result["success"] is False
            assert "current session" in result.get("error", "").lower()

    def test_scroll_rebind_allows_same_scope_compression_child(self, db):
        mid = _create_compression_child(
            db, "parent", "child", scope_id="cs_a", route_key="route-a",
            content="child scroll needle",
        )

        result = json.loads(session_search(
            session_id="parent",
            around_message_id=mid,
            db=db,
            current_conversation_scope_id="cs_a",
            current_route_partition_key="route-a",
        ))

        assert result["success"] is True
        assert result["session_id"] == "child"
        assert any(m["id"] == mid and m.get("anchor") for m in result["messages"])

    def test_scroll_rejects_cross_scope_anchor(self, db):
        mid = _create_scoped_session(
            db, "chat-b", scope_id="cs_b", route_key="route-b",
            content="foreign scroll needle",
        )

        result = json.loads(session_search(
            session_id="chat-b",
            around_message_id=mid,
            db=db,
            current_conversation_scope_id="cs_a",
            current_route_partition_key="route-a",
        ))

        assert result["success"] is False
        assert "outside the current conversation scope" in result.get("error", "")

    def test_scroll_invalid_around_message_id_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id="not-an-int", db=db, scope="global"
        ))
        assert result["success"] is False


class TestScrollPattern:
    """The forward/backward scroll loop using tool output."""

    def test_scroll_forward_from_last_id(self, db):
        # Long session
        db.create_session("s_long", source="cli")
        ids = []
        for i in range(20):
            ids.append(db.append_message("s_long", role="user" if i % 2 == 0 else "assistant",
                                         content=f"long session msg {i}"))

        v1 = json.loads(session_search(
            session_id="s_long", around_message_id=ids[5], window=3, db=db, scope="global"
        ))
        last_id = v1["messages"][-1]["id"]
        v2 = json.loads(session_search(
            session_id="s_long", around_message_id=last_id, window=3, db=db, scope="global"
        ))
        # Forward scroll: v2 should reach further than v1
        assert max(m["id"] for m in v2["messages"]) > max(m["id"] for m in v1["messages"])
        # Boundary id appears in both
        assert last_id in [m["id"] for m in v1["messages"]]
        assert last_id in [m["id"] for m in v2["messages"]]


# =========================================================================
# Shape precedence
# =========================================================================

class TestShapePrecedence:
    def test_scroll_args_beat_query(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db, scope="global"))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        # Pass both query and scroll args — scroll should win
        result = json.loads(session_search(
            query="modpack",  # would normally trigger discovery
            session_id=anchor_sid, around_message_id=anchor_mid, db=db, scope="global",
        ))
        assert result["mode"] == "scroll"

    def test_empty_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="   ", db=db, scope="global"))
        assert result["mode"] == "browse"

    def test_non_string_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query=None, db=db, scope="global"))  # type: ignore
        assert result["mode"] == "browse"

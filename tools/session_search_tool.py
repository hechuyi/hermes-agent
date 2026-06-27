#!/usr/bin/env python3
"""
Session Search Tool - Long-Term Conversation Recall

Single-shape tool with three calling modes (inferred from args, no explicit
mode parameter):

  1. DISCOVERY — pass ``query``. Runs FTS5, dedupes hits by session lineage,
     returns top N sessions each with: snippet, ±5 message window around the
     match, plus bookend_start (first 3 user+assistant msgs of session) and
     bookend_end (last 3). Zero LLM cost.

  2. SCROLL — pass ``session_id`` + ``around_message_id``. Returns a window
     of ±window messages centered on the anchor, no FTS5, no bookends. To
     scroll forward / backward, re-anchor on the last / first message id of
     the returned window.

  3. BROWSE — no args. Returns recent sessions chronologically (titles,
     previews, timestamps).

All three modes operate on the SQLite session DB via the FTS5 index and
the get_anchored_view / get_messages_around primitives in hermes_state.
No LLM calls anywhere — every shape returns actual messages from the DB.

History: PR #20238 (JabberELF) seeded a fast/summary dual-mode split; the
toolkit expansion in PR #26419 (yoniebans) added the anchored drill-down,
bookends, and sort. This module merges all of that into a single calling
shape with no mode parameter, no summary LLM path, and explicit scroll
support.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

# Sources that are excluded from session browsing/searching by default.
# Third-party integrations tag their sessions with HERMES_SESSION_SOURCE=tool
# so they don't clutter the user's session history.
_HIDDEN_SESSION_SOURCES = ("tool",)
_DEMOTED_SESSION_SOURCES = ("cron",)
_DISCOVER_SCAN_LIMIT = 300
_IMPLICIT_SCOPE = "__implicit__"
_VALID_SCOPES = {"current_chat", "current_route", "global"}


def _format_timestamp(ts: Union[int, float, str, None]) -> str:
    """Convert a Unix timestamp (float/int) or ISO string to a human-readable date.

    Returns "unknown" for None, str(ts) if conversion fails.
    """
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime
                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        logging.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logging.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


def _resolve_to_parent(db, session_id: str) -> str:
    """Resolve a session lineage root only through the DB's scope-aware contract."""
    if not session_id:
        return session_id
    resolver = getattr(db, "resolve_session_lineage_root", None)
    if callable(resolver):
        try:
            return resolver(session_id)
        except Exception as e:
            logging.debug("scope-aware lineage resolution failed for %s: %s", session_id, e, exc_info=True)
    return session_id


def _compression_root(db, session_id: str) -> str:
    if not session_id:
        return session_id
    resolver = getattr(db, "resolve_session_lineage_root", None)
    if callable(resolver):
        try:
            return resolver(session_id, compression_only=True)
        except TypeError:
            try:
                return resolver(session_id)
            except Exception:
                logging.debug("compression lineage resolution failed for %s", session_id, exc_info=True)
        except Exception:
            logging.debug("compression lineage resolution failed for %s", session_id, exc_info=True)
    return session_id


def _same_compression_lineage(db, left_session_id: str, right_session_id: str) -> bool:
    if not left_session_id or not right_session_id:
        return False
    return _compression_root(db, left_session_id) == _compression_root(db, right_session_id)


def _order_for_recall(raw_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable-sort FTS rows so scheduled automation cannot crowd out chats."""
    return sorted(
        raw_results,
        key=lambda r: 1 if (r.get("source") or "") in _DEMOTED_SESSION_SOURCES else 0,
    )


def _shape_message(m: Dict[str, Any], anchor_id: Optional[int] = None) -> Dict[str, Any]:
    """Slim a message row for the tool response. Keeps content even if empty."""
    entry = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": m.get("content"),
        "timestamp": m.get("timestamp"),
    }
    if m.get("tool_name"):
        entry["tool_name"] = m.get("tool_name")
    if m.get("tool_calls"):
        entry["tool_calls"] = m.get("tool_calls")
    if m.get("tool_call_id"):
        entry["tool_call_id"] = m.get("tool_call_id")
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    # Strip None values to keep payload tight, but always keep content
    # (absent content is meaningful — tool-call-only assistant turns).
    return {k: v for k, v in entry.items() if v is not None or k in ("content",)}


def _scope_filter_kwargs(
    scope: str,
    current_conversation_scope_id: Optional[str] = None,
    current_route_partition_key: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Translate tool scope selection into DB filter kwargs.

    Gateway callers with runtime scope default to ``current_chat``. Local/CLI
    callers without injected runtime scope keep the historical global default.
    """
    scope_norm = _effective_scope(
        scope,
        current_conversation_scope_id=current_conversation_scope_id,
    )
    if scope_norm == "global":
        return {}
    if scope_norm == "current_route":
        return {
            "conversation_scope_id": current_conversation_scope_id,
            "route_partition_key": current_route_partition_key,
        }
    return {"conversation_scope_id": current_conversation_scope_id}


def _scope_error(
    scope: str,
    current_conversation_scope_id: Optional[str] = None,
    current_route_partition_key: Optional[str] = None,
    current_platform_account_id: Optional[str] = None,
) -> Optional[str]:
    scope_norm = _effective_scope(
        scope,
        current_conversation_scope_id=current_conversation_scope_id,
    )
    if scope_norm == "global":
        return None
    if scope_norm == "current_chat":
        if not current_conversation_scope_id:
            return "current_chat requires a runtime conversation scope"
        return None
    if not current_conversation_scope_id or not current_route_partition_key:
        return "current_route requires a runtime conversation and route scope"
    return None


def _effective_scope(
    scope: str,
    *,
    current_conversation_scope_id: Optional[str] = None,
) -> str:
    if scope is None or scope == _IMPLICIT_SCOPE:
        return "current_chat" if current_conversation_scope_id else "global"
    return scope if scope in _VALID_SCOPES else "current_chat"


def _scope_provenance(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "conversation_scope_id": meta.get("conversation_scope_id"),
        "route_partition_key": meta.get("route_partition_key"),
        "scope_assignment_status": meta.get("scope_assignment_status"),
    }


def _list_recent_sessions(
    db,
    limit: int,
    current_session_id: str = None,
    scope: str = "current_chat",
    current_conversation_scope_id: Optional[str] = None,
    current_route_partition_key: Optional[str] = None,
) -> str:
    """Return metadata for the most recent sessions (no LLM calls, no FTS5)."""
    try:
        scope_kwargs = _scope_filter_kwargs(
            scope,
            current_conversation_scope_id=current_conversation_scope_id,
            current_route_partition_key=current_route_partition_key,
        )
        sessions = db.list_sessions_rich(
            limit=limit + 5,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            order_by_last_active=True,
            **scope_kwargs,
        )  # fetch extra so we can skip current

        current_root = _resolve_to_parent(db, current_session_id) if current_session_id else None

        results = []
        for s in sessions:
            sid = s.get("id", "")
            if current_root and (sid == current_root or sid == current_session_id):
                continue
            # Skip child / delegation sessions
            if s.get("parent_session_id"):
                continue
            results.append({
                "session_id": sid,
                "title": s.get("title") or None,
                "source": s.get("source", ""),
                "started_at": s.get("started_at", ""),
                "last_active": s.get("last_active", ""),
                "message_count": s.get("message_count", 0),
                "preview": s.get("preview", ""),
                **_scope_provenance(s),
            })
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "browse",
            "results": results,
            "count": len(results),
            "message": f"Showing {len(results)} most recent sessions. Pass a query= to search, or session_id+around_message_id to scroll.",
        }, ensure_ascii=False)
    except Exception as e:
        logging.error("Error listing recent sessions: %s", e, exc_info=True)
        return tool_error(f"Failed to list recent sessions: {e}", success=False)


def _scroll(
    db,
    session_id: str,
    around_message_id: int,
    window: int = 5,
    current_session_id: str = None,
    scope: str = "current_chat",
    current_conversation_scope_id: Optional[str] = None,
    current_route_partition_key: Optional[str] = None,
) -> str:
    """Scroll shape: return a window of messages centered on an anchor.

    No FTS5, no bookends — just the slice. The discovery shape's lineage
    fixup is preserved: if the anchor doesn't live in the named session
    but does live in a child session in the same lineage, rebind silently.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return tool_error("scroll requires session_id", success=False)
    session_id = session_id.strip()

    try:
        around_message_id = int(around_message_id)
    except (TypeError, ValueError):
        return tool_error("scroll requires integer around_message_id", success=False)

    # Window clamp [1, 20]
    if not isinstance(window, int):
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 5
    window = max(1, min(window, 20))

    # Session existence check
    try:
        session_meta = db.get_session(session_id) or {}
    except Exception as e:
        logging.debug("get_session failed for %s: %s", session_id, e, exc_info=True)
        session_meta = {}
    if not session_meta:
        return tool_error(f"session_id not found: {session_id}", success=False)

    scope_norm = scope if scope in {"current_chat", "current_route", "global"} else "current_chat"
    if scope_norm != "global":
        target_scope = session_meta.get("conversation_scope_id")
        target_route = session_meta.get("route_partition_key")
        if current_conversation_scope_id and target_scope != current_conversation_scope_id:
            return tool_error(
                "scroll rejected: target session is outside the current conversation scope",
                success=False,
            )
        if session_meta.get("scope_assignment_status") != "scoped":
            return tool_error(
                "scroll rejected: target session is not scoped to the current conversation",
                success=False,
            )
        if (
            scope_norm == "current_route"
            and current_route_partition_key
            and target_route != current_route_partition_key
        ):
            return tool_error(
                "scroll rejected: target session is outside the current route scope",
                success=False,
            )

    owning = None
    try:
        conn = getattr(db, "_conn", None)
        if conn is not None:
            row = conn.execute(
                "SELECT session_id FROM messages WHERE id = ?",
                (around_message_id,),
            ).fetchone()
            owning = row[0] if row else None
    except Exception as e:
        logging.debug("owning-session lookup failed: %s", e, exc_info=True)
        owning = None

    if current_session_id and owning == current_session_id:
        return tool_error(
            "scroll rejected: anchor lives in the current session (already in your active context)",
            success=False,
        )

    # Fetch the window
    try:
        view = db.get_messages_around(
            session_id,
            around_message_id,
            window=window,
            **_scope_filter_kwargs(
                scope,
                current_conversation_scope_id=current_conversation_scope_id,
                current_route_partition_key=current_route_partition_key,
            ),
        )
    except Exception as e:
        logging.error("get_messages_around failed: %s", e, exc_info=True)
        return tool_error(f"failed to load messages: {e}", success=False)

    messages = view.get("window") or []

    # Lineage rebind: caller may have paired a parent session_id with a
    # message id that lives in a descendant (compaction / delegation creates
    # child sessions). Locate the real owning session and refetch.
    rebind_warning = None
    if not messages:
        if owning and owning != session_id:
            if not _same_compression_lineage(db, session_id, owning):
                return tool_error(
                    "around_message_id belongs to a session outside the compatible compression lineage",
                    success=False,
                )
            else:
                owning_meta = {}
                try:
                    owning_meta = db.get_session(owning) or {}
                except Exception:
                    owning_meta = {}
                if scope_norm != "global":
                    owning_scope = owning_meta.get("conversation_scope_id")
                    owning_route = owning_meta.get("route_partition_key")
                    if current_conversation_scope_id and owning_scope != current_conversation_scope_id:
                        return tool_error(
                            "scroll rejected: owning session is outside the current conversation scope",
                            success=False,
                        )
                    if owning_meta.get("scope_assignment_status") != "scoped":
                        return tool_error(
                            "scroll rejected: owning session is not scoped to the current conversation",
                            success=False,
                        )
                    if (
                        scope_norm == "current_route"
                        and current_route_partition_key
                        and owning_route != current_route_partition_key
                    ):
                        return tool_error(
                            "scroll rejected: owning session is outside the current route scope",
                            success=False,
                        )
                try:
                    rebind_view = db.get_messages_around(
                        owning,
                        around_message_id,
                        window=window,
                        **_scope_filter_kwargs(
                            scope,
                            current_conversation_scope_id=current_conversation_scope_id,
                            current_route_partition_key=current_route_partition_key,
                        ),
                    )
                    messages = rebind_view.get("window") or []
                    if messages:
                        view = rebind_view
                        rebind_warning = (
                            f"around_message_id {around_message_id} lives in {owning} "
                            f"(child of {session_id}); rebound transparently"
                        )
                        try:
                            session_meta = owning_meta or db.get_session(owning) or session_meta
                        except Exception:
                            pass
                        session_id = owning
                except Exception as e:
                    logging.debug("rebind get_messages_around failed: %s", e, exc_info=True)

    if not messages:
        return tool_error(
            f"around_message_id {around_message_id} not in session_id {session_id}",
            success=False,
        )

    response = {
        "success": True,
        "mode": "scroll",
        "session_id": session_id,
        "around_message_id": around_message_id,
        "session_meta": {
            "when": _format_timestamp(session_meta.get("started_at")),
            "source": session_meta.get("source"),
            "model": session_meta.get("model"),
            "title": session_meta.get("title"),
            **_scope_provenance(session_meta),
        },
        "window": window,
        "messages": [_shape_message(m, anchor_id=around_message_id) for m in messages],
        "messages_before": view.get("messages_before", 0),
        "messages_after": view.get("messages_after", 0),
    }
    if rebind_warning:
        response["warning"] = rebind_warning
    return json.dumps(response, ensure_ascii=False)


def _normalize_title_query(query: str) -> str:
    """Strip common quoting the model may include around a remembered title."""
    return query.strip().strip("`'\"")


def _title_match_result(
    db,
    query: str,
    current_session_id: Optional[str],
    current_lineage_root: Optional[str],
    scope: str,
    current_conversation_scope_id: Optional[str],
    current_route_partition_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    title_query = _normalize_title_query(query)
    if not title_query:
        return None

    scope_kwargs = _scope_filter_kwargs(
        scope,
        current_conversation_scope_id=current_conversation_scope_id,
        current_route_partition_key=current_route_partition_key,
    )
    try:
        session_id = db.resolve_session_by_title(title_query, **scope_kwargs)
    except Exception:
        logging.debug("resolve_session_by_title failed for %r", title_query, exc_info=True)
        return None
    if not session_id:
        return None

    lineage_root = _resolve_to_parent(db, session_id)
    if current_session_id and session_id == current_session_id:
        return None
    if current_lineage_root and lineage_root == current_lineage_root:
        return None

    try:
        hit_meta = db.get_session(session_id) or {}
    except Exception:
        logging.debug("get_session failed for title match %s", session_id, exc_info=True)
        hit_meta = {}
    if hit_meta.get("source") in _HIDDEN_SESSION_SOURCES:
        return None

    try:
        root_meta = db.get_session(lineage_root) or {}
    except Exception:
        root_meta = {}
    session_meta = hit_meta or root_meta

    try:
        messages = db.get_messages(session_id)
    except Exception:
        logging.debug("get_messages failed for title match %s", session_id, exc_info=True)
        messages = []

    anchor_id = messages[0].get("id") if messages else None
    if anchor_id is not None:
        try:
            view = db.get_anchored_view(
                session_id,
                anchor_id,
                window=5,
                bookend=3,
                **scope_kwargs,
            )
        except Exception:
            logging.debug(
                "get_anchored_view failed for title match %s/%s",
                session_id,
                anchor_id,
                exc_info=True,
            )
            view = {}
    else:
        view = {}

    entry = {
        "session_id": session_id,
        "when": _format_timestamp(session_meta.get("started_at")),
        "source": session_meta.get("source", "unknown"),
        "model": session_meta.get("model") or "unknown",
        "title": session_meta.get("title") or title_query,
        "matched_role": "session_title",
        "match_message_id": anchor_id,
        "snippet": f"Session title matched: {session_meta.get('title') or title_query}",
        "conversation_scope_id": session_meta.get("conversation_scope_id"),
        "route_partition_key": session_meta.get("route_partition_key"),
        "scope_assignment_status": session_meta.get("scope_assignment_status"),
        "bookend_start": [_shape_message(m) for m in (view.get("bookend_start") or messages[:3])],
        "messages": [_shape_message(m, anchor_id=anchor_id) for m in (view.get("window") or messages[:5])],
        "bookend_end": [_shape_message(m) for m in (view.get("bookend_end") or messages[-3:])],
        "messages_before": view.get("messages_before", 0),
        "messages_after": view.get("messages_after", max(len(messages) - 5, 0)),
        "_lineage_root": lineage_root,
    }
    if lineage_root and lineage_root != session_id:
        entry["parent_session_id"] = lineage_root
    return entry


def _discover(
    db,
    query: str,
    role_filter: Optional[List[str]],
    limit: int,
    sort: Optional[str],
    current_session_id: str = None,
    scope: str = "current_chat",
    current_conversation_scope_id: Optional[str] = None,
    current_route_partition_key: Optional[str] = None,
) -> str:
    """Discovery shape: FTS5 + anchored window + bookends per hit. Single call."""
    role_list = role_filter if role_filter else ["user", "assistant"]
    current_lineage_root = _resolve_to_parent(db, current_session_id) if current_session_id else None
    title_result = _title_match_result(
        db,
        query,
        current_session_id,
        current_lineage_root,
        scope,
        current_conversation_scope_id,
        current_route_partition_key,
    )

    try:
        scope_kwargs = _scope_filter_kwargs(
            scope,
            current_conversation_scope_id=current_conversation_scope_id,
            current_route_partition_key=current_route_partition_key,
        )
        raw_results = db.search_messages(
            query=query,
            role_filter=role_list,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            limit=_DISCOVER_SCAN_LIMIT,
            offset=0,
            sort=sort,
            **scope_kwargs,
        )
    except Exception as e:
        logging.error("FTS5 search failed: %s", e, exc_info=True)
        return tool_error(f"Search failed: {e}", success=False)

    raw_results = _order_for_recall(raw_results)

    if not raw_results and not title_result:
        return json.dumps({
            "success": True,
            "mode": "discover",
            "query": query,
            "results": [],
            "count": 0,
            "message": "No matching sessions found.",
        }, ensure_ascii=False)

    # Dedupe by lineage. Keep the raw owning session_id on the surviving
    # row — only that pairs validly with the FTS5 match id for the anchored
    # window. parent_session_id is exposed separately when different.
    seen_sessions = {}
    results = []
    if title_result:
        title_lineage = title_result.pop("_lineage_root", None)
        if title_lineage:
            seen_sessions[title_lineage] = {"_title_only": True}
        results.append(title_result)

    for r in raw_results:
        if len(seen_sessions) >= limit:
            break
        raw_sid = r["session_id"]
        resolved_sid = _resolve_to_parent(db, raw_sid)
        # Skip the current session lineage
        if current_lineage_root and resolved_sid == current_lineage_root:
            continue
        if current_session_id and raw_sid == current_session_id:
            continue
        if resolved_sid not in seen_sessions:
            row = dict(r)
            row["_lineage_root"] = resolved_sid
            seen_sessions[resolved_sid] = row
        if len(seen_sessions) >= limit:
            break

    for lineage_root, match_info in seen_sessions.items():
        if match_info.get("_title_only"):
            continue
        hit_sid = match_info.get("session_id") or lineage_root
        msg_id = match_info.get("id")
        try:
            view = db.get_anchored_view(
                hit_sid,
                msg_id,
                window=5,
                bookend=3,
                **_scope_filter_kwargs(
                    scope,
                    current_conversation_scope_id=current_conversation_scope_id,
                    current_route_partition_key=current_route_partition_key,
                ),
            )
        except Exception as e:
            logging.warning("get_anchored_view failed for %s/%s: %s", hit_sid, msg_id, e, exc_info=True)
            continue

        try:
            session_meta = db.get_session(lineage_root) or {}
        except Exception:
            session_meta = {}

        entry = {
            "session_id": hit_sid,
            "when": _format_timestamp(
                session_meta.get("started_at") or match_info.get("session_started")
            ),
            "source": session_meta.get("source") or match_info.get("source", "unknown"),
            "model": session_meta.get("model") or match_info.get("model") or "unknown",
            "title": session_meta.get("title") or None,
            "matched_role": match_info.get("role"),
            "match_message_id": msg_id,
            "snippet": match_info.get("snippet") or "",
            "conversation_scope_id": match_info.get("conversation_scope_id"),
            "route_partition_key": match_info.get("route_partition_key"),
            "scope_assignment_status": match_info.get("scope_assignment_status"),
            "bookend_start": [_shape_message(m) for m in (view.get("bookend_start") or [])],
            "messages": [_shape_message(m, anchor_id=msg_id) for m in (view.get("window") or [])],
            "bookend_end": [_shape_message(m) for m in (view.get("bookend_end") or [])],
            "messages_before": view.get("messages_before", 0),
            "messages_after": view.get("messages_after", 0),
        }
        if lineage_root and lineage_root != hit_sid:
            entry["parent_session_id"] = lineage_root
        results.append(entry)

    return json.dumps({
        "success": True,
        "mode": "discover",
        "query": query,
        "results": results,
        "count": len(results),
        "sessions_searched": len(seen_sessions),
    }, ensure_ascii=False)


def session_search(
    query: str = "",
    role_filter: str = None,
    limit: int = 3,
    db=None,
    current_session_id: str = None,
    current_conversation_scope_id: str = None,
    current_route_partition_key: str = None,
    current_platform_account_id: str = None,
    scope: str = _IMPLICIT_SCOPE,
    # Scroll shape
    session_id: str = None,
    around_message_id: int = None,
    window: int = 5,
    # Discovery shape
    sort: str = None,
) -> str:
    """Single-shape tool. Mode inferred from which args are set.

    Discovery: pass ``query``.
    Scroll:    pass ``session_id`` + ``around_message_id``.
    Browse:    pass nothing.

    Scroll wins over discovery when both are set — the agent has explicitly
    asked for a slice of a known session.
    """
    if db is None:
        try:
            from hermes_state import SessionDB
            db = SessionDB()
        except Exception:
            logging.debug("SessionDB unavailable for session_search", exc_info=True)
            from hermes_state import format_session_db_unavailable
            return tool_error(format_session_db_unavailable(), success=False)

    scope_norm = _effective_scope(
        scope,
        current_conversation_scope_id=current_conversation_scope_id,
    )

    scope_failure = _scope_error(
        scope_norm,
        current_conversation_scope_id=current_conversation_scope_id,
        current_route_partition_key=current_route_partition_key,
        current_platform_account_id=current_platform_account_id,
    )
    if scope_failure:
        return tool_error(scope_failure, success=False)

    # Scroll shape takes precedence — explicit anchor beats any query.
    if (isinstance(session_id, str) and session_id.strip()) and around_message_id is not None:
        return _scroll(
            db=db,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            current_session_id=current_session_id,
            scope=scope_norm,
            current_conversation_scope_id=current_conversation_scope_id,
            current_route_partition_key=current_route_partition_key,
        )

    # Limit clamp [1, 10]
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 3
    limit = max(1, min(limit, 10))

    # Browse shape: no query → recent sessions.
    if not query or not isinstance(query, str) or not query.strip():
        return _list_recent_sessions(
            db,
            limit,
            current_session_id,
            scope=scope_norm,
            current_conversation_scope_id=current_conversation_scope_id,
            current_route_partition_key=current_route_partition_key,
        )

    # Parse role_filter
    role_list: Optional[List[str]] = None
    if isinstance(role_filter, str) and role_filter.strip():
        role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

    # Normalise sort
    sort_norm: Optional[str] = None
    if isinstance(sort, str):
        candidate = sort.strip().lower()
        if candidate in ("newest", "oldest"):
            sort_norm = candidate

    return _discover(
        db=db,
        query=query.strip(),
        role_filter=role_list,
        limit=limit,
        sort=sort_norm,
        current_session_id=current_session_id,
        scope=scope_norm,
        current_conversation_scope_id=current_conversation_scope_id,
        current_route_partition_key=current_route_partition_key,
    )


def check_session_search_requirements() -> bool:
    """Requires the SQLite state database."""
    try:
        from hermes_state import DEFAULT_DB_PATH
        return DEFAULT_DB_PATH.parent.exists()
    except ImportError:
        return False


SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search past sessions stored in the local session DB, or scroll inside one. "
        "FTS5-backed retrieval over the SQLite message store. No LLM calls — every "
        "shape returns actual messages from the DB.\n\n"
        "THREE CALLING SHAPES\n\n"
        "  1) DISCOVERY — pass `query`:\n"
        "     session_search(query=\"auth refactor\", limit=3)\n"
        "     Runs FTS5, dedupes hits by session lineage, returns the top N sessions. "
        "Each result carries:\n"
        "       - session_id, title, when, source\n"
        "       - snippet: FTS5-highlighted match excerpt\n"
        "       - bookend_start: first 3 user+assistant messages of the session "
        "(the goal / kickoff)\n"
        "       - messages: ±5 messages around the FTS5 match, with the anchor message "
        "flagged (the hit in context)\n"
        "       - bookend_end: last 3 user+assistant messages of the session "
        "(the resolution / decisions)\n"
        "       - match_message_id, messages_before, messages_after\n"
        "     Bookends + window together let you reconstruct goal → match → resolution "
        "without paying for the whole transcript.\n\n"
        "  2) SCROLL — pass `session_id` + `around_message_id`:\n"
        "     session_search(session_id=\"...\", around_message_id=12345, window=10)\n"
        "     Returns a window of ±`window` messages centered on the anchor. No FTS5, "
        "no bookends — just the slice. Use after a discovery call when you need more "
        "context than the ±5 default window.\n"
        "       - To scroll FORWARD: pass messages[-1].id back as around_message_id.\n"
        "       - To scroll BACKWARD: pass messages[0].id back as around_message_id.\n"
        "       - The boundary message appears in both windows — orientation marker.\n"
        "       - When messages_before or messages_after is < window, you're at the "
        "start or end of the session.\n\n"
        "  3) BROWSE — no args:\n"
        "     session_search()\n"
        "     Returns recent sessions chronologically: titles, previews, timestamps. "
        "Use when the user asks \"what was I working on\" without naming a topic.\n\n"
        "FTS5 SYNTAX\n\n"
        "  AND is the default — multi-word queries require all terms. Use OR explicitly "
        "for broader recall (`alpha OR beta OR gamma`), quoted phrases for exact match "
        "(`\"docker networking\"`), boolean (`python NOT java`), or prefix wildcards "
        "(`deploy*`).\n\n"
        "WHEN TO USE\n\n"
        "  Reach for this on any \"what did we do about X\" / \"where did we leave Y\" / "
        "\"find the session where Z\" question — before gh, web search, or filesystem "
        "inspection. The session DB carries what was said when; external tools show "
        "current world state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query (discovery shape). Keywords, phrases, or boolean "
                    "expressions to find in past sessions. Omit to browse recent "
                    "sessions. Ignored when session_id + around_message_id are set "
                    "(scroll shape)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Discovery shape only. Max sessions to return (default 3, max 10). "
                    "Bump to 5–10 when the topic likely spans several sessions and you "
                    "want to pick the right one to scroll into."
                ),
                "default": 3,
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": (
                    "Discovery shape only. Temporal bias on top of FTS5 ranking. Omit "
                    "to keep relevance-only ordering (suitable for exploratory recall — "
                    "\"what do we know about X\"). Set 'newest' for recency-shaped "
                    "questions (\"where did we leave X\"). Set 'oldest' for "
                    "origin-shaped questions (\"how did X start\"). Ignored in scroll "
                    "and browse shapes."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["current_chat", "current_route", "global"],
                "default": "current_chat",
                "description": (
                    "Recall boundary. Defaults to current_chat, which searches only "
                    "the current gateway conversation scope when the runtime provides "
                    "one. current_route narrows further to the current topic/thread "
                    "route. Use global only when the user explicitly asks to search "
                    "across chats or all history. The runtime injects the actual "
                    "current scope; do not invent conversation ids."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Scroll shape. Session to read inside. Use the session_id returned "
                    "from a prior discovery call. Must be paired with "
                    "around_message_id."
                ),
            },
            "around_message_id": {
                "type": "integer",
                "description": (
                    "Scroll shape. Message id to center the window on. From a discovery "
                    "result use match_message_id, or any id seen in a prior window. To "
                    "scroll forward pass the last window message's id; to scroll "
                    "backward pass the first."
                ),
            },
            "window": {
                "type": "integer",
                "description": (
                    "Scroll shape only. Messages to return on each side of the anchor "
                    "(anchor itself always included). Clamped to [1, 20]. Default 5."
                ),
                "default": 5,
            },
            "role_filter": {
                "type": "string",
                "description": (
                    "Optional. Comma-separated roles to include. Discovery defaults to "
                    "'user,assistant' (tool output is usually noise). Pass "
                    "'user,assistant,tool' to include tool output (debugging tool "
                    "behaviour) or 'tool' to search tool output only."
                ),
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="session_search",
    toolset="session_search",
    schema=SESSION_SEARCH_SCHEMA,
    handler=lambda args, **kw: session_search(
        query=args.get("query") or "",
        role_filter=args.get("role_filter"),
        limit=args.get("limit", 3),
        session_id=args.get("session_id"),
        around_message_id=args.get("around_message_id"),
        window=args.get("window", 5),
        sort=args.get("sort"),
        scope=args["scope"] if "scope" in args else _IMPLICIT_SCOPE,
        db=kw.get("db"),
        current_session_id=kw.get("current_session_id"),
        current_conversation_scope_id=kw.get("current_conversation_scope_id"),
        current_route_partition_key=kw.get("current_route_partition_key"),
        current_platform_account_id=kw.get("current_platform_account_id"),
    ),
    check_fn=check_session_search_requirements,
    emoji="🔍",
)

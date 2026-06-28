"""Per-turn context preparation helpers.

This module keeps the turn-boundary setup that must happen before the next
API request is assembled.  The current extraction is intentionally small:
it restores the primary runtime, refreshes the MCP tool snapshot if needed,
and sanitizes the caller's user message payloads.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from agent.message_sanitization import _sanitize_surrogates

logger = logging.getLogger(__name__)


def build_turn_context(
    agent,
    user_message: Any,
    *,
    persist_user_message: Optional[Any] = None,
) -> Tuple[Any, Optional[Any]]:
    """Prepare the current turn's user-facing payloads.

    The helper is cache-safe: it runs at a turn boundary, before the request
    prefix for the turn is assembled.  That makes it the right place to let
    late-arriving MCP servers land in the next request snapshot without
    mutating an in-flight prefix.
    """
    agent._restore_primary_runtime()

    try:
        from tools.mcp_tool import has_registered_mcp_tools, refresh_agent_mcp_tools
        from tools.skill_provenance import is_background_review

        if not getattr(agent, "_skip_mcp_refresh", False) and not is_background_review():
            if has_registered_mcp_tools():
                refresh_agent_mcp_tools(agent, quiet_mode=True)
    except Exception:
        logger.debug("between-turns MCP tool refresh skipped", exc_info=True)

    if isinstance(user_message, str):
        user_message = _sanitize_surrogates(user_message)
    if isinstance(persist_user_message, str):
        persist_user_message = _sanitize_surrogates(persist_user_message)

    return user_message, persist_user_message

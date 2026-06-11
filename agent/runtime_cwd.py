"""Single source of truth for the agent working directory.

``TERMINAL_CWD`` carries the configured working directory for gateway/cron
entrypoints. Multi-session gateways can override it per async task with the
session contextvar below so prompt text, context-file discovery, terminal
tools, and file tools agree on the same logical cwd.
"""

import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def resolve_agent_cwd() -> Path:
    """Return an existing cwd for user-facing environment hints."""
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    """Return the configured cwd for context-file discovery, if any."""
    override = _session_cwd_override()
    if override:
        return Path(override).expanduser()
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    return Path(raw).expanduser() if raw else None

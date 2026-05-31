"""Shared persistence-result contract for run_agent/gateway diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PERSISTENCE_ATTEMPTED = "attempted"
PERSISTENCE_OK = "ok"
PERSISTENCE_ROW_IDS_BY_MESSAGE_INDEX = "row_ids_by_message_index"
PERSISTENCE_ASSISTANT_ROW_ID = "assistant_message_row_id"
PERSISTENCE_CANONICAL_ASSISTANT_ID = "canonical_assistant_message_id"
PERSISTENCE_ASSISTANT_CONTENT_SHA256 = "assistant_content_sha256"
PERSISTENCE_FAILURE_CLASS = "failure_class"
PERSISTENCE_STAGE = "stage"
PERSISTENCE_MESSAGE_INDEX = "message_index"
PERSISTENCE_ROLE = "role"
PERSISTENCE_SANITIZED_REASON = "sanitized_reason"

PERSISTENCE_NO_SESSION_DB = "no_session_db"
PERSISTENCE_DB_APPEND_FAILED = "db_append_failed"
PERSISTENCE_DB_SESSION_UNAVAILABLE = "db_session_unavailable"
PERSISTENCE_NO_CURRENT_PROOF = "no_current_persistence"
PERSISTENCE_STAGE_SESSION_DB_UNAVAILABLE = "session_db_unavailable"
PERSISTENCE_STAGE_CREATE_SESSION = "create_session"
PERSISTENCE_STAGE_APPEND_MESSAGE = "append_message"
PERSISTENCE_STAGE_TURN_EXIT = "turn_exit"

_PERSISTENCE_PREFIX_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{10,}\b"),
    re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)(?<![/?&\w])"
        r"([A-Z0-9_]*(?:API_?KEY|SECRET|PASSWORD|AUTH)[A-Z0-9_]*\s*[:=]\s*)"
        r"[^&\s'\";,]+"
    ),
)
_PERSISTENCE_URL_RE = re.compile(r"\bhttps?://[^\s'\"<>]+")
_PERSISTENCE_URL_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "client_secret",
    "id_token",
    "jwt",
    "key",
    "password",
    "refresh_token",
    "secret",
    "signature",
    "sign",
    "sig",
    "token",
}
_PERSISTENCE_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token|"
    r"api[_-]?key|apikey|client[_-]?secret|password|auth|jwt|secret|"
    r"signature|sign|sig|key"
    r")$"
)
_PERSISTENCE_BARE_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![/?&\w])"
    r"([A-Z0-9_-]*(?:access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"token|api[_-]?key|apikey|client[_-]?secret|password|auth|jwt|secret|"
    r"signature|key)[A-Z0-9_-]*)(\s*[:=]\s*)([^&\s'\";,]+)"
)
_PERSISTENCE_QUERY_PARAM_SECRET_RE = re.compile(
    r"(?i)([?&][A-Z0-9_-]*(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"auth[_-]?token|token|api[_-]?key|apikey|client[_-]?secret|password|auth|jwt|"
    r"secret|signature|key)[A-Z0-9_-]*=)([^&\s'\";,]+)"
)
_PERSISTENCE_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/])/(?:[^/\s:?]+/)+[^/\s:?]+"
)
_PERSISTENCE_WINDOWS_PATH_RE = re.compile(
    r"(?i)\b[A-Z]:(?:\\\\|\\)(?:(?!\s).)+"
)
_PERSISTENCE_HTTP_METHOD_BEFORE_PATH_RE = re.compile(
    r"(?i)(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+$"
)
_PERSISTENCE_NON_SECRET_ASSIGNMENT_VALUES = {
    "await",
    "async",
    "break",
    "case",
    "class",
    "const",
    "continue",
    "def",
    "else",
    "false",
    "for",
    "function",
    "if",
    "let",
    "new",
    "none",
    "null",
    "return",
    "true",
    "var",
    "while",
    "yield",
}


def _persistence_key_value_secret_replacement(match: re.Match) -> str:
    key, separator, value = match.group(1), match.group(2), match.group(3)
    if value.strip().lower() in _PERSISTENCE_NON_SECRET_ASSIGNMENT_VALUES:
        return match.group(0)
    return f"{key}{separator}***"


def _persistence_query_secret_replacement(match: re.Match) -> str:
    value = match.group(2)
    suffix = ""
    while value and value[-1] in ".)]":
        suffix = value[-1] + suffix
        value = value[:-1]
    return f"{match.group(1)}***{suffix}"


def _sanitize_persistence_url(match: re.Match) -> str:
    raw_url = match.group(0)
    trailing = ""
    while raw_url and raw_url[-1] in ".,);]":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return match.group(0)
    if not parts.query:
        return match.group(0)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    sanitized_pairs = []
    for key, value in query_pairs:
        if key.lower() in _PERSISTENCE_URL_SECRET_QUERY_KEYS or _PERSISTENCE_SECRET_KEY_RE.search(key):
            sanitized_pairs.append((key, "***"))
            changed = True
        else:
            sanitized_pairs.append((key, value))
    if not changed:
        return match.group(0)
    sanitized_query = urlencode(sanitized_pairs, doseq=True, safe="*")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, sanitized_query, parts.fragment)
    ) + trailing


def sanitize_persistence_failure_reason(exc: BaseException) -> str:
    """Return a short failure reason safe for durable persistence contracts.

    This is intentionally narrower than the global log redactor: persistence
    proof diagnostics must never expose credentials or local paths, but ordinary
    source code snippets, web URLs, and HTTP access-log request targets remain
    diagnosable.
    """
    reason = f"{type(exc).__name__}: {exc}"
    reason = _PERSISTENCE_URL_RE.sub(_sanitize_persistence_url, reason)
    reason = _PERSISTENCE_QUERY_PARAM_SECRET_RE.sub(
        _persistence_query_secret_replacement,
        reason,
    )
    for pattern in _PERSISTENCE_PREFIX_SECRET_PATTERNS:
        reason = pattern.sub(
            lambda m: (m.group(1) + "***") if m.lastindex else "***",
            reason,
        )
    reason = _PERSISTENCE_BARE_KEY_VALUE_RE.sub(
        _persistence_key_value_secret_replacement,
        reason,
    )
    home = str(Path.home())
    if home:
        reason = reason.replace(home, "[home]")
    reason = _PERSISTENCE_POSIX_PATH_RE.sub(
        lambda m: (
            m.group(0)
            if _PERSISTENCE_HTTP_METHOD_BEFORE_PATH_RE.search(
                reason[max(0, m.start() - 16):m.start()]
            )
            else "[path]"
        ),
        reason,
    )
    reason = _PERSISTENCE_WINDOWS_PATH_RE.sub("[path]", reason)
    return reason[:300]

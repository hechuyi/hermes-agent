"""
Session management for the gateway.

Handles:
- Session context tracking (where messages come from)
- Session storage (conversations persisted to disk)
- Reset policy evaluation (when to start fresh)
- Dynamic system prompt injection (agent knows its context)
"""

import hashlib
import logging
import os
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


_FEISHU_LIVE_CONVERSATION_CHAT_TYPES = frozenset({"dm", "group", "thread", "forum"})


class InvalidLiveSessionSource(ValueError):
    """Stable failure for live inbound events that lack routing identity."""

    def __init__(self, reason: str, message: str):
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class SessionPersistenceError(RuntimeError):
    """Stable failure for gateway session persistence contract violations."""

    def __init__(
        self,
        failure_class: str,
        message: str,
        *,
        stage: str,
        action: str,
        role: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ):
        sanitized_reason = None
        if cause is not None:
            try:
                from hermes_cli.persistence_contract import sanitize_persistence_failure_reason

                sanitized_reason = sanitize_persistence_failure_reason(cause)
            except Exception:
                sanitized_reason = type(cause).__name__
        detail = message
        if sanitized_reason:
            detail = f"{message}: {sanitized_reason}"
        super().__init__(
            f"{failure_class}: stage={stage} action={action}"
            + (f" role={role}" if role else "")
            + f" {detail}"
        )
        self.failure_class = failure_class
        self.stage = stage
        self.action = action
        self.role = role
        self.sanitized_reason = sanitized_reason


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


# ---------------------------------------------------------------------------
# PII redaction helpers
# ---------------------------------------------------------------------------

def _hash_id(value: str) -> str:
    """Deterministic 12-char hex hash of an identifier."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _hash_sender_id(value: str) -> str:
    """Hash a sender ID to ``user_<12hex>``."""
    return f"user_{_hash_id(value)}"


def _hash_chat_id(value: str) -> str:
    """Hash the numeric portion of a chat ID, preserving platform prefix.

    ``telegram:12345`` → ``telegram:<hash>``
    ``12345``          → ``<hash>``
    """
    colon = value.find(":")
    if colon > 0:
        prefix = value[:colon]
        return f"{prefix}:{_hash_id(value[colon + 1:])}"
    return _hash_id(value)


from .config import (
    Platform,
    GatewayConfig,
    SessionResetPolicy,  # noqa: F401 — re-exported via gateway/__init__.py
    HomeChannel,
)
from utils import atomic_replace


@dataclass
class SessionSource:
    """
    Describes where a message originated from.
    
    This information is used to:
    1. Route responses back to the right place
    2. Inject context into the system prompt
    3. Track origin for cron job delivery
    """
    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"  # "dm", "group", "channel", "thread"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None  # For forum topics, Discord threads, etc.
    chat_topic: Optional[str] = None  # Channel topic/description (Discord, Slack)
    user_id_alt: Optional[str] = None  # Platform-specific stable alt ID (Signal UUID, Feishu union_id)
    chat_id_alt: Optional[str] = None  # Signal group internal ID
    is_bot: bool = False  # True when the message author is a bot/webhook (Discord)
    guild_id: Optional[str] = None  # Discord guild / Slack workspace / Matrix server scope
    parent_chat_id: Optional[str] = None  # Parent channel when chat_id refers to a thread
    message_id: Optional[str] = None  # ID of the triggering message (for pin/reply/react)
    
    @property
    def description(self) -> str:
        """Human-readable description of the source."""
        if self.platform == Platform.LOCAL:
            return "CLI terminal"
        
        parts = []
        if self.chat_type == "dm":
            parts.append(f"DM with {self.user_name or self.user_id or 'user'}")
        elif self.chat_type == "group":
            parts.append(f"group: {self.chat_name or self.chat_id}")
        elif self.chat_type == "channel":
            parts.append(f"channel: {self.chat_name or self.chat_id}")
        else:
            parts.append(self.chat_name or self.chat_id)
        
        if self.thread_id:
            parts.append(f"thread: {self.thread_id}")
        
        return ", ".join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        d = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "chat_topic": self.chat_topic,
        }
        if self.user_id_alt:
            d["user_id_alt"] = self.user_id_alt
        if self.chat_id_alt:
            d["chat_id_alt"] = self.chat_id_alt
        if self.guild_id:
            d["guild_id"] = self.guild_id
        if self.parent_chat_id:
            d["parent_chat_id"] = self.parent_chat_id
        if self.message_id:
            d["message_id"] = self.message_id
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSource":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            chat_name=data.get("chat_name"),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            thread_id=data.get("thread_id"),
            chat_topic=data.get("chat_topic"),
            user_id_alt=data.get("user_id_alt"),
            chat_id_alt=data.get("chat_id_alt"),
            guild_id=data.get("guild_id"),
            parent_chat_id=data.get("parent_chat_id"),
            message_id=data.get("message_id"),
        )
    


@dataclass
class SessionContext:
    """
    Full context for a session, used for dynamic system prompt injection.
    
    The agent receives this information to understand:
    - Where messages are coming from
    - What platforms are available
    - Where it can deliver scheduled task outputs
    """
    source: SessionSource
    connected_platforms: List[Platform]
    home_channels: Dict[Platform, HomeChannel]
    shared_multi_user_session: bool = False
    
    # Session metadata
    session_key: str = ""
    session_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    conversation_scope_id: Optional[str] = None
    platform_account_id: Optional[str] = None
    route_partition_key: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "connected_platforms": [p.value for p in self.connected_platforms],
            "home_channels": {
                p.value: hc.to_dict() for p, hc in self.home_channels.items()
            },
            "shared_multi_user_session": self.shared_multi_user_session,
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "conversation_scope_id": self.conversation_scope_id,
            "platform_account_id": self.platform_account_id,
            "route_partition_key": self.route_partition_key,
        }


_PII_SAFE_PLATFORMS = frozenset({Platform.FEISHU})
"""Runtime platforms where user/chat IDs can be safely redacted in prompts."""


def build_session_context_prompt(
    context: SessionContext,
    *,
    redact_pii: bool = False,
) -> str:
    """
    Build the dynamic system prompt section that tells the agent about its context.

    This is injected into the system prompt so the agent knows:
    - Where messages are coming from
    - What platforms are connected
    - Where it can deliver scheduled task outputs

    When *redact_pii* is True **and** the source platform is in
    ``_PII_SAFE_PLATFORMS``, phone numbers are stripped and user/chat IDs
    are replaced with deterministic hashes before being sent to the LLM.
    Platforms that need raw IDs for native mentions should not be marked safe.
    Routing still uses the original values (they stay in SessionSource).
    """
    # Only apply redaction on platforms where IDs aren't needed for mentions.
    # Check both the hardcoded set (builtins) and the plugin registry.
    _is_pii_safe = context.source.platform in _PII_SAFE_PLATFORMS
    if not _is_pii_safe:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(context.source.platform.value)
            if entry and entry.pii_safe:
                _is_pii_safe = True
        except Exception:
            pass
    redact_pii = redact_pii and _is_pii_safe
    lines = [
        "## Current Session Context",
        "",
    ]

    # Source info
    platform_name = context.source.platform.value.title()
    if context.source.platform == Platform.LOCAL:
        lines.append(f"**Source:** {platform_name} (the machine running this agent)")
    else:
        # Build a description that respects PII redaction
        src = context.source
        if redact_pii:
            # Build a safe description without raw IDs
            _uname = src.user_name or (
                _hash_sender_id(src.user_id) if src.user_id else "user"
            )
            _cname = src.chat_name or _hash_chat_id(src.chat_id)
            if src.chat_type == "dm":
                desc = f"DM with {_uname}"
            elif src.chat_type == "group":
                desc = f"group: {_cname}"
            elif src.chat_type == "channel":
                desc = f"channel: {_cname}"
            else:
                desc = _cname
        else:
            desc = src.description
        lines.append(f"**Source:** {platform_name} ({desc})")

    # Channel topic (if available - provides context about the channel's purpose)
    if context.source.chat_topic:
        lines.append(f"**Channel Topic:** {context.source.chat_topic}")

    # User identity.
    # In shared multi-user sessions (shared threads OR shared non-thread groups
    # when group_sessions_per_user=False), multiple users contribute to the same
    # conversation.  Don't pin a single user name in the system prompt — it
    # changes per-turn and would bust the prompt cache.  Instead, note that
    # this is a multi-user session; individual sender names are prefixed on
    # each user message by the gateway.
    if context.shared_multi_user_session:
        session_label = "Multi-user thread" if context.source.thread_id else "Multi-user session"
        lines.append(
            f"**Session type:** {session_label} — messages are prefixed "
            "with [sender name]. Multiple users may participate."
        )
    elif context.source.user_name:
        lines.append(f"**User:** {context.source.user_name}")
    elif context.source.user_id:
        uid = context.source.user_id
        if redact_pii:
            uid = _hash_sender_id(uid)
        lines.append(f"**User ID:** {uid}")

    # Connected platforms
    platforms_list = ["local (files on this machine)"]
    for p in context.connected_platforms:
        if p != Platform.LOCAL:
            platforms_list.append(f"{p.value}: Connected ✓")

    lines.append(f"**Connected Platforms:** {', '.join(platforms_list)}")

    # Home channels
    if context.home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, home in context.home_channels.items():
            hc_id = _hash_chat_id(home.chat_id) if redact_pii else home.chat_id
            lines.append(f"  - {platform.value}: {home.name} (ID: {hc_id})")

    # Delivery options for scheduled tasks
    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")

    from hermes_constants import display_hermes_home

    # Origin delivery
    if context.source.platform == Platform.LOCAL:
        lines.append("- `\"origin\"` → Local output (saved to files)")
    else:
        _origin_label = context.source.chat_name or (
            _hash_chat_id(context.source.chat_id) if redact_pii else context.source.chat_id
        )
        lines.append(f"- `\"origin\"` → Back to this chat ({_origin_label})")

    # Local always available
    lines.append(
        f"- `\"local\"` → Save to local files only ({display_hermes_home()}/cron/output/)"
    )

    # Platform home channels
    for platform, home in context.home_channels.items():
        lines.append(f"- `\"{platform.value}\"` → Home channel ({home.name})")

    # Note about explicit targeting
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID.*")

    return "\n".join(lines)


@dataclass
class SessionEntry:
    """
    Entry in the session store.
    
    Maps a session key to its current session ID and metadata.
    """
    session_key: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    
    # Origin metadata for delivery routing
    origin: Optional[SessionSource] = None
    
    # Display metadata
    display_name: Optional[str] = None
    platform: Optional[Platform] = None
    chat_type: str = "dm"
    
    # Token tracking
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_status: str = "unknown"
    
    # Last API-reported prompt tokens (for accurate compression pre-check)
    last_prompt_tokens: int = 0
    
    # Set when a session was created because the previous one expired;
    # consumed once by the message handler to inject a notice into context
    was_auto_reset: bool = False
    auto_reset_reason: Optional[str] = None  # "idle" or "daily"
    reset_had_activity: bool = False  # whether the expired session had any messages

    # Set by reset_session() when the user explicitly sends /new or /reset.
    # Consumed once by _handle_message_with_agent to trigger topic/channel
    # skill re-injection on the first message of the new session.  We can't
    # reuse was_auto_reset for this because that flag fires the "session
    # expired due to inactivity" user-facing notice and a misleading
    # context-note prepend — both wrong for an explicit manual reset.
    # See issue #6508.
    is_fresh_reset: bool = False
    
    # Set by the background expiry watcher after it finalizes an expired
    # session (invoking on_session_finalize hooks and evicting the cached
    # agent).  Persisted to sessions.json so the flag survives gateway
    # restarts — prevents redundant finalization runs.
    expiry_finalized: bool = False

    # When True the next call to get_or_create_session() will auto-reset
    # this session (create a new session_id) so the user starts fresh.
    # Set by /stop to break stuck-resume loops (#7536).
    suspended: bool = False

    # When True the session was interrupted by a gateway restart/shutdown
    # drain timeout, but recovery is still expected.  Unlike ``suspended``,
    # ``resume_pending`` preserves the existing session_id on next access —
    # the user stays on the same transcript and the agent auto-continues
    # from where it left off.  Cleared after the next successful turn.
    # Escalation to ``suspended`` is handled by the existing
    # ``.restart_failure_counts`` stuck-loop counter (#7536), not by a
    # parallel counter on this entry.
    resume_pending: bool = False
    resume_reason: Optional[str] = None  # e.g. "restart_timeout"
    last_resume_marked_at: Optional[datetime] = None
    conversation_scope_id: Optional[str] = None
    platform_account_id: Optional[str] = None
    route_partition_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "display_name": self.display_name,
            "platform": self.platform.value if self.platform else None,
            "chat_type": self.chat_type,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_status": self.cost_status,
            "expiry_finalized": self.expiry_finalized,
            "suspended": self.suspended,
            "resume_pending": self.resume_pending,
            "resume_reason": self.resume_reason,
            "last_resume_marked_at": (
                self.last_resume_marked_at.isoformat()
                if self.last_resume_marked_at
                else None
            ),
            "is_fresh_reset": self.is_fresh_reset,
            "was_auto_reset": self.was_auto_reset,
            "auto_reset_reason": self.auto_reset_reason,
            "reset_had_activity": self.reset_had_activity,
            "conversation_scope_id": self.conversation_scope_id,
            "platform_account_id": self.platform_account_id,
            "route_partition_key": self.route_partition_key,
        }
        if self.origin:
            result["origin"] = self.origin.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionEntry":
        origin = None
        if "origin" in data and data["origin"]:
            origin = SessionSource.from_dict(data["origin"])
        
        platform = None
        if data.get("platform"):
            try:
                platform = Platform(data["platform"])
            except ValueError as e:
                logger.debug("Unknown platform value %r: %s", data["platform"], e)

        last_resume_marked_at = None
        _lrma = data.get("last_resume_marked_at")
        if _lrma:
            try:
                last_resume_marked_at = datetime.fromisoformat(_lrma)
            except (TypeError, ValueError):
                last_resume_marked_at = None

        return cls(
            session_key=data["session_key"],
            session_id=data["session_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            origin=origin,
            display_name=data.get("display_name"),
            platform=platform,
            chat_type=data.get("chat_type", "dm"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_write_tokens=data.get("cache_write_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            last_prompt_tokens=data.get("last_prompt_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd", 0.0),
            cost_status=data.get("cost_status", "unknown"),
            expiry_finalized=data.get("expiry_finalized", data.get("memory_flushed", False)),
            suspended=data.get("suspended", False),
            resume_pending=data.get("resume_pending", False),
            resume_reason=data.get("resume_reason"),
            last_resume_marked_at=last_resume_marked_at,
            is_fresh_reset=data.get("is_fresh_reset", False),
            was_auto_reset=data.get("was_auto_reset", False),
            auto_reset_reason=data.get("auto_reset_reason"),
            reset_had_activity=data.get("reset_had_activity", False),
            conversation_scope_id=data.get("conversation_scope_id"),
            platform_account_id=data.get("platform_account_id"),
            route_partition_key=data.get("route_partition_key"),
        )


def is_shared_multi_user_session(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> bool:
    """Return True when a non-DM session is shared across participants.

    Mirrors the isolation rules in :func:`build_session_key`:
      - DMs are never shared.
      - Threads are shared unless ``thread_sessions_per_user`` is True.
      - Non-thread group/channel sessions are shared unless
        ``group_sessions_per_user`` is True (default: True = isolated).
    """
    if source.chat_type == "dm":
        return False
    if source.thread_id:
        return not thread_sessions_per_user
    return not group_sessions_per_user


def build_session_key(
    source: SessionSource,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    require_conversation_identity: bool = False,
) -> str:
    """Build a deterministic session key from a message source.

    This is the single source of truth for session key construction.

    DM rules:
      - DMs include chat_id when present, so each private conversation is isolated.
      - thread_id further differentiates threaded DMs within the same DM chat.
      - Without chat_id, user_id_alt/user_id isolates synthetic or non-standard
        DM sources before falling back to thread_id.
      - Without any identifier, DMs share a single per-platform sink.

    Group/channel rules:
      - chat_id identifies the parent group/channel.
      - user_id/user_id_alt isolates participants within that parent chat when available when
        ``group_sessions_per_user`` is enabled (default: enabled).
      - thread_id differentiates threads within that parent chat.  When
        ``thread_sessions_per_user`` is False (default), threads are *shared* across all
        participants — user_id is NOT appended, so every user in the thread
        shares a single session.  This is the expected UX for threaded
        conversations.
      - Without participant identifiers, or when isolation is disabled, messages fall back to one
        shared session per chat.
      - Without identifiers, messages fall back to one session per platform/chat_type.
    """
    platform = source.platform.value
    if (
        require_conversation_identity
        and source.platform == Platform.FEISHU
        and source.chat_type in _FEISHU_LIVE_CONVERSATION_CHAT_TYPES
        and not source.chat_id
    ):
        raise InvalidLiveSessionSource(
            "feishu_missing_chat_identity",
            "live Feishu inbound source requires chat_id or equivalent conversation identity",
        )
    if source.chat_type == "dm":
        dm_chat_id = source.chat_id
        if dm_chat_id:
            if source.thread_id:
                return f"agent:main:{platform}:dm:{dm_chat_id}:{source.thread_id}"
            return f"agent:main:{platform}:dm:{dm_chat_id}"
        dm_participant_id = source.user_id_alt or source.user_id
        if dm_participant_id:
            if source.thread_id:
                return f"agent:main:{platform}:dm:{dm_participant_id}:{source.thread_id}"
            return f"agent:main:{platform}:dm:{dm_participant_id}"
        if source.thread_id:
            return f"agent:main:{platform}:dm:{source.thread_id}"
        return f"agent:main:{platform}:dm"

    participant_id = source.user_id_alt or source.user_id
    key_parts = ["agent:main", platform, source.chat_type]

    if source.chat_id:
        key_parts.append(source.chat_id)
    if source.thread_id:
        key_parts.append(source.thread_id)

    # In threads, default to shared sessions (all participants see the same
    # conversation).  Per-user isolation only applies when explicitly enabled
    # via thread_sessions_per_user, or when there is no thread (regular group).
    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False

    if isolate_user and participant_id:
        key_parts.append(str(participant_id))

    return ":".join(key_parts)


class SessionStore:
    """
    Manages session storage and retrieval.
    
    Uses SQLite (via SessionDB) for session metadata and message transcripts.
    SQLite is mandatory for live gateway persistence.
    """
    
    def __init__(self, sessions_dir: Path, config: GatewayConfig,
                 has_active_processes_fn=None):
        self.sessions_dir = sessions_dir
        self.config = config
        self._entries: Dict[str, SessionEntry] = {}
        self._loaded = False
        self._lock = threading.Lock()
        self._has_active_processes_fn = has_active_processes_fn
        
        # Initialize SQLite session database
        self._db = None
        try:
            from hermes_state import SessionDB
            self._db = SessionDB()
        except Exception as e:
            raise SessionPersistenceError(
                "session_db_init_failed",
                "SQLite session store is unavailable",
                stage="session_db_init",
                action="initialize_session_store",
                cause=e,
            ) from None
    
    def _ensure_loaded(self) -> None:
        """Load sessions index from disk if not already loaded."""
        with self._lock:
            self._ensure_loaded_locked()

    def _ensure_loaded_locked(self) -> None:
        """Load sessions index from disk. Must be called with self._lock held."""
        if self._loaded:
            return

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        if sessions_file.exists():
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, entry_data in data.items():
                        try:
                            self._entries[key] = SessionEntry.from_dict(entry_data)
                        except (ValueError, KeyError):
                            # Skip entries with unknown/removed platform values
                            continue
            except Exception as e:
                print(f"[gateway] Warning: Failed to load sessions: {e}")

        self._loaded = True
    
    def _save(self) -> None:
        """Save sessions index to disk (kept for session key -> ID mapping)."""
        import tempfile
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        data = {key: entry.to_dict() for key, entry in self._entries.items()}
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.sessions_dir), suffix=".tmp", prefix=".sessions_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, sessions_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug("Could not remove temp file %s: %s", tmp_path, e)
            raise
    
    def _generate_session_key(self, source: SessionSource) -> str:
        """Generate a session key from a source."""
        if (
            source.platform == Platform.FEISHU
            and source.chat_type in _FEISHU_LIVE_CONVERSATION_CHAT_TYPES
            and not source.chat_id
        ):
            raise InvalidLiveSessionSource(
                "feishu_missing_chat_identity",
                "live Feishu inbound source requires chat_id or equivalent conversation identity",
            )
        if hasattr(self.config, "session_key_for_source"):
            return self.config.session_key_for_source(source)
        return build_session_key(
            source,
            group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
            require_conversation_identity=True,
        )

    def _scope_kwargs_for_source(self, source: Optional[SessionSource], session_key: str) -> Dict[str, Any]:
        """Return DB scope metadata for the current source.

        Feishu inbound scope is fail-closed: missing account, chat, route, or
        participant evidence is ambiguous, not legacy-global. Other/unknown
        sources remain legacy-compatible.
        """
        if source is None or source.platform != Platform.FEISHU:
            return {
                "scope_assignment_status": "legacy_unscoped",
                "conversation_scope_id": None,
                "route_session_key_snapshot": None,
                "route_partition_key": None,
            }
        feishu_ambiguous = {
            "scope_assignment_status": "ambiguous",
            "conversation_scope_id": None,
            "route_session_key_snapshot": None,
            "route_partition_key": None,
        }
        from gateway.conversation_scope import (
            conversation_identity,
            feishu_platform_account_id,
            route_partition_key,
        )

        platform_cfg = (getattr(self.config, "platforms", {}) or {}).get(Platform.FEISHU)
        account_id = feishu_platform_account_id(config=platform_cfg)
        if not account_id:
            return feishu_ambiguous
        isolation = (
            self.config.effective_session_isolation(source.platform)
            if hasattr(self.config, "effective_session_isolation")
            else {
                "group_sessions_per_user": getattr(self.config, "group_sessions_per_user", True),
                "thread_sessions_per_user": getattr(self.config, "thread_sessions_per_user", False),
            }
        )
        route_key = route_partition_key(source, **isolation)
        ident = conversation_identity(
            source,
            platform_account_id=account_id,
            **isolation,
            group_conversation_scope_per_user=getattr(
                self.config, "group_conversation_scope_per_user", False
            ),
            thread_conversation_scope_per_user=getattr(
                self.config, "thread_conversation_scope_per_user", False
            ),
        )
        if not ident:
            return feishu_ambiguous
        return {
            "scope_assignment_status": "scoped",
            "conversation_scope_id": ident.id,
            "route_session_key_snapshot": session_key,
            "route_partition_key": route_key,
            "platform_account_id": account_id,
            "_conversation_scope_identity": ident,
        }

    def _entry_accepts_scope_backfill(self, entry: SessionEntry, scope_kwargs: Dict[str, Any]) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        new_account = scope_kwargs.get("platform_account_id")
        if not new_scope or not new_route:
            return False
        current_scope = getattr(entry, "conversation_scope_id", None)
        current_route = getattr(entry, "route_partition_key", None)
        current_account = getattr(entry, "platform_account_id", None)
        return current_scope == new_scope and current_route == new_route and current_account == new_account

    def _entry_matches_scope(self, entry: SessionEntry, scope_kwargs: Dict[str, Any]) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        new_account = scope_kwargs.get("platform_account_id")
        if not new_scope or not new_route:
            return False
        current_scope = getattr(entry, "conversation_scope_id", None)
        current_route = getattr(entry, "route_partition_key", None)
        current_account = getattr(entry, "platform_account_id", None)
        return current_scope == new_scope and current_route == new_route and current_account == new_account

    def _entry_has_conflicting_scope(self, entry: SessionEntry, scope_kwargs: Dict[str, Any]) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        new_account = scope_kwargs.get("platform_account_id")
        if not new_scope or not new_route:
            return False
        current_scope = getattr(entry, "conversation_scope_id", None)
        current_route = getattr(entry, "route_partition_key", None)
        current_account = getattr(entry, "platform_account_id", None)
        return (
            (current_scope is not None and current_scope != new_scope)
            or (current_route is not None and current_route != new_route)
            or (current_account is not None and current_account != new_account)
        )

    def _db_row_requires_detach(self, db_row: Dict[str, Any], scope_kwargs: Dict[str, Any]) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        if not new_scope or not new_route or not db_row:
            return False
        status = db_row.get("scope_assignment_status")
        db_scope = db_row.get("conversation_scope_id")
        db_route = db_row.get("route_partition_key")
        db_snapshot = db_row.get("route_session_key_snapshot")
        if status == "ambiguous":
            return True
        if status != "scoped":
            return True
        if not db_scope or not db_route or not db_snapshot:
            return True
        return (
            db_scope != new_scope
            or db_route != new_route
            or db_snapshot != scope_kwargs.get("route_session_key_snapshot")
        )

    def _legacy_entry_requires_scoped_detach(
        self,
        entry: SessionEntry,
        scope_kwargs: Dict[str, Any],
    ) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        if not new_scope or not new_route:
            return False
        return not self._entry_matches_scope(entry, scope_kwargs)

    def _apply_scope_to_entry(self, entry: SessionEntry, scope_kwargs: Dict[str, Any]) -> None:
        entry.conversation_scope_id = scope_kwargs.get("conversation_scope_id")
        entry.platform_account_id = scope_kwargs.get("platform_account_id")
        entry.route_partition_key = scope_kwargs.get("route_partition_key")

    def _session_id_has_ambiguous_legacy_routes(
        self,
        *,
        session_id: str,
        session_key: str,
        scope_kwargs: Dict[str, Any],
    ) -> bool:
        new_scope = scope_kwargs.get("conversation_scope_id")
        new_route = scope_kwargs.get("route_partition_key")
        if not session_id or not new_scope or not new_route:
            return False
        for key, entry in self._entries.items():
            if key == session_key or entry.session_id != session_id:
                continue
            current_scope = getattr(entry, "conversation_scope_id", None)
            current_route = getattr(entry, "route_partition_key", None)
            if current_scope and current_scope != new_scope:
                return True
            if current_route and current_route != new_route:
                return True
            if not current_scope and not current_route:
                return True
        return False

    def _new_session_entry(
        self,
        *,
        session_key: str,
        source: SessionSource,
        now: datetime,
        was_auto_reset: bool = False,
        auto_reset_reason: Optional[str] = None,
        reset_had_activity: bool = False,
    ) -> tuple[SessionEntry, Dict[str, Any]]:
        session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        scope_kwargs = self._scope_kwargs_for_source(source, session_key)
        entry = SessionEntry(
            session_key=session_key,
            session_id=session_id,
            created_at=now,
            updated_at=now,
            origin=source,
            display_name=source.chat_name,
            platform=source.platform,
            chat_type=source.chat_type,
            was_auto_reset=was_auto_reset,
            auto_reset_reason=auto_reset_reason,
            reset_had_activity=reset_had_activity,
            conversation_scope_id=scope_kwargs.get("conversation_scope_id"),
            platform_account_id=scope_kwargs.get("platform_account_id"),
            route_partition_key=scope_kwargs.get("route_partition_key"),
        )
        db_create_kwargs = {
            "session_id": session_id,
            "source": source.platform.value,
            "user_id": source.user_id,
            **self._db_scope_kwargs(scope_kwargs),
        }
        return entry, {
            "db_create_kwargs": db_create_kwargs,
            "scope_kwargs": scope_kwargs,
        }

    def _db_scope_kwargs(self, scope_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return {
            k: v
            for k, v in scope_kwargs.items()
            if k not in {"platform_account_id", "_conversation_scope_identity"}
        }

    def _upsert_scope_and_session_row(
        self,
        *,
        session_id: str,
        source: SessionSource,
        scope_kwargs: Dict[str, Any],
    ) -> None:
        if not self._db:
            raise SessionPersistenceError(
                "session_db_unavailable",
                "SQLite session store is unavailable",
                stage="scope_backfill",
                action="upsert_scope_and_session_row",
            )
        result = self._db.update_session_scope_if_missing(
            session_id=session_id,
            source=source.platform.value if source.platform else "unknown",
            user_id=source.user_id,
            **self._db_scope_kwargs(scope_kwargs),
        )
        if result in {"created", "updated", "unchanged"}:
            identity = scope_kwargs.get("_conversation_scope_identity")
            if identity is not None:
                self._db.upsert_conversation_scope(identity)
        return result

    def bind_session_to_scope_for_handoff(
        self,
        *,
        session_id: str,
        source: SessionSource,
        session_key: str,
    ) -> bool:
        """Legacy handoff scope binding is disabled.

        CLI-to-Feishu handoff must not rewrite a CLI or legacy transcript as a
        Feishu scoped transcript. Callers should switch only to sessions that
        already satisfy the destination scope/route guards.
        """
        return False
    
    def _is_session_expired(self, entry: SessionEntry) -> bool:
        """Check if a session has expired based on its reset policy.
        
        Works from the entry alone — no SessionSource needed.
        Used by the background expiry watcher to proactively flush memories.
        Sessions with active background processes are never considered expired.
        """
        if self._has_active_processes_fn:
            if self._has_active_processes_fn(entry.session_key):
                return False

        policy = self.config.get_reset_policy(
            platform=entry.platform,
            session_type=entry.chat_type,
        )

        if policy.mode == "none":
            return False

        now = _now()

        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return True

        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour,
                minute=0, second=0, microsecond=0,
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            if entry.updated_at < today_reset:
                return True

        return False

    def _should_reset(self, entry: SessionEntry, source: SessionSource) -> Optional[str]:
        """
        Check if a session should be reset based on policy.
        
        Returns the reset reason ("idle" or "daily") if a reset is needed,
        or None if the session is still valid.
        
        Sessions with active background processes are never reset.
        """
        if self._has_active_processes_fn:
            session_key = self._generate_session_key(source)
            if self._has_active_processes_fn(session_key):
                return None

        policy = self.config.get_reset_policy(
            platform=source.platform,
            session_type=source.chat_type
        )
        
        if policy.mode == "none":
            return None
        
        now = _now()
        
        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return "idle"
        
        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour, 
                minute=0, 
                second=0, 
                microsecond=0
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            
            if entry.updated_at < today_reset:
                return "daily"
        
        return None
    
    def has_any_sessions(self) -> bool:
        """Check if any sessions have ever been created (across all platforms).

        Uses the SQLite database as the source of truth because it preserves
        historical session records (ended sessions still count).  The in-memory
        ``_entries`` dict replaces entries on reset, so ``len(_entries)`` would
        stay at 1 for single-platform users — which is the bug this fixes.

        The current session is already in the DB by the time this is called
        (get_or_create_session runs first), so we check ``> 1``.
        """
        if self._db:
            try:
                return self._db.session_count() > 1
            except Exception:
                pass  # fall through to heuristic
        # Fallback: check if sessions.json was loaded with existing data.
        # This covers the rare case where the DB is unavailable.
        with self._lock:
            self._ensure_loaded_locked()
            return len(self._entries) > 1

    def get_or_create_session(
        self,
        source: SessionSource,
        force_new: bool = False
    ) -> SessionEntry:
        """
        Get an existing session or create a new one.

        Evaluates reset policy to determine if the existing session is stale.
        Creates a session record in SQLite when a new session starts.
        """
        session_key = self._generate_session_key(source)
        now = _now()

        # SQLite calls are made outside the lock to avoid holding it during I/O.
        # All _entries / _loaded mutations are protected by self._lock.
        db_end_session_id = None
        db_create_kwargs = None
        db_scope_backfill = None
        json_scope_backfill = None
        db_mark_ambiguous = None
        db_mark_ambiguous_and_detach = False
        db_row = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries and not force_new:
                entry = self._entries[session_key]

                # Auto-reset sessions marked as suspended (e.g. after /stop
                # broke a stuck loop — #7536).  ``suspended`` is the hard
                # forced-wipe signal and always wins over ``resume_pending``,
                # so repeated interrupted restarts that escalate via the
                # existing ``.restart_failure_counts`` stuck-loop counter
                # still converge to a clean slate.
                if entry.suspended:
                    reset_reason = "suspended"
                elif entry.resume_pending:
                    # Restart-interrupted session: preserve the session_id
                    # and return the existing entry so the transcript
                    # reloads intact.  ``resume_pending`` is cleared after
                    # the NEXT successful turn completes (not here), which
                    # means a re-interrupted retry keeps trying — the
                    # stuck-loop counter handles terminal escalation.
                    reset_reason = None
                else:
                    reset_reason = self._should_reset(entry, source)
                if not reset_reason:
                    entry.updated_at = now
                    scope_kwargs = self._scope_kwargs_for_source(source, session_key)
                    if self._db:
                        try:
                            db_row = self._db.get_session(entry.session_id) or {}
                        except Exception:
                            db_row = {}
                    if self._session_id_has_ambiguous_legacy_routes(
                        session_id=entry.session_id,
                        session_key=session_key,
                        scope_kwargs=scope_kwargs,
                    ):
                        db_mark_ambiguous = {
                            "session_id": entry.session_id,
                            "source": source.platform.value if source.platform else "unknown",
                            "user_id": source.user_id,
                        }
                        db_mark_ambiguous_and_detach = True
                        entry, create_data = self._new_session_entry(
                            session_key=session_key,
                            source=source,
                            now=now,
                        )
                        self._entries[session_key] = entry
                        db_create_kwargs = create_data["db_create_kwargs"]
                        scope_kwargs = create_data["scope_kwargs"]
                        existing_entry = entry
                    elif self._entry_has_conflicting_scope(entry, scope_kwargs):
                        entry, create_data = self._new_session_entry(
                            session_key=session_key,
                            source=source,
                            now=now,
                        )
                        self._entries[session_key] = entry
                        db_create_kwargs = create_data["db_create_kwargs"]
                        scope_kwargs = create_data["scope_kwargs"]
                        existing_entry = entry
                    elif self._db_row_requires_detach(db_row or {}, scope_kwargs):
                        entry, create_data = self._new_session_entry(
                            session_key=session_key,
                            source=source,
                            now=now,
                        )
                        self._entries[session_key] = entry
                        db_create_kwargs = create_data["db_create_kwargs"]
                        scope_kwargs = create_data["scope_kwargs"]
                        existing_entry = entry
                    elif self._legacy_entry_requires_scoped_detach(entry, scope_kwargs):
                        entry, create_data = self._new_session_entry(
                            session_key=session_key,
                            source=source,
                            now=now,
                        )
                        self._entries[session_key] = entry
                        db_create_kwargs = create_data["db_create_kwargs"]
                        scope_kwargs = create_data["scope_kwargs"]
                        existing_entry = entry
                    elif self._entry_accepts_scope_backfill(entry, scope_kwargs):
                        db_scope_backfill = {
                            "session_id": entry.session_id,
                            "source": source,
                            "scope_kwargs": scope_kwargs,
                        }
                        json_scope_backfill = {
                            "session_key": session_key,
                            "scope_kwargs": scope_kwargs,
                        }
                    elif self._entry_matches_scope(entry, scope_kwargs):
                        db_scope_backfill = {
                            "session_id": entry.session_id,
                            "source": source,
                            "scope_kwargs": scope_kwargs,
                        }
                    self._save()
                    if not db_mark_ambiguous_and_detach:
                        existing_entry = entry
                else:
                    # Session is being auto-reset.
                    was_auto_reset = True
                    auto_reset_reason = reset_reason
                    # Track whether the expired session had any real conversation
                    reset_had_activity = entry.total_tokens > 0
                    db_end_session_id = entry.session_id
                    existing_entry = None
            else:
                was_auto_reset = False
                auto_reset_reason = None
                reset_had_activity = False
                existing_entry = None

            if existing_entry is not None:
                pass
            else:
                # Create new session
                entry, create_data = self._new_session_entry(
                    session_key=session_key,
                    source=source,
                    now=now,
                    was_auto_reset=was_auto_reset,
                    auto_reset_reason=auto_reset_reason,
                    reset_had_activity=reset_had_activity,
                )
                scope_kwargs = create_data["scope_kwargs"]

                self._entries[session_key] = entry
                self._save()
                db_create_kwargs = create_data["db_create_kwargs"]

        # SQLite operations outside the lock
        if db_mark_ambiguous and self._db:
            try:
                self._db.mark_session_scope_ambiguous(**db_mark_ambiguous)
            except Exception as e:
                raise SessionPersistenceError(
                    "session_db_scope_mark_failed",
                    "failed to mark ambiguous session scope",
                    stage="scope_mark",
                    action="mark_session_scope_ambiguous",
                    cause=e,
                ) from None
            if not db_mark_ambiguous_and_detach:
                return existing_entry

        if db_scope_backfill:
            try:
                backfill_result = self._upsert_scope_and_session_row(**db_scope_backfill)
                if backfill_result in {"created", "updated", "unchanged"} and json_scope_backfill:
                    with self._lock:
                        self._ensure_loaded_locked()
                        entry_to_update = self._entries.get(json_scope_backfill["session_key"])
                        if entry_to_update and entry_to_update.session_id == db_scope_backfill["session_id"]:
                            self._apply_scope_to_entry(entry_to_update, json_scope_backfill["scope_kwargs"])
                            self._save()
                            existing_entry = entry_to_update
            except Exception as e:
                if isinstance(e, SessionPersistenceError):
                    raise
                raise SessionPersistenceError(
                    "session_db_scope_backfill_failed",
                    "failed to backfill session scope",
                    stage="scope_backfill",
                    action="upsert_scope_and_session_row",
                    cause=e,
                ) from None
            return existing_entry

        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_reset")
            except Exception as e:
                raise SessionPersistenceError(
                    "session_db_end_failed",
                    "failed to end previous session row",
                    stage="create_session",
                    action="end_session",
                    cause=e,
                ) from None

        if self._db and db_create_kwargs:
            try:
                identity = scope_kwargs.get("_conversation_scope_identity") if "scope_kwargs" in locals() else None
                if identity is not None:
                    self._db.upsert_conversation_scope(identity)
                self._db.create_session(**db_create_kwargs)
            except Exception as e:
                raise SessionPersistenceError(
                    "session_db_create_failed",
                    "failed to create SQLite session row",
                    stage="create_session",
                    action="create_session",
                    cause=e,
                ) from None

        return entry

    def update_session(
        self,
        session_key: str,
        last_prompt_tokens: int = None,
    ) -> None:
        """Update lightweight session metadata after an interaction."""
        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries:
                entry = self._entries[session_key]
                entry.updated_at = _now()
                if last_prompt_tokens is not None:
                    entry.last_prompt_tokens = last_prompt_tokens
                self._save()

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session as suspended so it auto-resets on next access.

        Used by ``/stop`` to prevent stuck sessions from being resumed
        after a gateway restart (#7536).  Returns True if the session
        existed and was marked.
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                self._entries[session_key].suspended = True
                self._save()
                return True
        return False

    def mark_resume_pending(
        self,
        session_key: str,
        reason: str = "restart_timeout",
    ) -> bool:
        """Mark a session as resumable after a restart interruption.

        Unlike ``suspend_session()``, this preserves the existing
        ``session_id`` and the transcript.  The next call to
        ``get_or_create_session()`` for this key returns the same entry
        so the user auto-resumes on the same conversation lane.

        Returns True if the session existed and was marked.
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                entry = self._entries[session_key]
                # Never override an explicit ``suspended`` — that is a hard
                # forced-wipe signal (from /stop or stuck-loop escalation).
                if entry.suspended:
                    return False
                entry.resume_pending = True
                entry.resume_reason = reason
                entry.last_resume_marked_at = _now()
                self._save()
                return True
        return False

    def clear_resume_pending(self, session_key: str) -> bool:
        """Clear the resume-pending flag after a successful resumed turn.

        Called from the gateway after ``run_conversation()`` returns a
        final response for a session that had ``resume_pending=True``,
        signalling that recovery succeeded.

        Returns True if a flag was cleared.
        """
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or not entry.resume_pending:
                return False
            old_resume_reason = entry.resume_reason
            old_last_resume_marked_at = entry.last_resume_marked_at
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            try:
                self._save()
            except Exception as e:
                entry.resume_pending = True
                entry.resume_reason = old_resume_reason
                entry.last_resume_marked_at = old_last_resume_marked_at
                raise SessionPersistenceError(
                    "session_index_clear_resume_pending_failed",
                    "failed to persist resume_pending clear",
                    stage="clear_resume_pending",
                    action="clear_resume_pending",
                    cause=e,
                ) from None
            return True

    def prune_old_entries(self, max_age_days: int) -> int:
        """Drop SessionEntry records older than max_age_days.

        Pruning is based on ``updated_at`` (last activity), not ``created_at``.
        A session that's been active within the window is kept regardless of
        how old it is.  Entries marked ``suspended`` are kept — the user
        explicitly paused them for later resume.  Entries held by an active
        process (via has_active_processes_fn) are also kept so long-running
        background work isn't orphaned.

        Pruning is functionally identical to a natural reset-policy expiry:
        the transcript in SQLite stays, but the session_key → session_id
        mapping is dropped and the user starts a fresh session on return.

        ``max_age_days <= 0`` disables pruning; returns 0 immediately.
        Returns the number of entries removed.
        """
        if max_age_days is None or max_age_days <= 0:
            return 0
        from datetime import timedelta

        cutoff = _now() - timedelta(days=max_age_days)
        removed_keys: list[str] = []

        with self._lock:
            self._ensure_loaded_locked()
            for key, entry in list(self._entries.items()):
                if entry.suspended:
                    continue
                # Never prune sessions with an active background process
                # attached — the user may still be waiting on output.
                # The callback is keyed by session_key (see process_registry.
                # has_active_for_session); passing session_id here used to
                # never match, so active sessions got pruned anyway.
                if self._has_active_processes_fn is not None:
                    try:
                        if self._has_active_processes_fn(entry.session_key):
                            continue
                    except Exception as exc:
                        logger.debug(
                            "has_active_processes_fn raised during prune for %s: %s",
                            entry.session_key, exc,
                        )
                if entry.updated_at < cutoff:
                    removed_keys.append(key)
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()

        if removed_keys:
            logger.info(
                "SessionStore pruned %d entries older than %d days",
                len(removed_keys), max_age_days,
            )
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """Mark recently-active sessions as resumable after an unexpected exit.

        Called on gateway startup after a crash or fast restart to preserve
        in-flight sessions instead of destroying their conversation history
        (#7536).  Only marks sessions updated within *max_age_seconds* to
        avoid touching long-idle sessions.  Sets ``resume_pending=True`` so
        the next incoming message on the same session_key auto-resumes from
        the existing transcript.

        Entries already flagged ``resume_pending=True`` are skipped.  Entries
        explicitly ``suspended=True`` (from /stop or stuck-loop escalation)
        are also skipped.  Terminal escalation for genuinely stuck sessions
        is still handled by the existing ``.restart_failure_counts`` counter
        (threshold 3), which runs after this method and sets ``suspended=True``.

        Returns the number of sessions marked resumable.
        """
        from datetime import timedelta

        cutoff = _now() - timedelta(seconds=max_age_seconds)
        count = 0
        with self._lock:
            self._ensure_loaded_locked()
            for entry in self._entries.values():
                if entry.resume_pending:
                    continue
                if not entry.suspended and entry.updated_at >= cutoff:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.last_resume_marked_at = _now()
                    count += 1
            if count:
                self._save()
        return count

    def reset_session(self, session_key: str, display_name: Optional[str] = None) -> Optional[SessionEntry]:
        """Force reset a session, creating a new session ID."""
        db_end_session_id = None
        db_create_kwargs = None
        new_entry = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]
            db_end_session_id = old_entry.session_id

            now = _now()
            session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            scope_kwargs = self._scope_kwargs_for_source(old_entry.origin, session_key)

            new_entry = SessionEntry(
                session_key=session_key,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=display_name if display_name is not None else old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
                is_fresh_reset=True,
                conversation_scope_id=scope_kwargs.get("conversation_scope_id"),
                platform_account_id=scope_kwargs.get("platform_account_id"),
                route_partition_key=scope_kwargs.get("route_partition_key"),
            )

            self._entries[session_key] = new_entry
            self._save()
            db_create_kwargs = {
                "session_id": session_id,
                "source": old_entry.platform.value if old_entry.platform else "unknown",
                "user_id": old_entry.origin.user_id if old_entry.origin else None,
                **{
                    k: v
                    for k, v in scope_kwargs.items()
                    if k not in {"platform_account_id", "_conversation_scope_identity"}
                },
            }

        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_reset")
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

        if self._db and db_create_kwargs:
            try:
                identity = scope_kwargs.get("_conversation_scope_identity") if "scope_kwargs" in locals() else None
                if identity is not None:
                    self._db.upsert_conversation_scope(identity)
                self._db.create_session(**db_create_kwargs)
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

        return new_entry

    def switch_session(self, session_key: str, target_session_id: str) -> Optional[SessionEntry]:
        """Switch a session key to point at an existing session ID.

        Used by ``/resume`` to restore a previously-named session.
        Ends the current session in SQLite (like reset), but instead of
        generating a fresh session ID, re-uses ``target_session_id`` so the
        old transcript is loaded on the next message. If the target session was
        previously ended, re-open it so gateway resume semantics match the CLI.
        """
        db_end_session_id = None
        new_entry = None
        target_scope_id = None
        target_platform_account_id = None
        target_route_key = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]

            # Don't switch if already on that session
            if old_entry.session_id == target_session_id:
                return old_entry

            db_end_session_id = old_entry.session_id
            if self._db:
                try:
                    target_row = self._db.get_session(target_session_id) or {}
                except Exception as e:
                    logger.debug("Session DB get_session failed during switch: %s", e)
                    target_row = {}
                target_scope_id = target_row.get("conversation_scope_id")
                target_route_key = target_row.get("route_partition_key")
                target_status = target_row.get("scope_assignment_status")
                current_scope_id = getattr(old_entry, "conversation_scope_id", None)
                current_route_key = getattr(old_entry, "route_partition_key", None)
                current_scope_id = current_scope_id if isinstance(current_scope_id, str) and current_scope_id else None
                current_route_key = current_route_key if isinstance(current_route_key, str) and current_route_key else None
                if target_scope_id and (not current_scope_id or not current_route_key):
                    return None
                if (current_scope_id or current_route_key) and (
                    target_status != "scoped" or not target_scope_id or not target_route_key
                ):
                    return None
                if current_scope_id and target_scope_id != current_scope_id:
                    return None
                if current_route_key and target_route_key != current_route_key:
                    return None
                if target_scope_id:
                    try:
                        scope_row = self._db.get_conversation_scope(target_scope_id)
                    except Exception as e:
                        logger.debug("Session DB get_conversation_scope failed during switch: %s", e)
                        scope_row = None
                    if scope_row:
                        target_platform_account_id = scope_row.get("platform_account_id")

            now = _now()
            new_entry = SessionEntry(
                session_key=session_key,
                session_id=target_session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
                conversation_scope_id=target_scope_id,
                platform_account_id=(
                    (target_platform_account_id or getattr(old_entry, "platform_account_id", None))
                    if target_scope_id
                    else None
                ),
                route_partition_key=target_route_key,
            )

            self._entries[session_key] = new_entry
            self._save()

        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_switch")
            except Exception as e:
                logger.debug("Session DB end_session failed: %s", e)

        if self._db:
            try:
                self._db.reopen_session(target_session_id)
            except Exception as e:
                logger.debug("Session DB reopen_session failed: %s", e)

        return new_entry

    def list_sessions(self, active_minutes: Optional[int] = None) -> List[SessionEntry]:
        """List all sessions, optionally filtered by activity."""
        with self._lock:
            self._ensure_loaded_locked()
            entries = list(self._entries.values())

        if active_minutes is not None:
            cutoff = _now() - timedelta(minutes=active_minutes)
            entries = [e for e in entries if e.updated_at >= cutoff]

        entries.sort(key=lambda e: e.updated_at, reverse=True)

        return entries
    
    def append_to_transcript(self, session_id: str, message: Dict[str, Any], skip_db: bool = False) -> None:
        """Append a message to a session's transcript (SQLite).

        Args:
            skip_db: When True, skip the SQLite write. Used when the agent
                     already persisted messages to SQLite via its own
                     _flush_messages_to_session_db(), preventing the
                     duplicate-write bug (#860).
        """
        if skip_db:
            return
        if not self._db:
            raise SessionPersistenceError(
                "session_db_unavailable",
                "SQLite session store is unavailable",
                stage="append_message",
                action="append_to_transcript",
                role=str(message.get("role", "unknown")),
            )
        try:
            self._db.append_message(
                session_id=session_id,
                role=message.get("role", "unknown"),
                content=message.get("content"),
                tool_name=message.get("tool_name"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                reasoning=message.get("reasoning") if message.get("role") == "assistant" else None,
                reasoning_content=message.get("reasoning_content") if message.get("role") == "assistant" else None,
                reasoning_details=message.get("reasoning_details") if message.get("role") == "assistant" else None,
                codex_reasoning_items=message.get("codex_reasoning_items") if message.get("role") == "assistant" else None,
                codex_message_items=message.get("codex_message_items") if message.get("role") == "assistant" else None,
                # Platform-side message id (yuanbao msg_id, telegram update_id, …).
                # Accept either explicit ``platform_message_id`` or the legacy
                # ``message_id`` key the JSONL transcript used.
                platform_message_id=(
                    message.get("platform_message_id") or message.get("message_id")
                ),
                observed=bool(message.get("observed")),
            )
        except Exception as e:
            raise SessionPersistenceError(
                "session_db_append_failed",
                "failed to append transcript message",
                stage="append_message",
                action="append_to_transcript",
                role=str(message.get("role", "unknown")),
                cause=e,
            ) from None
    
    def rewrite_transcript(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Replace the entire transcript for a session with new messages.

        Used by /retry, /undo, and /compress to persist modified conversation
        history. state.db is the canonical store.
        """
        if not self._db:
            raise SessionPersistenceError(
                "session_db_unavailable",
                "SQLite session store is unavailable",
                stage="rewrite_transcript",
                action="rewrite_transcript",
            )
        try:
            self._db.replace_messages(session_id, messages)
        except Exception as e:
            raise SessionPersistenceError(
                "session_db_rewrite_failed",
                "failed to rewrite transcript",
                stage="rewrite_transcript",
                action="rewrite_transcript",
                cause=e,
            ) from None

    def load_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages from a session's transcript.

        state.db is the canonical store. The legacy JSONL fallback was removed
        in spec 002 — pre-DB sessions on existing disks have already been
        migrated (their DB row holds the full message history).
        """
        if not self._db:
            raise SessionPersistenceError(
                "session_db_unavailable",
                "SQLite session store is unavailable",
                stage="load_transcript",
                action="load_transcript",
            )
        try:
            return self._db.get_messages_as_conversation(session_id)
        except Exception as e:
            raise SessionPersistenceError(
                "session_db_load_failed",
                "failed to load transcript",
                stage="load_transcript",
                action="load_transcript",
                cause=e,
            ) from None


def build_session_context(
    source: SessionSource,
    config: GatewayConfig,
    session_entry: Optional[SessionEntry] = None
) -> SessionContext:
    """
    Build a full session context from a source and config.
    
    This is used to inject context into the agent's system prompt.
    """
    connected = config.get_connected_platforms()
    
    home_channels = {}
    for platform in connected:
        home = config.get_home_channel(platform)
        if home:
            home_channels[platform] = home
    
    context = SessionContext(
        source=source,
        connected_platforms=connected,
        home_channels=home_channels,
        shared_multi_user_session=is_shared_multi_user_session(
            source,
            **(
                config.effective_session_isolation(source.platform)
                if hasattr(config, "effective_session_isolation")
                else {
                    "group_sessions_per_user": getattr(config, "group_sessions_per_user", True),
                    "thread_sessions_per_user": getattr(config, "thread_sessions_per_user", False),
                }
            ),
        ),
    )
    
    if session_entry:
        context.session_key = session_entry.session_key
        context.session_id = session_entry.session_id
        context.created_at = session_entry.created_at
        context.updated_at = session_entry.updated_at
        context.conversation_scope_id = getattr(session_entry, "conversation_scope_id", None)
        context.platform_account_id = getattr(session_entry, "platform_account_id", None)
        context.route_partition_key = getattr(session_entry, "route_partition_key", None)
    
    return context

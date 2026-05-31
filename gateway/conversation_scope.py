"""Canonical conversation-scope and route-partition identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from gateway.config import Platform
from gateway.whatsapp_identity import canonical_whatsapp_identifier


@dataclass(frozen=True)
class ConversationScopeIdentity:
    id: str
    canonical_key: str
    platform: str
    platform_account_id: str
    chat_type: str
    chat_id: str
    thread_id: Optional[str]
    participant_mode: str
    participant_id: Optional[str]


def _platform_value(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return platform.value if hasattr(platform, "value") else str(platform or "")


def _participant_id(source: Any) -> Optional[str]:
    participant = getattr(source, "user_id_alt", None) or getattr(source, "user_id", None)
    if participant is None:
        return None
    participant = str(participant)
    if getattr(source, "platform", None) == Platform.WHATSAPP:
        participant = canonical_whatsapp_identifier(participant) or participant
    return participant


def feishu_platform_account_id(
    *,
    app_id: Optional[str] = None,
    platform_account_id: Optional[str] = None,
    config: Any = None,
) -> Optional[str]:
    """Return the stable, storage-safe Feishu app account id when known."""
    if platform_account_id:
        return str(platform_account_id)
    if not app_id and config is not None:
        extra = getattr(config, "extra", {}) or {}
        platform_account_id = extra.get("platform_account_id")
        if platform_account_id:
            return str(platform_account_id)
        app_id = extra.get("app_id")
    if not app_id:
        return None
    return "feishu_app:" + hashlib.sha256(str(app_id).encode("utf-8")).hexdigest()[:16]


def conversation_identity(
    source: Any,
    *,
    platform_account_id: Optional[str] = None,
    app_id: Optional[str] = None,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    group_conversation_scope_per_user: bool = False,
    thread_conversation_scope_per_user: bool = False,
) -> Optional[ConversationScopeIdentity]:
    """Build the deterministic semantic conversation-scope identity.

    ``group_sessions_per_user`` and ``thread_sessions_per_user`` are accepted
    to keep call sites parallel with route partitioning, but semantic scope is
    shared by default.  Explicit ``*_conversation_scope_per_user`` flags are
    the only knobs that include the participant in the canonical key.
    """
    platform = _platform_value(source)
    if not platform_account_id and platform == Platform.FEISHU.value:
        platform_account_id = feishu_platform_account_id(app_id=app_id)
    if not platform_account_id:
        return None

    chat_type = str(getattr(source, "chat_type", "") or "dm")
    thread_id = getattr(source, "thread_id", None)
    if thread_id is not None:
        thread_id = str(thread_id)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if platform == Platform.FEISHU.value and not chat_id:
        return None
    if chat_type != "dm" and not chat_id:
        return None

    per_user = False
    if chat_type != "dm":
        per_user = (
            bool(thread_conversation_scope_per_user)
            if thread_id
            else bool(group_conversation_scope_per_user)
        )

    participant_id = _participant_id(source) if per_user else None
    if per_user and not participant_id:
        return None
    participant_mode = "per_user" if participant_id else "shared"

    canonical = {
        "version": 1,
        "platform": platform,
        "platform_account_id": str(platform_account_id),
        "chat_type": chat_type,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "participant_mode": participant_mode,
        "participant_id": participant_id,
    }
    canonical_key = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    scope_id = "cs_" + hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:32]
    return ConversationScopeIdentity(
        id=scope_id,
        canonical_key=canonical_key,
        platform=platform,
        platform_account_id=str(platform_account_id),
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        participant_mode=participant_mode,
        participant_id=participant_id,
    )


def route_partition_key(
    source: Any,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Stable route partition key mirroring ``gateway.session.build_session_key``."""
    platform = _platform_value(source)
    chat_type = str(getattr(source, "chat_type", "") or "dm")
    chat_id = getattr(source, "chat_id", None)
    thread_id = getattr(source, "thread_id", None)

    if chat_type == "dm":
        dm_chat_id = chat_id
        if getattr(source, "platform", None) == Platform.WHATSAPP:
            dm_chat_id = canonical_whatsapp_identifier(chat_id)
        if dm_chat_id:
            if thread_id:
                return f"agent:main:{platform}:dm:{dm_chat_id}:{thread_id}"
            return f"agent:main:{platform}:dm:{dm_chat_id}"
        if thread_id:
            return f"agent:main:{platform}:dm:{thread_id}"
        return f"agent:main:{platform}:dm"

    parts = ["agent:main", platform, chat_type]
    if chat_id:
        parts.append(str(chat_id))
    if thread_id:
        parts.append(str(thread_id))

    isolate_user = group_sessions_per_user
    if thread_id and not thread_sessions_per_user:
        isolate_user = False
    participant = _participant_id(source)
    if isolate_user and participant:
        parts.append(str(participant))
    return ":".join(parts)


def turn_control_key(conversation_scope_id: str, route_partition: str) -> str:
    material = f"{conversation_scope_id}{route_partition}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()

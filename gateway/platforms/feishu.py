"""
Feishu/Lark platform adapter.

Supports:
- WebSocket long connection and Webhook transport
- Direct-message and group @mention-gated text receive/send
- Inbound image/file/audio/media caching
- Gateway allowlist integration via FEISHU_ALLOWED_USERS
- Persistent dedup state across restarts
- Per-chat serial message processing (matches openclaw createChatQueue)
- Processing status reactions: Typing while working, removed on success,
  swapped for CrossMark on failure
- Reaction events routed as synthetic text events (matches openclaw)
- Interactive card button-click events routed as synthetic COMMAND events
- Webhook anomaly tracking (matches openclaw createWebhookAnomalyTracker)
- Verification token validation as second auth layer (matches openclaw)

Feishu identity model
---------------------
Feishu uses three user-ID tiers (official docs:
https://open.feishu.cn/document/home/user-identity-introduction/introduction):

  open_id  (ou_xxx)  — **App-scoped**.  The same person gets a different
                        open_id under each Feishu app.  Always available in
                        event payloads without extra permissions.
  user_id  (u_xxx)   — **Tenant-scoped**.  Stable within a company but
                        requires the ``contact:user.employee_id:readonly``
                        scope.  May not be present.
  union_id (on_xxx)  — **Developer-scoped**.  Same across all apps owned by
                        one developer/ISV.  Best cross-app stable ID.

For bots specifically:

  app_id              — The application's canonical credential identifier.
  bot open_id         — Returned by ``/bot/v3/info``.  This is the bot's own
                        open_id *within its app context* and is what Feishu
                        puts in ``mentions[].id.open_id`` when someone
                        @-mentions the bot.  Used for mention gating only.

In single-bot mode (what Hermes currently supports), open_id works as a
de-facto unique user identifier since there is only one app context.

Session-key participant isolation prefers ``union_id`` (via user_id_alt)
over ``open_id`` (via user_id) so that sessions stay stable if the same
user is seen through different apps in the future.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import hmac
import itertools
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gateway import hermes_tools_gateway_event
from gateway.conversation_scope import (
    conversation_identity,
    feishu_platform_account_id,
    route_partition_key,
)
from gateway.feishu_contracts import (
    ConversationContract,
    build_feishu_conversation_contract,
    feishu_hashed_ref,
)
from gateway.feishu_action_plan import (
    RenderPlan,
    RenderPlanPart,
    validate_render_plan,
)
from gateway.feishu_legacy_guard import (
    current_feishu_broker_context,
    feishu_broker_context,
)

try:
    from gateway import gateway_event_ledger
    from gateway.gateway_event_contract import GatewayEventResult
except ImportError:
    gateway_event_ledger = hermes_tools_gateway_event
    GatewayEventResult = hermes_tools_gateway_event.HermesToolsGatewayEventResult
# aiohttp/websockets are independent optional deps — import outside lark_oapi
# so they remain available for tests and webhook mode even if lark_oapi is missing.
try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

try:
    import lark_oapi as lark
    from lark_oapi.api.application.v6 import GetApplicationRequest
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetChatRequest,
        GetMessageRequest,
        GetMessageResourceRequest,
        P2ImMessageMessageReadV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
    )
    from lark_oapi.core import AccessTokenType, HttpMethod
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
    from lark_oapi.core.model import BaseRequest
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        P2CardActionTriggerResponse,
    )
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None  # type: ignore[assignment]
    CallBackCard = None  # type: ignore[assignment]
    P2CardActionTriggerResponse = None  # type: ignore[assignment]
    EventDispatcherHandler = None  # type: ignore[assignment]
    FeishuWSClient = None  # type: ignore[assignment]
    FEISHU_DOMAIN = None  # type: ignore[assignment]
    LARK_DOMAIN = None  # type: ignore[assignment]

FEISHU_WEBSOCKET_AVAILABLE = websockets is not None

_FEISHU_CURRENT_ADMISSION_CONTEXT: contextvars.ContextVar[Dict[str, Any] | None] = (
    contextvars.ContextVar("feishu_current_admission", default=None)
)
FEISHU_WEBHOOK_AVAILABLE = aiohttp is not None

from gateway.config import Platform, PlatformConfig
from gateway.session import InvalidLiveSessionSource, build_session_key
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    _reply_anchor_for_event,
    cache_document_from_bytes,
    cache_image_from_url,
    cache_audio_from_bytes,
    cache_image_from_bytes,
)
from gateway.status import acquire_scoped_lock, release_scoped_lock
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(^\s*---+\s*$)|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)|(<u>.+?</u>)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
# Detect markdown tables: a line starting with | followed by a separator line.
# Feishu post-type 'md' elements do not render tables, so we force text mode.
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_MENTION_RE = re.compile(r"@_user_\d+")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_POST_CONTENT_INVALID_RE = re.compile(r"content format of the post type is incorrect", re.IGNORECASE)
_FEISHU_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DELIVERY_FAILURE_CLASS_RE = re.compile(r"^[a-z0-9_-]{1,128}$")
_FEISHU_DESCRIPTOR_PATCH_PATH_RE = re.compile(r"^/open-apis/im/v1/messages/([A-Za-z0-9_]+)$")
_FEISHU_AMBIGUOUS_NON_ACCEPTANCE_CODES = frozenset(
    {
        99991400,
        99991663,
        99991664,
        230020,
        230021,
    }
)
# ---------------------------------------------------------------------------
# Media type sets and upload constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus", ".webm"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}
_DOCUMENT_MIME_TO_EXT = {mime: ext for ext, mime in SUPPORTED_DOCUMENT_TYPES.items()}
_FEISHU_IMAGE_UPLOAD_TYPE = "message"
_FEISHU_FILE_UPLOAD_TYPE = "stream"
_FEISHU_OPUS_UPLOAD_EXTENSIONS = {".ogg", ".opus"}
_FEISHU_MEDIA_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v"}
_FEISHU_DOC_UPLOAD_TYPES = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
# ---------------------------------------------------------------------------
# Connection, retry and batching tuning
# ---------------------------------------------------------------------------

_MAX_TEXT_INJECT_BYTES = 100 * 1024
_FEISHU_CONNECT_ATTEMPTS = 3
_FEISHU_SEND_ATTEMPTS = 3
_FEISHU_APP_LOCK_SCOPE = "feishu-app-id"
_DEFAULT_TEXT_BATCH_DELAY_SECONDS = 0.6
_DEFAULT_TEXT_BATCH_MAX_MESSAGES = 8
_DEFAULT_TEXT_BATCH_MAX_CHARS = 4000
_DEFAULT_MEDIA_BATCH_DELAY_SECONDS = 0.8
_DEFAULT_DEDUP_CACHE_SIZE = 2048
_DEFAULT_WEBHOOK_HOST = "127.0.0.1"
_DEFAULT_WEBHOOK_PORT = 8765
_DEFAULT_WEBHOOK_PATH = "/feishu/webhook"
# ---------------------------------------------------------------------------
# TTL, rate-limit and webhook security constants
# ---------------------------------------------------------------------------

_FEISHU_DEDUP_TTL_SECONDS = 24 * 60 * 60          # 24 hours — matches openclaw
_FEISHU_SENDER_NAME_TTL_SECONDS = 10 * 60          # 10 minutes sender-name cache
_FEISHU_WEBHOOK_MAX_BODY_BYTES = 1 * 1024 * 1024   # 1 MB body limit
_FEISHU_WEBHOOK_RATE_WINDOW_SECONDS = 60            # sliding window for rate limiter
_FEISHU_WEBHOOK_RATE_LIMIT_MAX = 120               # max requests per window per IP — matches openclaw
_FEISHU_WEBHOOK_RATE_MAX_KEYS = 4096               # max tracked keys (prevents unbounded growth)
_FEISHU_WEBHOOK_BODY_TIMEOUT_SECONDS = 30          # max seconds to read request body
_FEISHU_WEBHOOK_ANOMALY_THRESHOLD = 25             # consecutive error responses before WARNING log
_FEISHU_WEBHOOK_ANOMALY_TTL_SECONDS = 6 * 60 * 60  # anomaly tracker TTL (6 hours) — matches openclaw
_FEISHU_CARD_ACTION_DEDUP_TTL_SECONDS = 15 * 60    # card action token dedup window (15 min)

_APPROVAL_CHOICE_MAP: Dict[str, str] = {
    "approve_once": "once",
    "approve_session": "session",
    "approve_always": "always",
    "deny": "deny",
}
_APPROVAL_LABEL_MAP: Dict[str, str] = {
    "once": "Approved once",
    "session": "Approved for session",
    "always": "Approved permanently",
    "deny": "Denied",
}
_FEISHU_BOT_MSG_TRACK_SIZE = 512                   # LRU size for tracking sent message IDs
_FEISHU_REPLY_FALLBACK_CODES = frozenset({230011, 231003})  # reply target withdrawn/missing → create fallback

# Feishu reactions render as prominent badges, unlike Discord/Telegram's
# small footer emoji — a success badge on every message would add noise, so
# we only mark start (Typing) and failure (CrossMark); the reply itself is
# the success signal.
_FEISHU_REACTION_IN_PROGRESS = "Typing"
_FEISHU_REACTION_FAILURE = "CrossMark"
# Bound on the (message_id → reaction_id) handle cache. Happy-path entries
# drain on completion; the cap is a safeguard against unbounded growth from
# delete-failures, not a capacity plan.
_FEISHU_PROCESSING_REACTION_CACHE_SIZE = 1024

# QR onboarding constants
_ONBOARD_ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_ONBOARD_OPEN_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_ONBOARD_REQUEST_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Fallback display strings
# ---------------------------------------------------------------------------

FALLBACK_POST_TEXT = "[Rich text message]"
FALLBACK_FORWARD_TEXT = "[Merged forward message]"
FALLBACK_SHARE_CHAT_TEXT = "[Shared chat]"
FALLBACK_INTERACTIVE_TEXT = "[Interactive message]"
FALLBACK_IMAGE_TEXT = "[Image]"
FALLBACK_ATTACHMENT_TEXT = "[Attachment]"
# ---------------------------------------------------------------------------
# Post/card parsing helpers
# ---------------------------------------------------------------------------

_PREFERRED_LOCALES = ("zh_cn", "en_us")
_MARKDOWN_SPECIAL_CHARS_RE = re.compile(r"([\\`*_{}\[\]()#+\-!|>~])")
_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")
_MENTION_BOUNDARY_CHARS = frozenset(" \t\n\r.,;:!?、，。；：！？()[]{}<>\"'`")
_TRAILING_TERMINAL_PUNCT = frozenset(" \t\n\r.!?。！？")
_WHITESPACE_RE = re.compile(r"\s+")
_SUPPORTED_CARD_TEXT_KEYS = (
    "title",
    "text",
    "content",
    "label",
    "value",
    "name",
    "summary",
    "subtitle",
    "description",
    "placeholder",
    "hint",
)
_SKIP_TEXT_KEYS = {
    "tag",
    "type",
    "msg_type",
    "message_type",
    "chat_id",
    "open_chat_id",
    "share_chat_id",
    "file_key",
    "image_key",
    "user_id",
    "open_id",
    "union_id",
    "url",
    "href",
    "link",
    "token",
    "template",
    "locale",
}


@dataclass(frozen=True)
class FeishuPostMediaRef:
    file_key: str
    file_name: str = ""
    resource_type: str = "file"


@dataclass(frozen=True)
class FeishuMentionRef:
    name: str = ""
    open_id: str = ""
    is_all: bool = False
    is_self: bool = False


@dataclass(frozen=True)
class _FeishuBotIdentity:
    open_id: str = ""
    user_id: str = ""
    name: str = ""

    def matches(self, *, open_id: str, user_id: str, name: str) -> bool:
        # Precedence: open_id > user_id > name. IDs are authoritative when both
        # sides have them; the next tier is only considered when either side
        # lacks the current one.
        if open_id and self.open_id:
            return open_id == self.open_id
        if user_id and self.user_id:
            return user_id == self.user_id
        return bool(self.name) and name == self.name


@dataclass(frozen=True)
class FeishuPostParseResult:
    text_content: str
    image_keys: List[str] = field(default_factory=list)
    media_refs: List[FeishuPostMediaRef] = field(default_factory=list)


@dataclass(frozen=True)
class FeishuNormalizedMessage:
    raw_type: str
    text_content: str
    preferred_message_type: str = "text"
    image_keys: List[str] = field(default_factory=list)
    media_refs: List[FeishuPostMediaRef] = field(default_factory=list)
    mentions: List[FeishuMentionRef] = field(default_factory=list)
    relation_kind: str = "plain"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeishuAdapterSettings:
    app_id: str  # Canonical bot/app identifier (credential, not from event payloads)
    app_secret: str
    domain_name: str
    connection_mode: str
    encrypt_key: str
    verification_token: str
    group_policy: str
    allowed_group_users: frozenset[str]
    # Bot's own open_id (app-scoped) — returned by /bot/v3/info.  Used only for
    # @mention matching: Feishu puts this value in mentions[].id.open_id when
    # a user @-mentions the bot in a group chat.
    bot_open_id: str
    # Bot's user_id (tenant-scoped) — optional, used as fallback mention match.
    bot_user_id: str
    bot_name: str
    dedup_cache_size: int
    text_batch_delay_seconds: float
    text_batch_split_delay_seconds: float
    text_batch_max_messages: int
    text_batch_max_chars: int
    media_batch_delay_seconds: float
    webhook_host: str
    webhook_port: int
    webhook_path: str
    ws_reconnect_nonce: int = 30
    ws_reconnect_interval: int = 120
    ws_ping_interval: Optional[int] = None
    ws_ping_timeout: Optional[int] = None
    admins: frozenset[str] = frozenset()
    default_group_policy: str = ""
    group_rules: Dict[str, FeishuGroupRule] = field(default_factory=dict)
    allow_bots: str = "none"  # "none" | "mentions" | "all"
    require_mention: bool = True


@dataclass
class FeishuGroupRule:
    """Per-group policy rule for controlling which users may interact with the bot."""

    policy: str  # "open" | "allowlist" | "blacklist" | "admin_only" | "disabled"
    allowlist: set[str] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)
    require_mention: Optional[bool] = None  # None = inherit global


@dataclass
class FeishuBatchState:
    events: Dict[str, MessageEvent] = field(default_factory=dict)
    tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Admission: policy types
# ---------------------------------------------------------------------------


RejectReason = Literal[
    "self_echo",
    "self_ids_unknown",
    "bots_disabled",
    "bot_not_mentioned",
    "group_policy_rejected",
]


@dataclass(frozen=True)
class FeishuCurrentConversationAdmissionResult:
    ok: bool
    record: Dict[str, Any] | None = None
    failure_class: str | None = None
    evidence: Dict[str, Any] = field(default_factory=dict)


def _is_bot_sender(sender: Any) -> bool:
    # receive_v1 docs say {user, bot}; accept "app" defensively.
    return getattr(sender, "sender_type", "") in {"bot", "app"}


def _sender_identity(sender: Any) -> frozenset:
    # Take any non-empty id variant — tenant sender_id_type decides which are populated.
    sid = getattr(sender, "sender_id", None)
    if sid is None:
        return frozenset()
    return frozenset(
        v for v in (
            getattr(sid, "open_id", None),
            getattr(sid, "user_id", None),
            getattr(sid, "union_id", None),
        )
        if v
    )


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _escape_markdown_text(text: str) -> str:
    return _MARKDOWN_SPECIAL_CHARS_RE.sub(r"\\\1", text)


def _to_boolean(value: Any) -> bool:
    return value is True or value == 1 or value == "true"


def _is_style_enabled(style: Dict[str, Any] | None, key: str) -> bool:
    if not style:
        return False
    return _to_boolean(style.get(key))


def _wrap_inline_code(text: str) -> str:
    max_run = max([0, *[len(run) for run in re.findall(r"`+", text)]])
    fence = "`" * (max_run + 1)
    body = f" {text} " if text.startswith("`") or text.endswith("`") else text
    return f"{fence}{body}{fence}"


def _sanitize_fence_language(language: str) -> str:
    return language.strip().replace("\n", " ").replace("\r", " ")


def _render_text_element(element: Dict[str, Any]) -> str:
    text = str(element.get("text", "") or "")
    style = element.get("style")
    style_dict = style if isinstance(style, dict) else None

    if _is_style_enabled(style_dict, "code"):
        return _wrap_inline_code(text)

    rendered = _escape_markdown_text(text)
    if not rendered:
        return ""
    if _is_style_enabled(style_dict, "bold"):
        rendered = f"**{rendered}**"
    if _is_style_enabled(style_dict, "italic"):
        rendered = f"*{rendered}*"
    if _is_style_enabled(style_dict, "underline"):
        rendered = f"<u>{rendered}</u>"
    if _is_style_enabled(style_dict, "strikethrough"):
        rendered = f"~~{rendered}~~"
    return rendered


def _render_code_block_element(element: Dict[str, Any]) -> str:
    language = _sanitize_fence_language(
        str(element.get("language", "") or "") or str(element.get("lang", "") or "")
    )
    code = (
        str(element.get("text", "") or "") or str(element.get("content", "") or "")
    ).replace("\r\n", "\n")
    trailing_newline = "" if code.endswith("\n") else "\n"
    return f"```{language}\n{code}{trailing_newline}```"


def _strip_markdown_to_plain_text(text: str) -> str:
    """Strip markdown formatting to plain text for Feishu text fallbacks.

    Delegates common markdown stripping to the shared helper and adds
    Feishu-specific patterns (blockquotes, strikethrough, underline tags,
    horizontal rules, \\r\\n normalisation).
    """
    from gateway.platforms.helpers import strip_markdown
    plain = text.replace("\r\n", "\n")
    plain = _MARKDOWN_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2).strip()})", plain)
    plain = re.sub(r"^>\s?", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s*---+\s*$", "---", plain, flags=re.MULTILINE)
    plain = re.sub(r"~~([^~\n]+)~~", r"\1", plain)
    plain = re.sub(r"<u>([\s\S]*?)</u>", r"\1", plain)
    plain = strip_markdown(plain)
    return plain


def _coerce_int(value: Any, default: Optional[int] = None, min_value: int = 0) -> Optional[int]:
    """Coerce value to int with optional default and minimum constraint."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else default


def _coerce_required_int(value: Any, default: int, min_value: int = 0) -> int:
    parsed = _coerce_int(value, default=default, min_value=min_value)
    return default if parsed is None else parsed


# ---------------------------------------------------------------------------
# Post payload builders and parsers
# ---------------------------------------------------------------------------


def _build_markdown_post_payload(content: str) -> str:
    rows = _build_markdown_post_rows(content)
    return json.dumps(
        {
            "zh_cn": {
                "content": rows,
            }
        },
        ensure_ascii=False,
    )


def _build_markdown_post_rows(content: str) -> List[List[Dict[str, str]]]:
    """Build Feishu post rows while isolating fenced code blocks.

    Feishu's `md` renderer can swallow trailing content when a fenced code block
    appears inside one large markdown element. Split the reply at real fence
    lines so prose before/after the code block remains visible while code stays
    in a dedicated row.
    """
    if not content:
        return [[{"tag": "md", "text": ""}]]
    if "```" not in content:
        return [[{"tag": "md", "text": content}]]

    rows: List[List[Dict[str, str]]] = []
    current: List[str] = []
    in_code_block = False

    def _flush_current() -> None:
        nonlocal current
        if not current:
            return
        segment = "\n".join(current)
        if segment.strip():
            rows.append([{"tag": "md", "text": segment}])
        current = []

    for raw_line in content.splitlines():
        stripped_line = raw_line.strip()
        is_fence = bool(
            _MARKDOWN_FENCE_CLOSE_RE.match(stripped_line)
            if in_code_block
            else _MARKDOWN_FENCE_OPEN_RE.match(stripped_line)
        )

        if is_fence:
            if not in_code_block:
                _flush_current()
            current.append(raw_line)
            in_code_block = not in_code_block
            if not in_code_block:
                _flush_current()
            continue

        current.append(raw_line)

    _flush_current()
    return rows or [[{"tag": "md", "text": content}]]


def parse_feishu_post_payload(
    payload: Any,
    *,
    mentions_map: Optional[Dict[str, FeishuMentionRef]] = None,
) -> FeishuPostParseResult:
    resolved = _resolve_post_payload(payload)
    if not resolved:
        return FeishuPostParseResult(text_content=FALLBACK_POST_TEXT)

    image_keys: List[str] = []
    media_refs: List[FeishuPostMediaRef] = []
    parts: List[str] = []

    title = _normalize_feishu_text(str(resolved.get("title", "")).strip())
    if title:
        parts.append(title)

    for row in resolved.get("content", []) or []:
        if not isinstance(row, list):
            continue
        row_text = _normalize_feishu_text(
            "".join(
                _render_post_element(item, image_keys, media_refs, mentions_map)
                for item in row
            )
        )
        if row_text:
            parts.append(row_text)

    return FeishuPostParseResult(
        text_content="\n".join(parts).strip() or FALLBACK_POST_TEXT,
        image_keys=image_keys,
        media_refs=media_refs,
    )


def _resolve_post_payload(payload: Any) -> Dict[str, Any]:
    direct = _to_post_payload(payload)
    if direct:
        return direct
    if not isinstance(payload, dict):
        return {}

    wrapped = payload.get("post")
    wrapped_direct = _resolve_locale_payload(wrapped)
    if wrapped_direct:
        return wrapped_direct
    return _resolve_locale_payload(payload)


def _resolve_locale_payload(payload: Any) -> Dict[str, Any]:
    direct = _to_post_payload(payload)
    if direct:
        return direct
    if not isinstance(payload, dict):
        return {}

    for key in _PREFERRED_LOCALES:
        candidate = _to_post_payload(payload.get(key))
        if candidate:
            return candidate
    for value in payload.values():
        candidate = _to_post_payload(value)
        if candidate:
            return candidate
    return {}


def _to_post_payload(candidate: Any) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    content = candidate.get("content")
    if not isinstance(content, list):
        return {}
    return {
        "title": str(candidate.get("title", "") or ""),
        "content": content,
    }


def _render_post_element(
    element: Any,
    image_keys: List[str],
    media_refs: List[FeishuPostMediaRef],
    mentions_map: Optional[Dict[str, FeishuMentionRef]] = None,
) -> str:
    if isinstance(element, str):
        return element
    if not isinstance(element, dict):
        return ""

    tag = str(element.get("tag", "")).strip().lower()
    if tag == "text":
        return _render_text_element(element)
    if tag == "a":
        href = str(element.get("href", "")).strip()
        label = str(element.get("text", href) or "").strip()
        if not label:
            return ""
        escaped_label = _escape_markdown_text(label)
        return f"[{escaped_label}]({href})" if href else escaped_label
    if tag == "at":
        # Post <at>.user_id is a placeholder ("@_user_N" or "@_all"); look up
        # the real ref in mentions_map for the display name.
        placeholder = str(element.get("user_id", "")).strip()
        if placeholder == "@_all":
            # Feishu SDK sometimes omits @_all from the top-level mentions
            # payload; record it here so the caller's mention list stays complete.
            if mentions_map is not None and "@_all" not in mentions_map:
                mentions_map["@_all"] = FeishuMentionRef(is_all=True)
            return "@all"
        ref = (mentions_map or {}).get(placeholder)
        if ref is not None:
            display_name = ref.name or ref.open_id or "user"
        else:
            display_name = str(element.get("user_name", "")).strip() or "user"
        return f"@{_escape_markdown_text(display_name)}"
    if tag in {"img", "image"}:
        image_key = str(element.get("image_key", "")).strip()
        if image_key and image_key not in image_keys:
            image_keys.append(image_key)
        alt = str(element.get("text", "")).strip() or str(element.get("alt", "")).strip()
        return f"[Image: {alt}]" if alt else "[Image]"
    if tag in {"media", "file", "audio", "video"}:
        file_key = str(element.get("file_key", "")).strip()
        file_name = (
            str(element.get("file_name", "")).strip()
            or str(element.get("title", "")).strip()
            or str(element.get("text", "")).strip()
        )
        if file_key:
            media_refs.append(
                FeishuPostMediaRef(
                    file_key=file_key,
                    file_name=file_name,
                    resource_type=tag if tag in {"audio", "video"} else "file",
                )
            )
        return f"[Attachment: {file_name}]" if file_name else "[Attachment]"
    if tag in {"emotion", "emoji"}:
        label = str(element.get("text", "")).strip() or str(element.get("emoji_type", "")).strip()
        return f":{_escape_markdown_text(label)}:" if label else "[Emoji]"
    if tag == "br":
        return "\n"
    if tag in {"hr", "divider"}:
        return "\n\n---\n\n"
    if tag == "code":
        code = str(element.get("text", "") or "") or str(element.get("content", "") or "")
        return _wrap_inline_code(code) if code else ""
    if tag in {"code_block", "pre"}:
        return _render_code_block_element(element)

    nested_parts: List[str] = []
    for key in ("text", "title", "content", "children", "elements"):
        extracted = _render_nested_post(element.get(key), image_keys, media_refs, mentions_map)
        if extracted:
            nested_parts.append(extracted)
    return " ".join(part for part in nested_parts if part)


def _render_nested_post(
    value: Any,
    image_keys: List[str],
    media_refs: List[FeishuPostMediaRef],
    mentions_map: Optional[Dict[str, FeishuMentionRef]] = None,
) -> str:
    if isinstance(value, str):
        return _escape_markdown_text(value)
    if isinstance(value, list):
        return " ".join(
            part
            for item in value
            for part in [_render_nested_post(item, image_keys, media_refs, mentions_map)]
            if part
        )
    if isinstance(value, dict):
        direct = _render_post_element(value, image_keys, media_refs, mentions_map)
        if direct:
            return direct
        return " ".join(
            part
            for item in value.values()
            for part in [_render_nested_post(item, image_keys, media_refs, mentions_map)]
            if part
        )
    return ""


# ---------------------------------------------------------------------------
# Message normalization
# ---------------------------------------------------------------------------


def normalize_feishu_message(
    *,
    message_type: str,
    raw_content: str,
    mentions: Optional[Sequence[Any]] = None,
    bot: _FeishuBotIdentity = _FeishuBotIdentity(),
) -> FeishuNormalizedMessage:
    normalized_type = str(message_type or "").strip().lower()
    payload = _load_feishu_payload(raw_content)
    mentions_map = _build_mentions_map(mentions, bot)

    if normalized_type == "text":
        text = str(payload.get("text", "") or "")
        # Feishu SDK sometimes omits @_all from the mentions payload even when
        # the text literal contains it (confirmed via im.v1.message.get).
        if "@_all" in text and "@_all" not in mentions_map:
            mentions_map["@_all"] = FeishuMentionRef(is_all=True)
        return FeishuNormalizedMessage(
            raw_type=normalized_type,
            text_content=_normalize_feishu_text(text, mentions_map),
            mentions=list(mentions_map.values()),
        )
    if normalized_type == "post":
        # The walker writes back to mentions_map if it encounters
        # <at user_id="@_all">, so reading .values() after parsing is enough.
        parsed_post = parse_feishu_post_payload(payload, mentions_map=mentions_map)
        return FeishuNormalizedMessage(
            raw_type=normalized_type,
            text_content=parsed_post.text_content,
            image_keys=list(parsed_post.image_keys),
            media_refs=list(parsed_post.media_refs),
            mentions=list(mentions_map.values()),
            relation_kind="post",
        )
    mention_refs = list(mentions_map.values())
    if normalized_type == "image":
        image_key = str(payload.get("image_key", "") or "").strip()
        alt_text = _normalize_feishu_text(
            str(payload.get("text", "") or "")
            or str(payload.get("alt", "") or "")
            or FALLBACK_IMAGE_TEXT,
            mentions_map,
        )
        return FeishuNormalizedMessage(
            raw_type=normalized_type,
            text_content=alt_text if alt_text != FALLBACK_IMAGE_TEXT else "",
            preferred_message_type="photo",
            image_keys=[image_key] if image_key else [],
            relation_kind="image",
            mentions=mention_refs,
        )
    if normalized_type in {"file", "audio", "media"}:
        media_ref = _build_media_ref_from_payload(payload, resource_type=normalized_type)
        placeholder = _attachment_placeholder(media_ref.file_name)
        return FeishuNormalizedMessage(
            raw_type=normalized_type,
            text_content="",
            preferred_message_type="audio" if normalized_type == "audio" else "document",
            media_refs=[media_ref] if media_ref.file_key else [],
            relation_kind=normalized_type,
            metadata={"placeholder_text": placeholder},
            mentions=mention_refs,
        )
    if normalized_type == "merge_forward":
        return _normalize_merge_forward_message(payload)
    if normalized_type == "share_chat":
        return _normalize_share_chat_message(payload)
    if normalized_type in {"interactive", "card"}:
        return _normalize_interactive_message(normalized_type, payload)

    return FeishuNormalizedMessage(raw_type=normalized_type, text_content="")


def _load_feishu_payload(raw_content: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        return {"text": raw_content}
    return parsed if isinstance(parsed, dict) else {"content": parsed}


def _normalize_merge_forward_message(payload: Dict[str, Any]) -> FeishuNormalizedMessage:
    title = _first_non_empty_text(
        payload.get("title"),
        payload.get("summary"),
        payload.get("preview"),
        _find_first_text(payload, keys=("title", "summary", "preview", "description")),
    )
    entries = _collect_forward_entries(payload)
    lines: List[str] = []
    if title:
        lines.append(title)
    lines.extend(entries[:8])
    text_content = "\n".join(lines).strip() or FALLBACK_FORWARD_TEXT
    return FeishuNormalizedMessage(
        raw_type="merge_forward",
        text_content=text_content,
        relation_kind="merge_forward",
        metadata={"entry_count": len(entries), "title": title},
    )


def _normalize_share_chat_message(payload: Dict[str, Any]) -> FeishuNormalizedMessage:
    chat_name = _first_non_empty_text(
        payload.get("chat_name"),
        payload.get("name"),
        payload.get("title"),
        _find_first_text(payload, keys=("chat_name", "name", "title")),
    )
    share_id = _first_non_empty_text(
        payload.get("chat_id"),
        payload.get("open_chat_id"),
        payload.get("share_chat_id"),
    )
    lines = []
    if chat_name:
        lines.append(f"Shared chat: {chat_name}")
    else:
        lines.append(FALLBACK_SHARE_CHAT_TEXT)
    if share_id:
        lines.append(f"Chat ID: {share_id}")
    text_content = "\n".join(lines)
    return FeishuNormalizedMessage(
        raw_type="share_chat",
        text_content=text_content,
        relation_kind="share_chat",
        metadata={"chat_id": share_id, "chat_name": chat_name},
    )


def _normalize_interactive_message(message_type: str, payload: Dict[str, Any]) -> FeishuNormalizedMessage:
    card_payload = payload.get("card") if isinstance(payload.get("card"), dict) else payload
    title = _first_non_empty_text(
        _find_header_title(card_payload),
        payload.get("title"),
        _find_first_text(card_payload, keys=("title", "summary", "subtitle")),
    )
    body_lines = _collect_card_lines(card_payload)
    actions = _collect_action_labels(card_payload)

    lines: List[str] = []
    if title:
        lines.append(title)
    for line in body_lines:
        if line != title:
            lines.append(line)
    if actions:
        lines.append(f"Actions: {', '.join(actions)}")

    text_content = "\n".join(lines[:12]).strip() or FALLBACK_INTERACTIVE_TEXT
    return FeishuNormalizedMessage(
        raw_type=message_type,
        text_content=text_content,
        relation_kind="interactive",
        metadata={"title": title, "actions": actions},
    )


# ---------------------------------------------------------------------------
# Content extraction utilities (card / forward / text walking)
# ---------------------------------------------------------------------------


def _collect_forward_entries(payload: Dict[str, Any]) -> List[str]:
    candidates: List[Any] = []
    for key in ("messages", "items", "message_list", "records", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    entries: List[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            text = _normalize_feishu_text(str(item or ""))
            if text:
                entries.append(f"- {text}")
            continue
        sender = _first_non_empty_text(
            item.get("sender_name"),
            item.get("user_name"),
            item.get("sender"),
            item.get("name"),
        )
        nested_type = str(item.get("message_type", "") or item.get("msg_type", "")).strip().lower()
        if nested_type == "post":
            body = parse_feishu_post_payload(item.get("content") or item).text_content
        else:
            body = _first_non_empty_text(
                item.get("text"),
                item.get("summary"),
                item.get("preview"),
                item.get("content"),
                _find_first_text(item, keys=("text", "content", "summary", "preview", "title")),
            )
        body = _normalize_feishu_text(body)
        if sender and body:
            entries.append(f"- {sender}: {body}")
        elif body:
            entries.append(f"- {body}")
    return _unique_lines(entries)


def _collect_card_lines(payload: Any) -> List[str]:
    lines = _collect_text_segments(payload, in_rich_block=False)
    normalized = [_normalize_feishu_text(line) for line in lines]
    return _unique_lines([line for line in normalized if line])


def _collect_action_labels(payload: Any) -> List[str]:
    labels: List[str] = []
    for item in _walk_nodes(payload):
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "") or item.get("type", "")).strip().lower()
        if tag not in {"button", "select_static", "overflow", "date_picker", "picker"}:
            continue
        label = _first_non_empty_text(
            item.get("text"),
            item.get("name"),
            item.get("value"),
            _find_first_text(item, keys=("text", "content", "name", "value")),
        )
        if label:
            labels.append(label)
    return _unique_lines(labels)


def _collect_text_segments(value: Any, *, in_rich_block: bool) -> List[str]:
    if isinstance(value, str):
        return [_normalize_feishu_text(value)] if in_rich_block else []
    if isinstance(value, list):
        segments: List[str] = []
        for item in value:
            segments.extend(_collect_text_segments(item, in_rich_block=in_rich_block))
        return segments
    if not isinstance(value, dict):
        return []

    tag = str(value.get("tag", "") or value.get("type", "")).strip().lower()
    next_in_rich_block = in_rich_block or tag in {
        "plain_text",
        "lark_md",
        "markdown",
        "note",
        "div",
        "column_set",
        "column",
        "action",
        "button",
        "select_static",
        "date_picker",
    }

    segments: List[str] = []
    for key in _SUPPORTED_CARD_TEXT_KEYS:
        item = value.get(key)
        if isinstance(item, str) and next_in_rich_block:
            normalized = _normalize_feishu_text(item)
            if normalized:
                segments.append(normalized)

    for key, item in value.items():
        if key in _SKIP_TEXT_KEYS:
            continue
        segments.extend(_collect_text_segments(item, in_rich_block=next_in_rich_block))
    return segments


def _build_media_ref_from_payload(payload: Dict[str, Any], *, resource_type: str) -> FeishuPostMediaRef:
    file_key = str(payload.get("file_key", "") or "").strip()
    file_name = _first_non_empty_text(
        payload.get("file_name"),
        payload.get("title"),
        payload.get("text"),
    )
    effective_type = resource_type if resource_type in {"audio", "video"} else "file"
    return FeishuPostMediaRef(file_key=file_key, file_name=file_name, resource_type=effective_type)


def _attachment_placeholder(file_name: str) -> str:
    normalized_name = _normalize_feishu_text(file_name)
    return f"[Attachment: {normalized_name}]" if normalized_name else FALLBACK_ATTACHMENT_TEXT


def _find_header_title(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    header = payload.get("header")
    if not isinstance(header, dict):
        return ""
    title = header.get("title")
    if isinstance(title, dict):
        return _first_non_empty_text(title.get("content"), title.get("text"), title.get("name"))
    return _normalize_feishu_text(str(title or ""))


def _find_first_text(payload: Any, *, keys: tuple[str, ...]) -> str:
    for node in _walk_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, str):
                normalized = _normalize_feishu_text(value)
                if normalized:
                    return normalized
    return ""


def _walk_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item)


def _first_non_empty_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str):
            normalized = _normalize_feishu_text(value)
            if normalized:
                return normalized
        elif value is not None and not isinstance(value, (dict, list)):
            normalized = _normalize_feishu_text(str(value))
            if normalized:
                return normalized
    return ""


# ---------------------------------------------------------------------------
# General text utilities
# ---------------------------------------------------------------------------


def _normalize_feishu_text(
    text: str,
    mentions_map: Optional[Dict[str, FeishuMentionRef]] = None,
) -> str:
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(0)
        ref = (mentions_map or {}).get(key)
        if ref is None:
            return " "
        name = ref.name or ref.open_id or "user"
        return f"@{name}"

    cleaned = _MENTION_PLACEHOLDER_RE.sub(_sub, text or "")
    cleaned = cleaned.replace("@_all", "@all")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in cleaned.split("\n"))
    cleaned = "\n".join(line for line in cleaned.split("\n") if line)
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def _unique_lines(lines: List[str]) -> List[str]:
    seen: set[str] = set()
    unique: List[str] = []
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return unique


# ---------------------------------------------------------------------------
# Mention helpers
# ---------------------------------------------------------------------------


def _extract_mention_ids(mention: Any) -> tuple[str, str]:
    # Returns (open_id, user_id). im.v1.message.get hands back id as a string
    # plus id_type discriminator; event payloads hand back a nested UserId
    # object carrying both fields.
    mention_id = getattr(mention, "id", None)
    if isinstance(mention_id, str):
        id_type = str(getattr(mention, "id_type", "") or "").lower()
        if id_type == "open_id":
            return mention_id, ""
        if id_type == "user_id":
            return "", mention_id
        return "", ""
    if mention_id is None:
        return "", ""
    return (
        str(getattr(mention_id, "open_id", "") or ""),
        str(getattr(mention_id, "user_id", "") or ""),
    )


def _build_mentions_map(
    mentions: Optional[Sequence[Any]],
    bot: _FeishuBotIdentity,
) -> Dict[str, FeishuMentionRef]:
    result: Dict[str, FeishuMentionRef] = {}
    for mention in mentions or []:
        key = str(getattr(mention, "key", "") or "")
        if not key:
            continue
        if key == "@_all":
            result[key] = FeishuMentionRef(is_all=True)
            continue
        open_id, user_id = _extract_mention_ids(mention)
        name = str(getattr(mention, "name", "") or "").strip()
        result[key] = FeishuMentionRef(
            name=name,
            open_id=open_id,
            is_self=bot.matches(open_id=open_id, user_id=user_id, name=name),
        )
    return result


def _build_mention_hint(mentions: Sequence[FeishuMentionRef]) -> str:
    parts: List[str] = []
    seen: set = set()
    for ref in mentions:
        if ref.is_self:
            continue
        signature = (ref.is_all, ref.open_id, ref.name)
        if signature in seen:
            continue
        seen.add(signature)
        if ref.is_all:
            parts.append("@all")
        elif ref.open_id:
            parts.append(f"{ref.name or 'unknown'} (open_id={ref.open_id})")
        else:
            parts.append(ref.name or "unknown")
    return f"[Mentioned: {', '.join(parts)}]" if parts else ""


def _strip_edge_self_mentions(
    text: str,
    mentions: Sequence[FeishuMentionRef],
) -> str:
    # Leading: strip consecutive self-mentions unconditionally.
    # Trailing: strip only when followed by whitespace/terminal punct, so
    # mid-sentence references ("don't @Bot again") stay intact.
    # Leading word-boundary prevents @Al from eating @Alice.
    if not text:
        return text
    self_names = [
        f"@{ref.name or ref.open_id or 'user'}"
        for ref in mentions
        if ref.is_self
    ]
    if not self_names:
        return text

    remaining = text.lstrip()
    while True:
        for nm in self_names:
            if not remaining.startswith(nm):
                continue
            after = remaining[len(nm):]
            if after and after[0] not in _MENTION_BOUNDARY_CHARS:
                continue
            remaining = after.lstrip()
            break
        else:
            break

    while True:
        i = len(remaining)
        while i > 0 and remaining[i - 1] in _TRAILING_TERMINAL_PUNCT:
            i -= 1
        body = remaining[:i]
        tail = remaining[i:]
        for nm in self_names:
            if body.endswith(nm):
                remaining = body[: -len(nm)].rstrip() + tail
                break
        else:
            return remaining


def _run_official_feishu_ws_client(ws_client: Any, adapter: Any) -> None:
    """Run the official Lark WS client in its own thread-local event loop."""
    import lark_oapi.ws.client as ws_client_module

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ws_client_module.loop = loop
    adapter._ws_thread_loop = loop

    original_connect = ws_client_module.websockets.connect
    original_configure = getattr(ws_client, "_configure", None)

    def _apply_runtime_ws_overrides() -> None:
        try:
            setattr(ws_client, "_reconnect_nonce", adapter._ws_reconnect_nonce)
            setattr(ws_client, "_reconnect_interval", adapter._ws_reconnect_interval)
            if adapter._ws_ping_interval is not None:
                setattr(ws_client, "_ping_interval", adapter._ws_ping_interval)
        except Exception:
            logger.debug("[Feishu] Failed to apply websocket runtime overrides", exc_info=True)

    def _connect_with_overrides(*args: Any, **kwargs: Any) -> Any:
        if adapter._ws_ping_interval is not None and "ping_interval" not in kwargs:
            kwargs["ping_interval"] = adapter._ws_ping_interval
        if adapter._ws_ping_timeout is not None and "ping_timeout" not in kwargs:
            kwargs["ping_timeout"] = adapter._ws_ping_timeout
        return original_connect(*args, **kwargs)

    def _configure_with_overrides(conf: Any) -> Any:
        if original_configure is None:
            raise RuntimeError("Feishu _configure_with_overrides called but original_configure is None")
        result = original_configure(conf)
        _apply_runtime_ws_overrides()
        return result

    ws_client_module.websockets.connect = _connect_with_overrides
    if original_configure is not None:
        setattr(ws_client, "_configure", _configure_with_overrides)
    _apply_runtime_ws_overrides()
    try:
        ws_client.start()
    except Exception:
        pass
    finally:
        ws_client_module.websockets.connect = original_connect
        if original_configure is not None:
            setattr(ws_client, "_configure", original_configure)
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        try:
            loop.stop()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        adapter._ws_thread_loop = None


def check_feishu_requirements() -> bool:
    """Check if Feishu/Lark dependencies are available.

    Lazy-installs lark-oapi via ``tools.lazy_deps.ensure("platform.feishu")``
    on first call if not present. Rebinds all module-level globals on success.
    """
    if FEISHU_AVAILABLE:
        return True

    def _import():
        import lark_oapi as lark
        from lark_oapi.api.application.v6 import GetApplicationRequest
        from lark_oapi.api.im.v1 import (
            CreateFileRequest, CreateFileRequestBody,
            CreateImageRequest, CreateImageRequestBody,
            CreateMessageRequest, CreateMessageRequestBody,
            GetChatRequest, GetMessageRequest, GetMessageResourceRequest,
            P2ImMessageMessageReadV1,
            ReplyMessageRequest, ReplyMessageRequestBody,
            UpdateMessageRequest, UpdateMessageRequestBody,
        )
        from lark_oapi.core import AccessTokenType, HttpMethod
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        from lark_oapi.core.model import BaseRequest
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard, P2CardActionTriggerResponse,
        )
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import Client as FeishuWSClient
        return {
            "lark": lark,
            "GetApplicationRequest": GetApplicationRequest,
            "CreateFileRequest": CreateFileRequest,
            "CreateFileRequestBody": CreateFileRequestBody,
            "CreateImageRequest": CreateImageRequest,
            "CreateImageRequestBody": CreateImageRequestBody,
            "CreateMessageRequest": CreateMessageRequest,
            "CreateMessageRequestBody": CreateMessageRequestBody,
            "GetChatRequest": GetChatRequest,
            "GetMessageRequest": GetMessageRequest,
            "GetMessageResourceRequest": GetMessageResourceRequest,
            "P2ImMessageMessageReadV1": P2ImMessageMessageReadV1,
            "ReplyMessageRequest": ReplyMessageRequest,
            "ReplyMessageRequestBody": ReplyMessageRequestBody,
            "UpdateMessageRequest": UpdateMessageRequest,
            "UpdateMessageRequestBody": UpdateMessageRequestBody,
            "AccessTokenType": AccessTokenType,
            "HttpMethod": HttpMethod,
            "FEISHU_DOMAIN": FEISHU_DOMAIN,
            "LARK_DOMAIN": LARK_DOMAIN,
            "BaseRequest": BaseRequest,
            "CallBackCard": CallBackCard,
            "P2CardActionTriggerResponse": P2CardActionTriggerResponse,
            "EventDispatcherHandler": EventDispatcherHandler,
            "FeishuWSClient": FeishuWSClient,
            "FEISHU_AVAILABLE": True,
        }

    from tools.lazy_deps import ensure_and_bind
    return ensure_and_bind("platform.feishu", _import, globals(), prompt=False)


class FeishuAdapter(BasePlatformAdapter):
    """Feishu/Lark bot adapter."""

    MAX_MESSAGE_LENGTH = 8000
    # Max distinct chat IDs retained before LRU eviction bounds memory growth.
    CHAT_LOCK_MAX_SIZE: int = 1000
    _DELIVERY_OPERATION_HINT_KEY = "delivery_operation_hint"
    _SEND_DELIVERY_OPERATION_HINTS = frozenset(
        {"queued_followup_first_reply", "stream_fresh_final"}
    )
    _DELIVERY_AUDIT_STATE_MISSING = "feishu_delivery_audit_state_missing"
    # Threshold for detecting Feishu client-side message splits.
    # When a chunk is near the ~4096-char practical limit, a continuation
    # is almost certain.
    _SPLIT_THRESHOLD = 4000

    # =========================================================================
    # Lifecycle — init / settings / connect / disconnect
    # =========================================================================

    def __init__(self, config: PlatformConfig, session_isolation_config: Optional[Any] = None):
        super().__init__(config, Platform.FEISHU, session_isolation_config=session_isolation_config)
        self._settings = self._load_settings(config.extra or {})
        self._apply_settings(self._settings)
        self._client: Optional[Any] = None
        self._ws_client: Optional[Any] = None
        self._ws_future: Optional[asyncio.Future] = None
        self._ws_thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._webhook_runner: Optional[Any] = None
        self._webhook_site: Optional[Any] = None
        self._event_handler: Optional[Any] = None
        self._seen_message_ids: Dict[str, float] = {}  # message_id → seen_at (time.time())
        self._seen_message_order: List[str] = []
        self._dedup_state_path = get_hermes_home() / "feishu_seen_message_ids.json"
        gateway_event_state_dir = (
            config.extra.get("gateway_event_state_dir")
            or config.extra.get("hermes_tools_state_dir")
            or os.getenv("HERMES_GATEWAY_EVENT_STATE_DIR", "")
            or os.getenv("HERMES_TOOLS_STATE_DIR", "")
        )
        self._gateway_event_state_dir = (
            Path(str(gateway_event_state_dir)) if gateway_event_state_dir else None
        )
        self._hermes_tools_state_dir = self._gateway_event_state_dir
        self._dedup_lock = threading.Lock()
        self._sender_name_cache: Dict[str, tuple[str, float]] = {}  # sender_id → (name, expire_at)
        self._webhook_rate_counts: Dict[str, tuple[int, float]] = {}  # rate_key → (count, window_start)
        self._webhook_anomaly_counts: Dict[str, tuple[int, str, float]] = {}  # ip → (count, last_status, first_seen)
        self._card_action_tokens: Dict[str, float] = {}  # token → first_seen_time
        # Inbound events that arrived before the adapter loop was ready
        # (e.g. during startup/restart or network-flap reconnect). A single
        # drainer thread replays them as soon as the loop becomes available.
        self._pending_inbound_events: List[Any] = []
        self._pending_inbound_lock = threading.Lock()
        self._pending_drain_scheduled = False
        self._pending_inbound_max_depth = 1000  # cap queue; drop oldest beyond
        self._chat_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()  # chat_id → lock (per-chat serial processing, LRU-bounded)
        self._sent_message_ids_to_chat: Dict[str, str] = {}  # message_id → chat_id (for reaction routing)
        self._sent_message_id_order: List[str] = []  # LRU order for _sent_message_ids_to_chat
        self._chat_info_cache: Dict[str, Dict[str, Any]] = {}
        self._message_text_cache: Dict[str, Optional[str]] = {}
        self._app_lock_identity: Optional[str] = None
        self._text_batch_state = FeishuBatchState()
        self._pending_text_batches = self._text_batch_state.events
        self._pending_text_batch_tasks = self._text_batch_state.tasks
        self._pending_text_batch_counts = self._text_batch_state.counts
        self._media_batch_state = FeishuBatchState()
        self._pending_media_batches = self._media_batch_state.events
        self._pending_media_batch_tasks = self._media_batch_state.tasks
        # Exec approval button state (approval_id → session/message/chat scope metadata)
        self._approval_state: Dict[int, Dict[str, Any]] = {}
        self._approval_counter = itertools.count(1)
        # Update prompt button state (prompt_id → session/message/chat scope metadata)
        self._update_prompt_state: Dict[int, Dict[str, Any]] = {}
        self._update_prompt_counter = itertools.count(1)
        # Feishu reaction deletion requires the opaque reaction_id returned
        # by create, so we cache it per message_id.
        self._pending_processing_reactions: "OrderedDict[str, str]" = OrderedDict()
        self._load_seen_message_ids()

    @staticmethod
    def _load_settings(extra: Dict[str, Any]) -> FeishuAdapterSettings:
        # Parse per-group rules from config
        raw_group_rules = extra.get("group_rules", {})
        group_rules: Dict[str, FeishuGroupRule] = {}
        if isinstance(raw_group_rules, dict):
            for chat_id, rule_cfg in raw_group_rules.items():
                if not isinstance(rule_cfg, dict):
                    continue
                # Only override when the key is explicitly set — missing vs false
                # must not collapse.
                per_chat_require_mention: Optional[bool] = None
                if "require_mention" in rule_cfg:
                    per_chat_require_mention = _to_boolean(rule_cfg.get("require_mention"))
                group_rules[str(chat_id)] = FeishuGroupRule(
                    policy=str(rule_cfg.get("policy", "open")).strip().lower(),
                    allowlist={str(u).strip() for u in rule_cfg.get("allowlist", []) if str(u).strip()},
                    blacklist={str(u).strip() for u in rule_cfg.get("blacklist", []) if str(u).strip()},
                    require_mention=per_chat_require_mention,
                )

        # Bot-level admins
        raw_admins = extra.get("admins", [])
        admins = frozenset(str(u).strip() for u in raw_admins if str(u).strip())

        # Default group policy (for groups not in group_rules)
        default_group_policy = str(extra.get("default_group_policy", "")).strip().lower()

        # Env-only so adapter and gateway auth bypass share one source; yaml
        # feishu.allow_bots is bridged to this env var at config load.
        allow_bots = os.getenv("FEISHU_ALLOW_BOTS", "none").strip().lower()
        if allow_bots not in {"none", "mentions", "all"}:
            logger.warning(
                "[Feishu] Unknown allow_bots=%r, falling back to 'none'. Valid: none, mentions, all.",
                allow_bots,
            )
            allow_bots = "none"

        return FeishuAdapterSettings(
            app_id=str(extra.get("app_id") or os.getenv("FEISHU_APP_ID", "")).strip(),
            app_secret=str(extra.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")).strip(),
            domain_name=str(extra.get("domain") or os.getenv("FEISHU_DOMAIN", "feishu")).strip().lower(),
            connection_mode=str(
                extra.get("connection_mode") or os.getenv("FEISHU_CONNECTION_MODE", "websocket")
            ).strip().lower(),
            encrypt_key=str(extra.get("encrypt_key") or os.getenv("FEISHU_ENCRYPT_KEY", "")).strip(),
            verification_token=str(
                extra.get("verification_token") or os.getenv("FEISHU_VERIFICATION_TOKEN", "")
            ).strip(),
            group_policy=os.getenv("FEISHU_GROUP_POLICY", "allowlist").strip().lower(),
            allowed_group_users=frozenset(
                item.strip()
                for item in os.getenv("FEISHU_ALLOWED_USERS", "").split(",")
                if item.strip()
            ),
            bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", "").strip(),
            bot_user_id=os.getenv("FEISHU_BOT_USER_ID", "").strip(),
            bot_name=os.getenv("FEISHU_BOT_NAME", "").strip(),
            dedup_cache_size=max(
                32,
                int(os.getenv("HERMES_FEISHU_DEDUP_CACHE_SIZE", str(_DEFAULT_DEDUP_CACHE_SIZE))),
            ),
            text_batch_delay_seconds=float(
                os.getenv("HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS", str(_DEFAULT_TEXT_BATCH_DELAY_SECONDS))
            ),
            text_batch_split_delay_seconds=float(
                os.getenv("HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0")
            ),
            text_batch_max_messages=max(
                1,
                int(os.getenv("HERMES_FEISHU_TEXT_BATCH_MAX_MESSAGES", str(_DEFAULT_TEXT_BATCH_MAX_MESSAGES))),
            ),
            text_batch_max_chars=max(
                1,
                int(os.getenv("HERMES_FEISHU_TEXT_BATCH_MAX_CHARS", str(_DEFAULT_TEXT_BATCH_MAX_CHARS))),
            ),
            media_batch_delay_seconds=float(
                os.getenv("HERMES_FEISHU_MEDIA_BATCH_DELAY_SECONDS", str(_DEFAULT_MEDIA_BATCH_DELAY_SECONDS))
            ),
            webhook_host=str(
                extra.get("webhook_host") or os.getenv("FEISHU_WEBHOOK_HOST", _DEFAULT_WEBHOOK_HOST)
            ).strip(),
            webhook_port=int(
                extra.get("webhook_port") or os.getenv("FEISHU_WEBHOOK_PORT", str(_DEFAULT_WEBHOOK_PORT))
            ),
            webhook_path=(
                str(extra.get("webhook_path") or os.getenv("FEISHU_WEBHOOK_PATH", _DEFAULT_WEBHOOK_PATH)).strip()
                or _DEFAULT_WEBHOOK_PATH
            ),
            ws_reconnect_nonce=_coerce_required_int(extra.get("ws_reconnect_nonce"), default=30, min_value=0),
            ws_reconnect_interval=_coerce_required_int(extra.get("ws_reconnect_interval"), default=120, min_value=1),
            ws_ping_interval=_coerce_int(extra.get("ws_ping_interval"), default=None, min_value=1),
            ws_ping_timeout=_coerce_int(extra.get("ws_ping_timeout"), default=None, min_value=1),
            admins=admins,
            default_group_policy=default_group_policy,
            group_rules=group_rules,
            allow_bots=allow_bots,
            require_mention=_to_boolean(
                extra.get("require_mention", os.getenv("FEISHU_REQUIRE_MENTION", "true"))
            ),
        )

    def _apply_settings(self, settings: FeishuAdapterSettings) -> None:
        self._app_id = settings.app_id
        self._app_secret = settings.app_secret
        self._domain_name = settings.domain_name
        self._connection_mode = settings.connection_mode
        self._encrypt_key = settings.encrypt_key
        self._verification_token = settings.verification_token
        self._group_policy = settings.group_policy
        self._allowed_group_users = set(settings.allowed_group_users)
        self._admins = set(settings.admins)
        self._default_group_policy = settings.default_group_policy or settings.group_policy
        self._group_rules = settings.group_rules
        self._bot_open_id = settings.bot_open_id
        self._bot_user_id = settings.bot_user_id
        self._bot_name = settings.bot_name
        self._dedup_cache_size = settings.dedup_cache_size
        self._text_batch_delay_seconds = settings.text_batch_delay_seconds
        self._text_batch_split_delay_seconds = settings.text_batch_split_delay_seconds
        self._text_batch_max_messages = settings.text_batch_max_messages
        self._text_batch_max_chars = settings.text_batch_max_chars
        self._media_batch_delay_seconds = settings.media_batch_delay_seconds
        self._webhook_host = settings.webhook_host
        self._webhook_port = settings.webhook_port
        self._webhook_path = settings.webhook_path
        self._ws_reconnect_nonce = settings.ws_reconnect_nonce
        self._ws_reconnect_interval = settings.ws_reconnect_interval
        self._ws_ping_interval = settings.ws_ping_interval
        self._ws_ping_timeout = settings.ws_ping_timeout
        self._allow_bots = settings.allow_bots
        self._require_mention = settings.require_mention

    def _build_event_handler(self) -> Any:
        if EventDispatcherHandler is None:
            return None
        return (
            EventDispatcherHandler.builder(
                self._encrypt_key,
                self._verification_token,
            )
            .register_p2_im_message_message_read_v1(self._on_message_read_event)
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_im_message_reaction_created_v1(
                lambda data: self._on_reaction_event("im.message.reaction.created_v1", data)
            )
            .register_p2_im_message_reaction_deleted_v1(
                lambda data: self._on_reaction_event("im.message.reaction.deleted_v1", data)
            )
            .register_p2_card_action_trigger(self._on_card_action_trigger)
            .register_p2_im_chat_member_bot_added_v1(self._on_bot_added_to_chat)
            .register_p2_im_chat_member_bot_deleted_v1(self._on_bot_removed_from_chat)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_p2p_chat_entered)
            .register_p2_im_message_recalled_v1(self._on_message_recalled)
            .register_p2_customized_event(
                "drive.notice.comment_add_v1",
                self._on_drive_comment_event,
            )
            .build()
        )

    async def connect(self) -> bool:
        """Connect to Feishu/Lark."""
        if not FEISHU_AVAILABLE:
            logger.error("[Feishu] lark-oapi not installed")
            return False
        if not self._app_id or not self._app_secret:
            logger.error("[Feishu] FEISHU_APP_ID or FEISHU_APP_SECRET not set")
            return False
        if self._connection_mode not in {"websocket", "webhook"}:
            logger.error(
                "[Feishu] Unsupported FEISHU_CONNECTION_MODE=%s. Supported modes: websocket, webhook.",
                self._connection_mode,
            )
            return False
        if self._connection_mode == "webhook" and not (self._verification_token or self._encrypt_key):
            logger.error(
                "[Feishu] Webhook mode requires FEISHU_VERIFICATION_TOKEN or FEISHU_ENCRYPT_KEY."
            )
            return False

        try:
            self._app_lock_identity = self._app_id
            acquired, existing = acquire_scoped_lock(
                _FEISHU_APP_LOCK_SCOPE,
                self._app_lock_identity,
                metadata={"platform": self.platform.value},
            )
            if not acquired:
                owner_pid = existing.get("pid") if isinstance(existing, dict) else None
                message = (
                    "Another local Hermes gateway is already using this Feishu app_id"
                    + (f" (PID {owner_pid})." if owner_pid else ".")
                    + " Stop the other gateway before starting a second Feishu websocket client."
                )
                logger.error("[Feishu] %s", message)
                self._set_fatal_error("feishu_app_lock", message, retryable=False)
                return False

            self._loop = asyncio.get_running_loop()
            await self._connect_with_retry()
            self._mark_connected()
            logger.info("[Feishu] Connected in %s mode (%s)", self._connection_mode, self._domain_name)
            return True
        except Exception as exc:
            await self._release_app_lock()
            message = f"Feishu startup failed: {exc}"
            self._set_fatal_error("feishu_connect_error", message, retryable=True)
            logger.error("[Feishu] Failed to connect: %s", exc, exc_info=True)
            return False

    async def disconnect(self) -> None:
        """Disconnect from Feishu/Lark."""
        self._running = False
        await self._cancel_pending_tasks(self._pending_text_batch_tasks)
        await self._cancel_pending_tasks(self._pending_media_batch_tasks)
        self._reset_batch_buffers()
        self._disable_websocket_auto_reconnect()
        await self._stop_webhook_server()

        ws_thread_loop = self._ws_thread_loop
        if ws_thread_loop is not None and not ws_thread_loop.is_closed():
            logger.debug("[Feishu] Cancelling websocket thread tasks and stopping loop")

            def cancel_all_tasks() -> None:
                tasks = [t for t in asyncio.all_tasks(ws_thread_loop) if not t.done()]
                logger.debug("[Feishu] Found %d pending tasks in websocket thread", len(tasks))
                for task in tasks:
                    task.cancel()
                ws_thread_loop.call_later(0.1, ws_thread_loop.stop)

            ws_thread_loop.call_soon_threadsafe(cancel_all_tasks)

        ws_future = self._ws_future
        if ws_future is not None:
            try:
                logger.debug("[Feishu] Waiting for websocket thread to exit (timeout=10s)")
                await asyncio.wait_for(asyncio.shield(ws_future), timeout=10.0)
                logger.debug("[Feishu] Websocket thread exited cleanly")
            except asyncio.TimeoutError:
                logger.warning("[Feishu] Websocket thread did not exit within 10s - may be stuck")
            except asyncio.CancelledError:
                logger.debug("[Feishu] Websocket thread cancelled during disconnect")
            except Exception as exc:
                logger.debug("[Feishu] Websocket thread exited with error: %s", exc, exc_info=True)

        self._ws_future = None
        self._ws_thread_loop = None
        self._loop = None
        self._event_handler = None
        self._persist_seen_message_ids()
        await self._release_app_lock()

        self._mark_disconnected()
        logger.info("[Feishu] Disconnected")

    async def _cancel_pending_tasks(self, tasks: Dict[str, asyncio.Task]) -> None:
        pending = [task for task in tasks.values() if task and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        tasks.clear()

    def _reset_batch_buffers(self) -> None:
        self._pending_text_batches.clear()
        self._pending_text_batch_counts.clear()
        self._pending_media_batches.clear()

    def _disable_websocket_auto_reconnect(self) -> None:
        if self._ws_client is None:
            return
        try:
            setattr(self._ws_client, "_auto_reconnect", False)
        except Exception:
            pass
        finally:
            self._ws_client = None

    async def _stop_webhook_server(self) -> None:
        if self._webhook_runner is None:
            return
        try:
            await self._webhook_runner.cleanup()
        finally:
            self._webhook_runner = None
            self._webhook_site = None

    # =========================================================================
    # Outbound — send / edit / send_image / send_voice / …
    # =========================================================================

    @staticmethod
    def _metadata_with_current_admission(metadata: Any, reply_to: Optional[str]) -> Any:
        admission = _FEISHU_CURRENT_ADMISSION_CONTEXT.get()
        if not isinstance(admission, dict):
            return metadata
        if metadata is not None and not isinstance(metadata, dict):
            return metadata
        merged = dict(metadata or {})
        if "feishu_current_admission" not in merged:
            reply_admission = dict(admission)
            reply_ref = feishu_hashed_ref("feishu_reply_anchor", reply_to)
            if "reply_anchor_ref" not in reply_admission and reply_ref is not None:
                reply_admission["reply_anchor_ref"] = reply_ref.value_hash
            merged["feishu_current_admission"] = reply_admission
        return merged

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> SendResult:
        return await super()._send_with_retry(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=self._metadata_with_current_admission(metadata, reply_to),
            max_retries=max_retries,
            base_delay=base_delay,
        )

    _RENDER_PLAN_NATIVE_PART_TYPES = frozenset(
        {"plain_text", "post", "markdown", "code_block", "table", "link"}
    )
    _RENDER_PLAN_ATTACHMENT_PART_TYPES = frozenset(
        {"image", "file", "attachment", "local_path"}
    )
    _RENDER_PLAN_FALLBACK_PART_TYPES = frozenset({"card", "button"})
    _RENDER_PLAN_RAW_DESCRIPTOR_KEYS = frozenset(
        {
            "body",
            "method",
            "openapidescriptor",
            "path",
            "rawbody",
            "rawpath",
            "requestbody",
            "sdkrequest",
        }
    )

    @classmethod
    def _rendered_part_contains_raw_descriptor(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(key, str)
                    and re.sub(r"[^a-z0-9]+", "", key.lower())
                    in cls._RENDER_PLAN_RAW_DESCRIPTOR_KEYS
                ):
                    return True
                if cls._rendered_part_contains_raw_descriptor(item):
                    return True
            return False
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return any(cls._rendered_part_contains_raw_descriptor(item) for item in value)
        return False

    def _render_plan_delivery_id(
        self,
        *,
        metadata: Optional[Dict[str, Any]],
        plan: RenderPlan,
        part_index: int,
        part: Optional[RenderPlanPart],
        message_index: int,
        message_count: int,
    ) -> str:
        explicit = (metadata or {}).get("delivery_id")
        if isinstance(explicit, str) and re.fullmatch(r"^[A-Za-z0-9_.:-]{1,127}$", explicit):
            if part is None:
                return self._derived_delivery_id(explicit, "render_plan_probe")
            suffix = f"render_plan_part_{part_index + 1}_of_{len(plan.parts)}"
            if message_count > 1:
                suffix = (
                    f"{suffix}_message_{message_index + 1}_of_{message_count}"
                )
            return self._derived_delivery_id(explicit, suffix)
        return self._delivery_id_for(
            "render_plan_send",
            metadata=metadata,
            parts=[
                plan.plan_hash,
                part.part_hash if part is not None else "route-probe",
                part_index,
                message_index,
                message_count,
            ],
        )

    @classmethod
    def _render_plan_legacy_ref(cls, kind: str, *values: Any) -> str:
        material = "\x1f".join(str(value) for value in values)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"feishu:render_plan_{kind}:{digest}"

    @classmethod
    def _render_plan_legacy_message_id(cls, *values: Any) -> str:
        material = "\x1f".join(str(value) for value in values)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"feishu_render_plan_message_{digest}"

    def _metadata_for_render_plan_part(
        self,
        *,
        metadata: Optional[Dict[str, Any]],
        plan: RenderPlan,
        part: RenderPlanPart,
        part_index: int,
        message_index: int,
        message_count: int,
    ) -> Optional[Dict[str, Any]]:
        if metadata is None:
            return None
        chunk_metadata = dict(metadata)
        chunk_metadata["delivery_id"] = self._render_plan_delivery_id(
            metadata=metadata,
            plan=plan,
            part_index=part_index,
            part=part,
            message_index=message_index,
            message_count=message_count,
        )
        chunk_metadata["correlation_id"] = chunk_metadata["delivery_id"]
        chunk_metadata["feishu_render_plan"] = {
            "plan_hash": plan.plan_hash,
            "part_hash": part.part_hash,
            "part_index": part_index,
            "part_count": len(plan.parts),
            "part_type": part.part_type,
            "message_index": message_index,
            "message_count": message_count,
            "chunk_group_hash": part.chunk_group,
            "chunk_index": part.chunk_index,
            "chunk_count": part.chunk_count,
            "fallback_action": part.fallback_action,
        }
        return chunk_metadata

    @classmethod
    def _render_plan_failure_class(cls, result: SendResult) -> str:
        if result.error and result.error.startswith("["):
            return "feishu_terminal_non_acceptance"
        if result.error:
            return cls._stable_failure_class(result.error)
        return "feishu_render_part_send_failed"

    def _render_plan_part_messages(
        self,
        *,
        part: RenderPlanPart,
        rendered: Any,
        part_index: int,
        fallback_events: list[dict[str, Any]],
    ) -> tuple[list[tuple[str, str]], Optional[str]]:
        if part.part_type in self._RENDER_PLAN_ATTACHMENT_PART_TYPES:
            return [], "feishu_render_attachment_provenance_required"
        if part.part_type in self._RENDER_PLAN_FALLBACK_PART_TYPES:
            return self._render_plan_fallback_messages(
                part=part,
                rendered=rendered,
                part_index=part_index,
                fallback_events=fallback_events,
            )
        if part.part_type not in self._RENDER_PLAN_NATIVE_PART_TYPES:
            return [], "feishu_render_part_type_invalid"

        msg_type, text, failure = self._render_plan_native_message_text(part, rendered)
        if failure is not None:
            return [], failure
        chunks = self.truncate_message(text, self.MAX_MESSAGE_LENGTH)
        messages: list[tuple[str, str]] = []
        for chunk in chunks:
            if len(chunk) > self.MAX_MESSAGE_LENGTH:
                return [], "feishu_render_part_too_large"
            messages.append((msg_type, self._render_plan_payload(msg_type, chunk)))
        return messages, None

    def _render_plan_fallback_messages(
        self,
        *,
        part: RenderPlanPart,
        rendered: Any,
        part_index: int,
        fallback_events: list[dict[str, Any]],
    ) -> tuple[list[tuple[str, str]], Optional[str]]:
        event = {
            "part_hash": part.part_hash,
            "part_index": part_index,
            "part_type": part.part_type,
            "fallback_action": part.fallback_action,
        }
        if part.fallback_action == "drop":
            fallback_events.append({**event, "outcome": "dropped"})
            return [], None
        if isinstance(rendered, str):
            text = rendered
        elif isinstance(rendered, dict):
            key = (
                "preserve_text"
                if part.fallback_action == "preserve"
                else "replacement_text"
            )
            text = rendered.get(key)
        else:
            text = None
        if not isinstance(text, str):
            return [], "feishu_render_fallback_content_missing"
        chunks = self.truncate_message(text, self.MAX_MESSAGE_LENGTH)
        messages: list[tuple[str, str]] = []
        for chunk in chunks:
            if len(chunk) > self.MAX_MESSAGE_LENGTH:
                return [], "feishu_render_part_too_large"
            messages.append(("text", self._render_plan_payload("text", chunk)))
        fallback_events.append({**event, "outcome": "sent"})
        return messages, None

    def _render_plan_native_message_text(
        self,
        part: RenderPlanPart,
        rendered: Any,
    ) -> tuple[str, str, Optional[str]]:
        if part.part_type == "plain_text":
            if not isinstance(rendered, str):
                return "text", "", "feishu_render_part_content_invalid"
            return "text", rendered, None
        if part.part_type in {"post", "markdown"}:
            if not isinstance(rendered, str):
                return "post", "", "feishu_render_part_content_invalid"
            return "post", rendered, None
        if part.part_type == "code_block":
            if isinstance(rendered, str):
                return "post", f"```\n{rendered}\n```", None
            if not isinstance(rendered, dict) or not isinstance(rendered.get("code"), str):
                return "post", "", "feishu_render_part_content_invalid"
            language = rendered.get("language")
            if not isinstance(language, str):
                language = ""
            return "post", f"```{language}\n{rendered['code']}\n```", None
        if part.part_type == "table":
            return self._render_plan_table_text(rendered)
        if part.part_type == "link":
            return self._render_plan_link_text(rendered)
        return "text", "", "feishu_render_part_type_invalid"

    @staticmethod
    def _render_plan_table_text(rendered: Any) -> tuple[str, str, Optional[str]]:
        if not isinstance(rendered, dict):
            return "text", "", "feishu_render_part_content_invalid"
        headers = rendered.get("headers")
        rows = rendered.get("rows")
        if (
            not isinstance(headers, Sequence)
            or isinstance(headers, (bytes, bytearray, str))
            or not all(isinstance(item, str) for item in headers)
            or not isinstance(rows, Sequence)
            or isinstance(rows, (bytes, bytearray, str))
        ):
            return "text", "", "feishu_render_part_content_invalid"
        rendered_rows: list[list[str]] = []
        for row in rows:
            if (
                not isinstance(row, Sequence)
                or isinstance(row, (bytes, bytearray, str))
                or not all(isinstance(item, str) for item in row)
            ):
                return "text", "", "feishu_render_part_content_invalid"
            rendered_rows.append(list(row))
        header_line = "| " + " | ".join(headers) + " |"
        separator = "| " + " | ".join("---" for _ in headers) + " |"
        body = ["| " + " | ".join(row) + " |" for row in rendered_rows]
        return "text", "\n".join([header_line, separator, *body]), None

    @staticmethod
    def _render_plan_link_text(rendered: Any) -> tuple[str, str, Optional[str]]:
        if not isinstance(rendered, dict):
            return "post", "", "feishu_render_part_content_invalid"
        label = rendered.get("label")
        href = rendered.get("href")
        if not isinstance(label, str) or not isinstance(href, str):
            return "post", "", "feishu_render_part_content_invalid"
        return "post", f"[{label}]({href})", None

    @staticmethod
    def _render_plan_payload(msg_type: str, text: str) -> str:
        if msg_type == "post":
            return _build_markdown_post_payload(text)
        return json.dumps({"text": text}, ensure_ascii=False)

    async def send_render_plan(
        self,
        *,
        chat_id: str,
        plan: RenderPlan,
        rendered_parts: Dict[str, Any],
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if self._gateway_event_state_dir is None:
            return SendResult(
                success=False,
                error="feishu_render_plan_audit_state_missing",
            )

        valid, failure_class = validate_render_plan(plan)
        if not valid:
            return SendResult(
                success=False,
                error=failure_class or "feishu_render_plan_invalid",
            )
        if not isinstance(rendered_parts, dict):
            return SendResult(
                success=False,
                error="feishu_render_part_content_missing",
            )
        if self._rendered_part_contains_raw_descriptor(rendered_parts):
            return SendResult(
                success=False,
                error="feishu_action_raw_tool_material",
            )

        route_probe_id = self._render_plan_delivery_id(
            metadata=metadata,
            plan=plan,
            part_index=0,
            part=None,
            message_index=0,
            message_count=1,
        )
        lifecycle_context = self._current_delivery_lifecycle_context(
            delivery_id=route_probe_id,
            action="send",
            target=f"feishu:chat:{chat_id}",
            metadata=metadata,
        )
        if lifecycle_context is None:
            return SendResult(
                success=False,
                error="feishu_current_reply_admission_missing",
            )

        message_ids: list[str] = []
        fallback_events: list[dict[str, Any]] = []
        sent_part_count = 0
        for part_index, part in enumerate(plan.parts):
            rendered = rendered_parts.get(part.part_hash)
            if rendered is None:
                return SendResult(
                    success=False,
                    error="feishu_render_part_content_missing",
                    raw_response={
                        "failed_part_index": part_index,
                        "part_hash": part.part_hash,
                    },
                )
            messages, render_failure = self._render_plan_part_messages(
                part=part,
                rendered=rendered,
                part_index=part_index,
                fallback_events=fallback_events,
            )
            if render_failure is not None:
                return SendResult(
                    success=False,
                    error=render_failure,
                    raw_response={
                        "failed_part_index": part_index,
                        "part_hash": part.part_hash,
                        "part_type": part.part_type,
                    },
                )
            for message_index, (msg_type, payload) in enumerate(messages):
                chunk_metadata = self._metadata_for_render_plan_part(
                    metadata=metadata,
                    plan=plan,
                    part=part,
                    part_index=part_index,
                    message_index=message_index,
                    message_count=len(messages),
                )
                delivery_id = self._render_plan_delivery_id(
                    metadata=chunk_metadata,
                    plan=plan,
                    part_index=part_index,
                    part=part,
                    message_index=message_index,
                    message_count=len(messages),
                )
                raw_inbound_id = self._delivery_metadata(
                    chunk_metadata, "inbound_id", reply_to or chat_id
                )
                raw_session_id = self._delivery_metadata(
                    chunk_metadata, "session_id", "session"
                )
                raw_correlation_id = self._delivery_metadata(
                    chunk_metadata, "correlation_id", delivery_id
                )
                legacy_message_id = self._render_plan_legacy_message_id(
                    delivery_id,
                    plan.plan_hash,
                    part.part_hash,
                    message_index,
                )
                response_or_result = await self._audited_delivery(
                    delivery_id=delivery_id,
                    operation="render_plan_send",
                    target=f"feishu:chat:{chat_id}",
                    metadata=chunk_metadata,
                    inbound_id=raw_inbound_id,
                    session_id=raw_session_id,
                    correlation_id=raw_correlation_id,
                    ledger_target=self._render_plan_legacy_ref(
                        "target",
                        plan.plan_hash,
                        plan.target_ref_hash,
                        chat_id,
                        part.part_hash,
                        message_index,
                    ),
                    ledger_inbound_id=self._render_plan_legacy_ref(
                        "inbound",
                        plan.plan_hash,
                        raw_inbound_id,
                        reply_to or "",
                        part.part_hash,
                        message_index,
                    ),
                    ledger_session_id=self._render_plan_legacy_ref(
                        "session",
                        plan.plan_hash,
                        raw_session_id,
                        part.part_hash,
                        message_index,
                    ),
                    ledger_correlation_id=self._render_plan_legacy_ref(
                        "correlation",
                        delivery_id,
                        raw_correlation_id,
                    ),
                    ledger_message_id=legacy_message_id,
                    network_call=lambda uuid_value, msg_type=msg_type, payload=payload, chunk_metadata=chunk_metadata: self._send_raw_message(
                        chat_id=chat_id,
                        msg_type=msg_type,
                        payload=payload,
                        reply_to=reply_to,
                        metadata=chunk_metadata,
                        uuid_value=uuid_value,
                    ),
                    require_returned_message_id=True,
                    terminal_exception_matcher=(
                        self._is_post_content_invalid_exception
                        if msg_type == "post"
                        else None
                    ),
                )
                result = (
                    response_or_result
                    if isinstance(response_or_result, SendResult)
                    else self._finalize_send_result(
                        response_or_result,
                        "render plan send failed",
                    )
                )
                if not result.success:
                    failure = self._render_plan_failure_class(result)
                    return SendResult(
                        success=False,
                        error=failure,
                        raw_response={
                            "failure_class": failure,
                            "failed_part_index": part_index,
                            "failed_chunk_index": part.chunk_index,
                            "chunk_count": part.chunk_count,
                            "chunk_group_hash": part.chunk_group,
                            "sent_part_count": sent_part_count,
                        },
                    )
                if result.message_id:
                    message_ids.append(legacy_message_id)
            if messages:
                sent_part_count += 1

        return SendResult(
            success=True,
            message_id=message_ids[-1] if message_ids else None,
            continuation_message_ids=tuple(message_ids) if len(message_ids) > 1 else (),
            raw_response={
                "fallback_events": fallback_events,
                "sent_part_count": sent_part_count,
            },
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Feishu message."""
        if self._gateway_event_state_dir is None:
            return self._delivery_audit_state_missing_result()
        if not self._client:
            return SendResult(success=False, error="Not connected")

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
        chunk_total = len(chunks)
        last_response = None
        chunk_message_ids: list[str] = []

        def remember_chunk_message_id(response_or_result: Any) -> None:
            if isinstance(response_or_result, SendResult):
                if response_or_result.success and response_or_result.message_id:
                    chunk_message_ids.append(str(response_or_result.message_id))
                return
            if self._response_succeeded(response_or_result):
                message_id = self._extract_response_field(response_or_result, "message_id")
                if message_id:
                    chunk_message_ids.append(str(message_id))

        try:
            for chunk_index, chunk in enumerate(chunks):
                msg_type, payload = self._build_outbound_payload(chunk)
                if self._gateway_event_state_dir is not None:
                    operation, operation_error = self._send_delivery_operation(
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                    if operation_error is not None:
                        return SendResult(success=False, error=operation_error)
                    chunk_metadata = self._metadata_for_send_chunk(
                        operation=operation,
                        metadata=metadata,
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                    )
                    delivery_id = self._delivery_id_for(
                        operation,
                        metadata=chunk_metadata,
                        parts=[chat_id, reply_to or "", msg_type, payload],
                    )
                    response_or_result = await self._audited_delivery(
                        delivery_id=delivery_id,
                        operation=operation,
                        target=f"feishu:chat:{chat_id}",
                        metadata=chunk_metadata,
                        inbound_id=self._delivery_metadata(
                            chunk_metadata, "inbound_id", reply_to or chat_id
                        ),
                        session_id=self._delivery_metadata(
                            chunk_metadata, "session_id", "session"
                        ),
                        correlation_id=self._delivery_metadata(
                            chunk_metadata, "correlation_id", delivery_id
                        ),
                        network_call=lambda uuid_value: self._send_raw_message(
                            chat_id=chat_id,
                            msg_type=msg_type,
                            payload=payload,
                            reply_to=reply_to,
                            metadata=chunk_metadata,
                            uuid_value=uuid_value,
                        ),
                        require_returned_message_id=True,
                        terminal_exception_matcher=(
                            self._is_post_content_invalid_exception
                            if msg_type == "post"
                            else None
                        ),
                    )
                    if isinstance(response_or_result, SendResult):
                        if response_or_result.success:
                            remember_chunk_message_id(response_or_result)
                            last_response = response_or_result
                            continue
                        if msg_type == "post" and self._is_post_content_invalid_result(
                            response_or_result
                        ):
                            logger.warning(
                                "[Feishu] Audited post payload rejected by API; falling back to plain text"
                            )
                            fallback_payload = json.dumps(
                                {"text": _strip_markdown_to_plain_text(chunk)},
                                ensure_ascii=False,
                            )
                            response_or_result = await self._audited_delivery(
                                delivery_id=self._derived_delivery_id(
                                    delivery_id,
                                    "post_text_fallback",
                                ),
                                operation=f"{operation}_text_fallback",
                                target=f"feishu:chat:{chat_id}",
                                metadata=chunk_metadata,
                                inbound_id=self._delivery_metadata(
                                    chunk_metadata, "inbound_id", reply_to or chat_id
                                ),
                                session_id=self._delivery_metadata(
                                    chunk_metadata, "session_id", "session"
                                ),
                                correlation_id=self._delivery_metadata(
                                    chunk_metadata, "correlation_id", delivery_id
                                ),
                                network_call=lambda uuid_value: self._send_raw_message(
                                    chat_id=chat_id,
                                    msg_type="text",
                                    payload=fallback_payload,
                                    reply_to=reply_to,
                                    metadata=chunk_metadata,
                                    uuid_value=uuid_value,
                                ),
                                require_returned_message_id=True,
                            )
                            if isinstance(response_or_result, SendResult):
                                if not response_or_result.success:
                                    return response_or_result
                                remember_chunk_message_id(response_or_result)
                                last_response = response_or_result
                                continue
                            remember_chunk_message_id(response_or_result)
                            last_response = response_or_result
                            continue
                        return response_or_result
                    remember_chunk_message_id(response_or_result)
                    last_response = response_or_result
                    continue
                try:
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id,
                        msg_type=msg_type,
                        payload=payload,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                except Exception as exc:
                    if msg_type != "post" or not _POST_CONTENT_INVALID_RE.search(str(exc)):
                        raise
                    logger.warning("[Feishu] Invalid post payload rejected by API; falling back to plain text")
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id,
                        msg_type="text",
                        payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                if (
                    msg_type == "post"
                    and not self._response_succeeded(response)
                    and _POST_CONTENT_INVALID_RE.search(str(getattr(response, "msg", "") or ""))
                ):
                    logger.warning("[Feishu] Post payload rejected by API response; falling back to plain text")
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id,
                        msg_type="text",
                        payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                remember_chunk_message_id(response)
                last_response = response

            if isinstance(last_response, SendResult):
                result = last_response
            else:
                result = self._finalize_send_result(last_response, "send failed")
            if result.success and len(chunk_message_ids) > 1:
                result.message_id = chunk_message_ids[-1]
                result.continuation_message_ids = tuple(chunk_message_ids)
            return result
        except Exception as exc:
            logger.error("[Feishu] Send error: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Edit a previously sent Feishu text/post message."""
        if self._gateway_event_state_dir is None:
            return self._delivery_audit_state_missing_result()
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not self._valid_feishu_message_id(message_id):
            return SendResult(success=False, error="Invalid Feishu message_id")

        content = self.format_message(content)
        try:
            msg_type, payload = self._build_outbound_payload(content)
            body = self._build_update_message_body(msg_type=msg_type, content=payload)
            request = self._build_update_message_request(message_id=message_id, request_body=body)
            if self._gateway_event_state_dir is not None:
                delivery_id = self._delivery_id_for(
                    "message_edit",
                    metadata=metadata,
                    parts=[chat_id, message_id, msg_type, payload],
                )
                ownership_proof, proof_failure = self._current_edit_ownership_proof(
                    chat_id=chat_id,
                    message_id=message_id,
                    metadata=metadata,
                )
                if proof_failure is not None:
                    lifecycle_gap_result = (
                        await self._fail_if_lifecycle_failed_append_missing(
                            delivery_id=delivery_id,
                            action="edit",
                            target=f"feishu:message:{message_id}",
                            metadata=metadata,
                            failure_class=proof_failure,
                            message_id=message_id,
                            original_proof=ownership_proof,
                        )
                    )
                    if lifecycle_gap_result is not None:
                        return lifecycle_gap_result
                    return SendResult(success=False, error=proof_failure)
                response_or_result = await self._audited_delivery(
                    delivery_id=delivery_id,
                    operation="message_edit",
                    target=f"feishu:message:{message_id}",
                    metadata=metadata,
                    inbound_id=self._delivery_metadata(metadata, "inbound_id", message_id),
                    session_id=self._delivery_metadata(metadata, "session_id", "session"),
                    correlation_id=self._delivery_metadata(
                        metadata, "correlation_id", delivery_id
                    ),
                    network_call=lambda _uuid_value: asyncio.to_thread(
                        self._client.im.v1.message.update, request
                    ),
                    require_returned_message_id=False,
                    existing_message_id=message_id,
                    current_delivery_proof=ownership_proof,
                    terminal_exception_matcher=(
                        self._is_post_content_invalid_exception
                        if msg_type == "post"
                        else None
                    ),
                )
                if isinstance(response_or_result, SendResult):
                    if msg_type != "post" or not self._is_post_content_invalid_result(
                        response_or_result
                    ):
                        return response_or_result
                    logger.warning(
                        "[Feishu] Audited post update payload rejected by API; falling back to plain text"
                    )
                    fallback_body = self._build_update_message_body(
                        msg_type="text",
                        content=json.dumps(
                            {"text": _strip_markdown_to_plain_text(content)},
                            ensure_ascii=False,
                        ),
                    )
                    fallback_request = self._build_update_message_request(
                        message_id=message_id,
                        request_body=fallback_body,
                    )
                    response_or_result = await self._audited_delivery(
                        delivery_id=self._derived_delivery_id(
                            delivery_id,
                            "post_text_fallback",
                        ),
                        operation="message_edit_text_fallback",
                        target=f"feishu:message:{message_id}",
                        metadata=metadata,
                        inbound_id=self._delivery_metadata(
                            metadata, "inbound_id", message_id
                        ),
                        session_id=self._delivery_metadata(
                            metadata, "session_id", "session"
                        ),
                        correlation_id=self._delivery_metadata(
                            metadata, "correlation_id", delivery_id
                        ),
                        network_call=lambda _uuid_value: asyncio.to_thread(
                            self._client.im.v1.message.update, fallback_request
                        ),
                        require_returned_message_id=False,
                        existing_message_id=message_id,
                        current_delivery_proof=ownership_proof,
                    )
                    if isinstance(response_or_result, SendResult):
                        return response_or_result
                response = response_or_result
            else:
                response = await asyncio.to_thread(self._client.im.v1.message.update, request)
            result = self._finalize_send_result(response, "update failed")
            if not result.success and msg_type == "post" and _POST_CONTENT_INVALID_RE.search(result.error or ""):
                logger.warning("[Feishu] Invalid post update payload rejected by API; falling back to plain text")
                fallback_body = self._build_update_message_body(
                    msg_type="text",
                    content=json.dumps({"text": _strip_markdown_to_plain_text(content)}, ensure_ascii=False),
                )
                fallback_request = self._build_update_message_request(message_id=message_id, request_body=fallback_body)
                fallback_response = await asyncio.to_thread(self._client.im.v1.message.update, fallback_request)
                result = self._finalize_send_result(fallback_response, "update failed")
            if result.success:
                result.message_id = message_id
            return result
        except Exception as exc:
            logger.error("[Feishu] Failed to edit message %s: %s", message_id, exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive card with approval buttons.

        The buttons carry ``hermes_action`` in their value dict so that
        ``_handle_card_action_event`` can intercept them and call
        ``resolve_gateway_approval()`` to unblock the waiting agent thread.
        """
        if self._gateway_event_state_dir is None:
            return self._delivery_audit_state_missing_result()
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            approval_id = next(self._approval_counter)
            cmd_preview = command[:3000] + "..." if len(command) > 3000 else command
            card_scope = self._build_card_scope_metadata(
                chat_id=chat_id,
                session_key=session_key,
                metadata=metadata,
            )

            def _btn(label: str, action_name: str, btn_type: str = "default") -> dict:
                return {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": btn_type,
                    "value": {
                        "hermes_action": action_name,
                        "approval_id": approval_id,
                        "hermes_card_scope": dict(card_scope),
                    },
                }

            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": "⚠️ Command Approval Required", "tag": "plain_text"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"```\n{cmd_preview}\n```\n**Reason:** {description}",
                    },
                    {
                        "tag": "action",
                        "actions": [
                            _btn("✅ Allow Once", "approve_once", "primary"),
                            _btn("✅ Session", "approve_session"),
                            _btn("✅ Always", "approve_always"),
                            _btn("❌ Deny", "deny", "danger"),
                        ],
                    },
                ],
            }

            payload = json.dumps(card, ensure_ascii=False)
            if self._gateway_event_state_dir is not None:
                operation = "approval_prompt_card_create"
                delivery_id = self._delivery_id_for(
                    operation,
                    metadata=metadata,
                    parts=[chat_id, session_key, str(approval_id), payload],
                )
                response_or_result = await self._audited_delivery(
                    delivery_id=delivery_id,
                    operation=operation,
                    target=f"feishu:chat:{chat_id}",
                    inbound_id=self._delivery_metadata(metadata, "inbound_id", chat_id),
                    session_id=self._delivery_metadata(
                        metadata, "session_id", session_key or "session"
                    ),
                    correlation_id=self._delivery_metadata(
                        metadata, "correlation_id", delivery_id
                    ),
                    network_call=lambda uuid_value: self._send_raw_message(
                        chat_id=chat_id,
                        msg_type="interactive",
                        payload=payload,
                        reply_to=None,
                        metadata=metadata,
                        uuid_value=uuid_value,
                    ),
                    require_returned_message_id=True,
                )
                if isinstance(response_or_result, SendResult):
                    return response_or_result
                response = response_or_result
            else:
                response = await self._feishu_send_with_retry(
                    chat_id=chat_id,
                    msg_type="interactive",
                    payload=payload,
                    reply_to=None,
                    metadata=metadata,
                )

            result = self._finalize_send_result(response, "send_exec_approval failed")
            if result.success:
                approval_state = {
                    "session_key": session_key,
                    "message_id": result.message_id or "",
                    "chat_id": chat_id,
                    **card_scope,
                }
                if self._gateway_event_state_dir is not None:
                    approval_state.update(
                        {
                            "prompt_card": copy.deepcopy(card),
                            "resolution_attempt": 0,
                            "inbound_id": self._delivery_metadata(metadata, "inbound_id", chat_id),
                            "session_id": self._delivery_metadata(
                                metadata, "session_id", session_key or "session"
                            ),
                            "correlation_id": self._delivery_metadata(
                                metadata,
                                "correlation_id",
                                result.message_id or str(approval_id),
                            ),
                        }
                    )
                self._approval_state[approval_id] = approval_state
            return result
        except Exception as exc:
            logger.warning("[Feishu] send_exec_approval failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    @staticmethod
    def _build_update_prompt_card(*, prompt: str, default: str, prompt_id: int) -> Dict[str, Any]:
        default_hint = f"\n\nDefault: `{default}`" if default else ""

        def _btn(label: str, answer: str, btn_type: str) -> dict:
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": btn_type,
                "value": {
                    "hermes_update_prompt_action": answer,
                    "update_prompt_id": prompt_id,
                },
            }

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "⚕ Update Needs Your Input", "tag": "plain_text"},
                "template": "orange",
            },
            "elements": [
                {"tag": "markdown", "content": f"{prompt}{default_hint}"},
                {
                    "tag": "action",
                    "actions": [
                        _btn("✓ Yes", "y", "primary"),
                        _btn("✗ No", "n", "danger"),
                    ],
                },
            ],
        }

    async def send_update_prompt(
        self, chat_id: str, prompt: str, default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive update prompt with Yes/No buttons."""
        if self._gateway_event_state_dir is None:
            return self._delivery_audit_state_missing_result()
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            prompt_id = next(self._update_prompt_counter)
            card_scope = self._build_card_scope_metadata(
                chat_id=chat_id,
                session_key=session_key,
                metadata=metadata,
            )
            card_payload = self._build_update_prompt_card(
                prompt=prompt,
                default=default,
                prompt_id=prompt_id,
            )
            payload = json.dumps(card_payload, ensure_ascii=False)
            try:
                card_payload = json.loads(payload)
                for element in card_payload.get("elements", []):
                    for action in element.get("actions", []) if isinstance(element, dict) else []:
                        value = action.get("value") if isinstance(action, dict) else None
                        if isinstance(value, dict):
                            value["hermes_card_scope"] = dict(card_scope)
                payload = json.dumps(card_payload, ensure_ascii=False)
            except Exception:
                logger.debug("[Feishu] Failed to attach update prompt card scope metadata", exc_info=True)
            if self._gateway_event_state_dir is not None:
                operation = "update_prompt_card_create"
                delivery_id = self._delivery_id_for(
                    operation,
                    metadata=metadata,
                    parts=[chat_id, session_key, str(prompt_id), payload],
                )
                response_or_result = await self._audited_delivery(
                    delivery_id=delivery_id,
                    operation=operation,
                    target=f"feishu:chat:{chat_id}",
                    inbound_id=self._delivery_metadata(metadata, "inbound_id", chat_id),
                    session_id=self._delivery_metadata(
                        metadata, "session_id", session_key or "session"
                    ),
                    correlation_id=self._delivery_metadata(
                        metadata, "correlation_id", delivery_id
                    ),
                    network_call=lambda uuid_value: self._send_raw_message(
                        chat_id=chat_id,
                        msg_type="interactive",
                        payload=payload,
                        reply_to=None,
                        metadata=metadata,
                        uuid_value=uuid_value,
                    ),
                    require_returned_message_id=True,
                )
                if isinstance(response_or_result, SendResult):
                    return response_or_result
                response = response_or_result
            else:
                response = await self._feishu_send_with_retry(
                    chat_id=chat_id,
                    msg_type="interactive",
                    payload=payload,
                    reply_to=None,
                    metadata=metadata,
                )

            result = self._finalize_send_result(response, "send_update_prompt failed")
            if result.success:
                prompt_state = {
                    "session_key": session_key,
                    "message_id": result.message_id or "",
                    "chat_id": chat_id,
                    **card_scope,
                }
                if self._gateway_event_state_dir is not None:
                    prompt_state.update(
                        {
                            "prompt_card": copy.deepcopy(card_payload),
                            "resolution_attempt": 0,
                            "inbound_id": self._delivery_metadata(metadata, "inbound_id", chat_id),
                            "session_id": self._delivery_metadata(
                                metadata, "session_id", session_key or "session"
                            ),
                            "correlation_id": self._delivery_metadata(
                                metadata,
                                "correlation_id",
                                result.message_id or str(prompt_id),
                            ),
                        }
                    )
                self._update_prompt_state[prompt_id] = prompt_state
            return result
        except Exception as exc:
            logger.warning("[Feishu] send_update_prompt failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    @staticmethod
    def _build_resolved_approval_card(*, choice: str, user_name: str) -> Dict[str, Any]:
        """Build raw card JSON for a resolved approval action."""
        icon = "❌" if choice == "deny" else "✅"
        label = _APPROVAL_LABEL_MAP.get(choice, "Resolved")
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": f"{icon} {label}", "tag": "plain_text"},
                "template": "red" if choice == "deny" else "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"{icon} **{label}** by {user_name}",
                },
            ],
        }

    @staticmethod
    def _build_resolved_update_prompt_card(*, answer: str, user_name: str) -> Dict[str, Any]:
        yes = answer == "y"
        label = "Yes" if yes else "No"
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": f"{'✅' if yes else '❌'} Update prompt answered: {label}", "tag": "plain_text"},
                "template": "green" if yes else "red",
            },
            "elements": [
                {"tag": "markdown", "content": f"Answered by **{user_name}**"},
            ],
        }

    @staticmethod
    def _write_update_prompt_response(answer: str) -> None:
        response_path = get_hermes_home() / ".update_response"
        tmp_path = response_path.with_suffix(".tmp")
        tmp_path.write_text(answer)
        tmp_path.replace(response_path)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio to Feishu as a file attachment plus optional caption."""
        return await self._send_uploaded_file_message(
            chat_id=chat_id,
            file_path=audio_path,
            reply_to=reply_to,
            metadata=metadata,
            caption=caption,
            outbound_message_type="audio",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment to Feishu."""
        return await self._send_uploaded_file_message(
            chat_id=chat_id,
            file_path=file_path,
            reply_to=reply_to,
            metadata=metadata,
            caption=caption,
            file_name=file_name,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file to Feishu."""
        return await self._send_uploaded_file_message(
            chat_id=chat_id,
            file_path=video_path,
            reply_to=reply_to,
            metadata=metadata,
            caption=caption,
            outbound_message_type="media",
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file to Feishu."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        preflight = await self._preflight_attachment_upload(
            chat_id=chat_id,
            file_path=image_path,
            reply_to=reply_to,
            metadata=metadata,
            declared_mime_class="image",
            upload_kind="image",
        )
        if preflight is not None:
            return preflight
        if not os.path.exists(image_path):
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class="image",
                failure_class="feishu_attachment_local_file_missing",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error="Image file not found")

        try:
            import io as _io
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            # Wrap in BytesIO so lark SDK's MultipartEncoder can read .name and .tell()
            image_file = _io.BytesIO(image_bytes)
            image_file.name = os.path.basename(image_path)
            body = self._build_image_upload_body(
                image_type=_FEISHU_IMAGE_UPLOAD_TYPE,
                image=image_file,
            )
            request = self._build_image_upload_request(body)
            upload_response = await asyncio.to_thread(self._client.im.v1.image.create, request)
            image_key = self._extract_response_field(upload_response, "image_key")
        except Exception as exc:
            logger.error(
                "[Feishu] Failed to upload image attachment: failure_class=%s",
                exc.__class__.__name__,
            )
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class="image",
                failure_class="feishu_image_upload_exception",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error=exc.__class__.__name__)
        if not image_key:
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class="image",
                failure_class="feishu_image_upload_missing_image_key",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error="Feishu image upload missing image_key")

        if caption:
            post_payload = self._build_media_post_payload(
                caption=caption,
                media_tag={"tag": "img", "image_key": image_key},
            )
            return await self._send_audited_or_legacy_message(
                chat_id=chat_id,
                msg_type="post",
                payload=post_payload,
                reply_to=reply_to,
                metadata=metadata,
                default_message="image send failed",
            )
        return await self._send_audited_or_legacy_message(
            chat_id=chat_id,
            msg_type="image",
            payload=json.dumps({"image_key": image_key}, ensure_ascii=False),
            reply_to=reply_to,
            metadata=metadata,
            default_message="image send failed",
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Feishu bot API does not expose a typing indicator."""
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Reject raw remote image URLs before any download or fallback side effect."""
        del chat_id, image_url, caption, reply_to
        return await self._deny_raw_remote_attachment_upload(
            metadata=metadata,
            declared_mime_class="image",
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Reject raw remote animation URLs before any download or fallback side effect."""
        del chat_id, animation_url, caption, reply_to
        return await self._deny_raw_remote_attachment_upload(
            metadata=metadata,
            declared_mime_class="media",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return real chat metadata from Feishu when available."""
        fallback = {
            "chat_id": chat_id,
            "name": chat_id,
            "type": "dm",
            "reliable": False,
            "failure_class": "feishu_chat_info_unavailable",
        }
        if not self._client:
            return fallback

        cached = self._chat_info_cache.get(chat_id)
        if cached is not None:
            return dict(cached)

        try:
            request = self._build_get_chat_request(chat_id)
            response = await asyncio.to_thread(self._client.im.v1.chat.get, request)
            if not response or getattr(response, "success", lambda: False)() is False:
                code = getattr(response, "code", "unknown")
                msg = getattr(response, "msg", "chat lookup failed")
                logger.warning("[Feishu] Failed to get chat info for %s: [%s] %s", chat_id, code, msg)
                return fallback

            data = getattr(response, "data", None)
            raw_chat_type = str(getattr(data, "chat_type", "") or "").strip().lower()
            info = {
                "chat_id": chat_id,
                "name": str(getattr(data, "name", None) or chat_id),
                "type": self._map_chat_type(raw_chat_type),
                "raw_type": raw_chat_type or None,
                "reliable": True,
            }
            self._chat_info_cache[chat_id] = info
            return dict(info)
        except Exception:
            logger.warning("[Feishu] Failed to get chat info for %s", chat_id, exc_info=True)
            return fallback

    def format_message(self, content: str) -> str:
        """Feishu text messages are plain text by default."""
        return content.strip()

    # =========================================================================
    # Inbound event handlers
    # =========================================================================

    def _on_message_event(self, data: Any, transport_kind: str = "websocket") -> None:
        """Normalize Feishu inbound events into MessageEvent.

        Called by the lark_oapi SDK's event dispatcher on a background thread.
        If the adapter loop is not currently accepting callbacks (brief window
        during startup/restart or network-flap reconnect), the event is queued
        for replay instead of dropped.
        """
        transport = self._normalize_inbound_transport_kind(transport_kind)
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            start_drainer = self._enqueue_pending_inbound_event(data, transport)
            if start_drainer:
                threading.Thread(
                    target=self._drain_pending_inbound_events,
                    name="feishu-pending-inbound-drainer",
                    daemon=True,
                ).start()
            return
        self._submit_on_loop(
            loop,
            self._handle_message_event_data(data, transport_kind=transport),
        )

    @staticmethod
    def _normalize_inbound_transport_kind(transport_kind: str | None) -> str:
        transport = str(transport_kind or "").strip().lower()
        return transport if transport in {"webhook", "websocket"} else "websocket"

    @staticmethod
    def _pending_inbound_event_parts(item: Any) -> tuple[Any, str]:
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and str(item[1] or "").strip().lower() in {"webhook", "websocket"}
        ):
            return item[0], str(item[1]).strip().lower()
        return item, "websocket"

    def _enqueue_pending_inbound_event(
        self,
        data: Any,
        transport_kind: str = "websocket",
    ) -> bool:
        """Append an event to the pending-inbound queue.

        Returns True if the caller should spawn a drainer thread (no drainer
        currently scheduled), False if a drainer is already running and will
        pick up the new event on its next pass.
        """
        transport = self._normalize_inbound_transport_kind(transport_kind)
        with self._pending_inbound_lock:
            if len(self._pending_inbound_events) >= self._pending_inbound_max_depth:
                # Queue full — drop the oldest to make room. This happens only
                # if the loop stays unavailable for an extended period AND the
                # WS keeps firing callbacks. Still better than silent drops.
                dropped = self._pending_inbound_events.pop(0)
                try:
                    dropped_data, _dropped_transport = self._pending_inbound_event_parts(dropped)
                    event = getattr(dropped_data, "event", None)
                    message = getattr(event, "message", None)
                    message_id = str(getattr(message, "message_id", "") or "unknown")
                except Exception:
                    message_id = "unknown"
                logger.error(
                    "[Feishu] Pending-inbound queue full (%d); dropped oldest event %s",
                    self._pending_inbound_max_depth,
                    message_id,
                )
            self._pending_inbound_events.append((data, transport))
            depth = len(self._pending_inbound_events)
            should_start = not self._pending_drain_scheduled
            if should_start:
                self._pending_drain_scheduled = True
        logger.warning(
            "[Feishu] Queued inbound event for replay (loop not ready, queue depth=%d)",
            depth,
        )
        return should_start

    def _drain_pending_inbound_events(self) -> None:
        """Replay queued inbound events once the adapter loop is ready.

        Runs in a dedicated daemon thread. Polls ``_running`` and
        ``_loop_accepts_callbacks`` until events can be dispatched or the
        adapter shuts down. A single drainer handles the entire queue;
        concurrent ``_on_message_event`` calls just append.
        """
        poll_interval = 0.25
        max_wait_seconds = 120.0  # safety cap: drop queue after 2 minutes
        waited = 0.0
        try:
            while True:
                if not getattr(self, "_running", True):
                    # Adapter shutting down — drop queued events rather than
                    # holding them against a closed loop.
                    with self._pending_inbound_lock:
                        dropped = len(self._pending_inbound_events)
                        self._pending_inbound_events.clear()
                    if dropped:
                        logger.warning(
                            "[Feishu] Dropped %d queued inbound event(s) during shutdown",
                            dropped,
                        )
                    return
                loop = self._loop
                if self._loop_accepts_callbacks(loop):
                    with self._pending_inbound_lock:
                        batch = self._pending_inbound_events[:]
                        self._pending_inbound_events.clear()
                    if not batch:
                        # Queue emptied between check and grab; done.
                        with self._pending_inbound_lock:
                            if not self._pending_inbound_events:
                                return
                        continue
                    dispatched = 0
                    requeue: List[Any] = []
                    for pending in batch:
                        pending_data, pending_transport = self._pending_inbound_event_parts(
                            pending
                        )
                        if self._submit_on_loop(
                            loop,
                            self._handle_message_event_data(
                                pending_data,
                                transport_kind=pending_transport,
                            ),
                        ):
                            dispatched += 1
                        else:
                            # Loop closed/unavailable — requeue and poll again.
                            requeue.append(pending)
                    if requeue:
                        with self._pending_inbound_lock:
                            self._pending_inbound_events[:0] = requeue
                    if dispatched:
                        logger.info(
                            "[Feishu] Replayed %d queued inbound event(s)",
                            dispatched,
                        )
                    if not requeue:
                        # Successfully drained; check if more arrived while
                        # we were dispatching and exit if not.
                        with self._pending_inbound_lock:
                            if not self._pending_inbound_events:
                                return
                    # More events queued or requeue pending — loop again.
                    continue
                if waited >= max_wait_seconds:
                    with self._pending_inbound_lock:
                        dropped = len(self._pending_inbound_events)
                        self._pending_inbound_events.clear()
                    logger.error(
                        "[Feishu] Adapter loop unavailable for %.0fs; "
                        "dropped %d queued inbound event(s)",
                        max_wait_seconds,
                        dropped,
                    )
                    return
                time.sleep(poll_interval)
                waited += poll_interval
        finally:
            with self._pending_inbound_lock:
                self._pending_drain_scheduled = False

    async def _handle_message_event_data(
        self,
        data: Any,
        *,
        transport_kind: str = "websocket",
    ) -> None:
        """Shared inbound message handling for websocket and webhook transports."""
        transport = self._normalize_inbound_transport_kind(transport_kind)
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if not message or not sender or not getattr(sender, "sender_id", None):
            logger.debug("[Feishu] Dropping malformed inbound event: missing message/sender")
            return

        message_id = getattr(message, "message_id", None)
        if not message_id:
            logger.debug("[Feishu] Dropping inbound event without message_id")
            return
        if self._gateway_event_state_dir is None:
            if self._is_duplicate(message_id):
                logger.debug("[Feishu] Dropping duplicate message_id: %s", message_id)
                return
        else:
            self._remember_legacy_seen_message_id(message_id)

        reason = self._admit(sender, message)
        if reason is not None:
            logger.debug("[Feishu] dropping inbound event: %s", reason)
            return

        chat_type = getattr(message, "chat_type", "p2p")
        await self._process_inbound_message(
            data=data,
            message=message,
            sender_id=getattr(sender, "sender_id", None),
            chat_type=chat_type,
            message_id=message_id,
            is_bot=_is_bot_sender(sender),
            transport_kind=transport,
        )

    def _on_message_read_event(self, data: P2ImMessageMessageReadV1) -> None:
        """Schedule Feishu read acknowledgements through the Hermes event ledger."""
        if self._gateway_event_state_dir is None:
            return
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            logger.warning(
                "[Feishu] Dropping read event before adapter loop is ready: "
                "reason=feishu_read_ack_loop_unavailable"
            )
            return
        self._submit_on_loop(loop, self._handle_message_read_event(data))

    async def _handle_message_read_event(self, data: P2ImMessageMessageReadV1) -> None:
        """Record Feishu read acknowledgements through the Hermes event ledger."""
        event = getattr(data, "event", None)
        message_id_list = self._message_id_list_field(event, "message_id_list")
        ack_event_id = self._feishu_event_id(data)
        if not message_id_list:
            logger.warning(
                "[Feishu] Dropping malformed read event: reason=feishu_read_ack_missing_message_id_list "
                "ack_event_id_present=%s",
                bool(ack_event_id),
            )
            return
        if not ack_event_id:
            logger.warning(
                "[Feishu] Dropping malformed read event: reason=feishu_read_ack_missing_event_id "
                "message_id_present=true",
            )
            return

        timestamp = int(time.time())
        for message_id in message_id_list:
            await self._apply_gateway_event(
                {
                    "type": "feishu_ack",
                    "message_id": message_id,
                    "ack_event_id": f"{ack_event_id}:{message_id}",
                    "timestamp": timestamp,
                }
            )

    def _on_bot_added_to_chat(self, data: Any) -> None:
        """Handle bot being added to a group chat."""
        event = getattr(data, "event", None)
        chat_id = str(getattr(event, "chat_id", "") or "")
        logger.info("[Feishu] Bot added to chat: %s", chat_id)
        self._chat_info_cache.pop(chat_id, None)

    def _on_bot_removed_from_chat(self, data: Any) -> None:
        """Handle bot being removed from a group chat."""
        event = getattr(data, "event", None)
        chat_id = str(getattr(event, "chat_id", "") or "")
        logger.info("[Feishu] Bot removed from chat: %s", chat_id)
        self._chat_info_cache.pop(chat_id, None)

    def _on_p2p_chat_entered(self, data: Any) -> None:
        logger.debug("[Feishu] User entered P2P chat with bot")

    def _on_message_recalled(self, data: Any) -> None:
        logger.debug("[Feishu] Message recalled by user")

    def _on_drive_comment_event(self, data: Any) -> None:
        """Handle drive document comment notification (drive.notice.comment_add_v1).

        Delegates to :mod:`gateway.platforms.feishu_comment` for parsing,
        logging, and reaction.  Scheduling follows the same
        ``run_coroutine_threadsafe`` pattern used by ``_on_message_event``.
        """
        from gateway.platforms.feishu_comment import handle_drive_comment_event

        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            logger.warning("[Feishu] Dropping drive comment event before adapter loop is ready")
            return
        self._submit_on_loop(
            loop,
            handle_drive_comment_event(self._client, data, self_open_id=self._bot_open_id),
        )

    def _on_reaction_event(self, event_type: str, data: Any) -> None:
        """Route user reactions on bot messages as synthetic text events."""
        event = getattr(data, "event", None)
        message_id = str(getattr(event, "message_id", "") or "")
        operator_type = str(getattr(event, "operator_type", "") or "")
        reaction_type_obj = getattr(event, "reaction_type", None)
        emoji_type = str(getattr(reaction_type_obj, "emoji_type", "") or "")
        action = "added" if "created" in event_type else "removed"
        logger.debug(
            "[Feishu] Reaction %s on message %s (operator_type=%s, emoji=%s)",
            action,
            message_id,
            operator_type,
            emoji_type,
        )
        # Drop bot/app-origin reactions to break the feedback loop from our
        # own lifecycle reactions. A human reacting with the same emoji (e.g.
        # clicking Typing on a bot message) is still routed through.
        loop = self._loop
        if (
            operator_type in {"bot", "app"}
            or not message_id
            or loop is None
            or bool(getattr(loop, "is_closed", lambda: False)())
        ):
            return
        reaction_event_id = self._feishu_event_id(data)
        if not self._feishu_legacy_descriptor_entry_gate_sync(
            surface="feishu.reaction",
            failure_class="feishu_legacy_descriptor_requires_broker",
            correlation_id=reaction_event_id,
            descriptor_seed=reaction_event_id,
            drop_label="reaction",
        ):
            return
        self._submit_on_loop(loop, self._handle_reaction_event(event_type, data))

    def _on_card_action_trigger(self, data: Any) -> Any:
        """Handle card-action callback from the Feishu SDK (synchronous).

        For approval and update-prompt actions: parses the event once and
        schedules async resolution. Audited mode returns no inline resolved
        card because the visible update must go through ledgered Feishu
        ``message.update`` first; non-audited mode still returns an inline
        callback card for the legacy synchronous UX.

        For other card actions: delegates to ``_handle_card_action_event``.
        """
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            logger.warning("[Feishu] Dropping card action before adapter loop is ready")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}
        hermes_action = action_value.get("hermes_action") if isinstance(action_value, dict) else None
        update_prompt_action = (
            action_value.get("hermes_update_prompt_action")
            if isinstance(action_value, dict) else None
        )

        if self._is_brokered_card_action_value(action_value):
            return self._handle_brokered_card_action_trigger(
                event=event,
                action_value=action_value,
                loop=loop,
            )
        if hermes_action:
            return self._handle_approval_card_action(event=event, action_value=action_value, loop=loop)
        if update_prompt_action:
            return self._handle_update_prompt_card_action(
                event=event,
                action_value=action_value,
                loop=loop,
            )

        token = str(getattr(event, "token", "") or "")
        if self._gateway_event_state_dir is None:
            logger.warning(
                "[Feishu] Dropping card action before submit: "
                "reason=feishu_legacy_descriptor_audit_state_missing surface=feishu.card_action "
                "failure_class=feishu_legacy_card_action_requires_broker"
            )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        audited = self._apply_feishu_legacy_descriptor_denied_sync(
            surface="feishu.card_action",
            failure_class="feishu_legacy_card_action_requires_broker",
            correlation_id=token,
            descriptor_seed=token,
        )
        if not audited:
            logger.warning(
                "[Feishu] Dropping card action before submit after audit failure: "
                "reason=feishu_legacy_descriptor_denied_apply_failed surface=feishu.card_action "
                "failure_class=feishu_legacy_card_action_requires_broker"
            )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        if P2CardActionTriggerResponse is None:
            return None
        return P2CardActionTriggerResponse()

    def _handle_brokered_card_action_trigger(
        self,
        *,
        event: Any,
        action_value: Dict[str, Any],
        loop: Any,
    ) -> Any:
        self._submit_on_loop(
            loop,
            self._resolve_brokered_card_action_trigger(event=event, action_value=action_value),
        )
        if P2CardActionTriggerResponse is None:
            return None
        return P2CardActionTriggerResponse()

    @staticmethod
    def _loop_accepts_callbacks(loop: Any) -> bool:
        """Return True when the adapter loop can accept thread-safe submissions."""
        return loop is not None and not bool(getattr(loop, "is_closed", lambda: False)())

    def _submit_on_loop(self, loop: Any, coro: Any) -> bool:
        """Schedule background work on the adapter loop with shared failure logging."""
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            coro, loop,
            logger=logger,
            log_message="[Feishu] Failed to schedule background callback work",
            log_level=logging.WARNING,
        )
        if future is None:
            return False
        future.add_done_callback(self._log_background_failure)
        return True

    def _is_interactive_operator_authorized(self, open_id: str) -> bool:
        """Return whether this card-action operator may answer gated prompts."""
        normalized = str(open_id or "").strip()
        if not normalized:
            return False
        allowed_ids = set(self._admins) | set(self._allowed_group_users)
        if not allowed_ids:
            return True
        return "*" in allowed_ids or normalized in allowed_ids

    @staticmethod
    def _session_key_scope_parts(session_key: str) -> Dict[str, Optional[str]]:
        """Extract Feishu chat scope evidence from a Hermes session key."""
        parts = str(session_key or "").split(":")
        if len(parts) < 4 or parts[0:3] != ["agent", "main", "feishu"]:
            return {}
        chat_type = FeishuAdapter._map_chat_type(parts[3])
        if chat_type not in {"dm", "group", "forum"}:
            return {}

        scope: Dict[str, Optional[str]] = {"chat_type": chat_type}
        if len(parts) >= 5:
            scope["chat_id"] = parts[4]
        return scope

    @classmethod
    def _build_card_scope_metadata(
        cls,
        *,
        chat_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build card/session scope evidence that can survive card callbacks."""
        scope = cls._session_key_scope_parts(session_key)
        scope["chat_id"] = str(scope.get("chat_id") or chat_id or "")
        if metadata:
            if metadata.get("thread_id"):
                scope["thread_id"] = str(metadata.get("thread_id"))
            raw_chat_type = metadata.get("chat_type") or metadata.get("feishu_chat_type")
            if raw_chat_type:
                scope["chat_type"] = cls._map_chat_type(str(raw_chat_type))
        return {k: v for k, v in scope.items() if v}

    @staticmethod
    def _chat_info_is_reliable(chat_info: Dict[str, Any]) -> bool:
        return chat_info.get("reliable") is not False

    @staticmethod
    def _reliable_chat_info_type(chat_info: Dict[str, Any]) -> Optional[str]:
        if not FeishuAdapter._chat_info_is_reliable(chat_info):
            return None
        resolved = str(chat_info.get("type") or "").strip().lower()
        if resolved in {"dm", "group", "forum"}:
            return resolved
        if resolved == "p2p":
            return "dm"
        return None

    @staticmethod
    def _scope_from_action_value(action_value: Dict[str, Any]) -> Dict[str, Any]:
        scope = action_value.get("hermes_card_scope") if isinstance(action_value, dict) else None
        return dict(scope) if isinstance(scope, dict) else {}

    def _scope_from_saved_card_state(self, action_value: Dict[str, Any]) -> Dict[str, Any]:
        state: Optional[Dict[str, Any]] = None
        if "approval_id" in action_value:
            state = self._approval_state.get(action_value.get("approval_id"))
        elif "update_prompt_id" in action_value:
            state = self._update_prompt_state.get(action_value.get("update_prompt_id"))
        if not state:
            return {}
        return {
            key: state.get(key)
            for key in ("chat_id", "chat_type", "thread_id")
            if state.get(key)
        }

    @staticmethod
    def _scope_matches_chat(scope: Dict[str, Any], chat_id: str) -> bool:
        scope_chat_id = str(scope.get("chat_id") or "").strip()
        return not scope_chat_id or scope_chat_id == str(chat_id or "").strip()

    def _resolve_card_action_chat_type(
        self,
        *,
        chat_info: Dict[str, Any],
        action_value: Dict[str, Any],
        thread_id: Optional[str],
        chat_id: str,
    ) -> Optional[str]:
        reliable_type = self._reliable_chat_info_type(chat_info)
        if reliable_type:
            return reliable_type

        scope = self._scope_from_action_value(action_value)
        if not scope:
            scope = self._scope_from_saved_card_state(action_value)
        if not self._scope_matches_chat(scope, chat_id):
            return None
        raw_type = scope.get("chat_type")
        if raw_type:
            mapped = self._map_chat_type(str(raw_type))
            if mapped in {"dm", "group", "forum"}:
                return mapped

        return None

    @staticmethod
    def _feishu_broker_context_present() -> bool:
        return current_feishu_broker_context() is not None

    def _delivery_audit_state_missing_result(self) -> SendResult:
        logger.warning(
            "[Feishu] Refusing outbound delivery before SDK delivery: "
            "reason=%s",
            self._DELIVERY_AUDIT_STATE_MISSING,
        )
        return SendResult(
            success=False,
            error=self._DELIVERY_AUDIT_STATE_MISSING,
        )

    @staticmethod
    def _feishu_audit_hash(value: str) -> str:
        h = 0xCBF29CE484222325
        for byte in str(value).encode("utf-8", "surrogatepass"):
            h ^= byte
            h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        return f"fnv1a64:{h:016x}"

    @staticmethod
    def _safe_feishu_audit_correlation_id(value: Optional[str]) -> str:
        text = str(value or "").strip()
        if text:
            return f"feishu-legacy-denial:{FeishuAdapter._feishu_audit_hash(text)}"
        return "feishu-legacy-denial"

    @classmethod
    def _is_brokered_card_action_value(cls, value: Any) -> bool:
        return isinstance(value, dict) and cls._is_broker_action_id(value.get("action_id"))

    @classmethod
    def _is_broker_action_id(cls, value: Any) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"broker_action:sha256:[a-f0-9]{64}", value) is not None
        )

    @classmethod
    def _is_broker_grant_handle(cls, value: Any) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"broker_grant_handle:sha256:[a-f0-9]{64}", value)
            is not None
        )

    @staticmethod
    def _broker_context_route_key(route_partition_hash: str) -> str:
        return f"route_snapshot:{route_partition_hash}"

    def _new_broker_action_id(self, action_kind: str) -> str:
        factory = getattr(self, "_feishu_broker_action_id_factory", None)
        if callable(factory):
            value = factory(action_kind)
            if self._is_broker_action_id(value):
                return value
        return "broker_action:" + self._sha256_ref_text(
            f"feishu_broker_action\x1f{action_kind}\x1f{uuid.uuid4().hex}"
        )

    def _new_broker_grant_handle(self, action_id: str) -> str:
        factory = getattr(self, "_feishu_broker_grant_factory", None)
        if callable(factory):
            value = factory(action_id)
            if self._is_broker_grant_handle(value):
                return value
        return "broker_grant_handle:" + self._sha256_ref_text(
            f"feishu_broker_grant\x1f{action_id}\x1f{uuid.uuid4().hex}"
        )

    async def _resolve_brokered_card_action_trigger(
        self,
        *,
        event: Any,
        action_value: Dict[str, Any],
    ) -> None:
        action_id = action_value.get("action_id")
        route_partition_hash = self._broker_card_route_partition_hash(event)
        route_snapshot_hash = self._broker_card_route_snapshot_hash(event)
        operator_hash = self._broker_card_operator_hash(event)
        record = (
            await self._broker_action_record(str(action_id))
            if self._is_broker_action_id(action_id)
            else None
        )
        contract_hash = (
            str(record.get("contract_hash"))
            if isinstance(record, dict) and self._is_sha256_ref_text(record.get("contract_hash"))
            else self._sha256_ref_text("feishu_broker_contract\x1fmissing")
        )
        if self._broker_card_callback_entrypoint_material_invalid(action_value):
            callback_hash = self._sha256_ref_text(
                json.dumps(
                    self._sanitize_broker_callback_for_hash(action_value),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_callback_material_invalid",
                callback_hash=callback_hash,
                action_id=action_id,
                payload_hash=action_value.get("payload_hash"),
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
            return
        await self.resolve_brokered_card_callback(
            action_value=action_value,
            route_partition_hash=route_partition_hash,
            route_snapshot_hash=route_snapshot_hash,
            operator_hash=operator_hash,
            contract_hash=contract_hash,
            on_resolved=self._handle_brokered_card_callback_resolution,
        )

    @classmethod
    def _broker_card_callback_entrypoint_material_invalid(cls, value: Dict[str, Any]) -> bool:
        allowed_keys = {"action_id", "payload_hash", "choice_hash"}
        for key, item in value.items():
            if str(key) not in allowed_keys:
                return True
            if key != "action_id" and not cls._is_sha256_ref_text(item):
                return True
        return False

    async def _handle_brokered_card_callback_resolution(self, record: Dict[str, Any]) -> None:
        """Hook for brokered card callback business resolution under broker context."""
        return None

    @classmethod
    def _broker_card_operator_hash(cls, event: Any) -> str:
        operator = getattr(event, "operator", None)
        for field in ("open_id", "user_id", "union_id"):
            value = str(getattr(operator, field, "") or "").strip()
            if value:
                return cls._sha256_ref_text(f"feishu_broker_operator\x1f{field}\x1f{value}")
        return cls._sha256_ref_text("feishu_broker_operator\x1fmissing")

    @classmethod
    def _broker_card_route_partition_hash(cls, event: Any) -> str:
        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "").strip()
        if chat_id:
            return cls._sha256_ref_text(
                f"feishu_broker_route_partition\x1fopen_chat_id\x1f{chat_id}"
            )
        return cls._sha256_ref_text("feishu_broker_route_partition\x1fmissing")

    @classmethod
    def _broker_card_route_snapshot_hash(cls, event: Any) -> str:
        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "").strip()
        thread_id = (
            getattr(context, "thread_id", None)
            or getattr(context, "root_id", None)
            or getattr(context, "parent_id", None)
            or getattr(context, "upper_message_id", None)
            or ""
        )
        if chat_id:
            return cls._sha256_ref_text(
                "feishu_broker_route_snapshot\x1fopen_chat_id\x1f"
                f"{chat_id}\x1fthread_id\x1f{str(thread_id or '').strip()}"
            )
        return cls._sha256_ref_text("feishu_broker_route_snapshot\x1fmissing")

    def create_brokered_clarification_card(self, **kwargs: Any) -> Dict[str, Any]:
        return self._create_brokered_current_card("clarification", **kwargs)

    def create_brokered_confirmation_card(self, **kwargs: Any) -> Dict[str, Any]:
        return self._create_brokered_current_card("confirmation", **kwargs)

    def _create_brokered_current_card(
        self,
        action_kind: str,
        *,
        route_partition_hash: str,
        route_snapshot_hash: str,
        operator_hash: str,
        contract_hash: str,
        payload: Dict[str, Any],
        expires_at: int | float,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if self._gateway_event_state_dir is None:
            return {"ok": False, "failure_class": "feishu_broker_action_audit_state_missing"}
        payload_hash = payload.get("payload_hash") if isinstance(payload, dict) else None
        if not (
            action_kind in {"clarification", "confirmation"}
            and self._is_sha256_ref_text(route_partition_hash)
            and self._is_sha256_ref_text(route_snapshot_hash)
            and self._is_sha256_ref_text(operator_hash)
            and self._is_sha256_ref_text(contract_hash)
            and self._is_sha256_ref_text(payload_hash)
            and isinstance(expires_at, (int, float))
            and not isinstance(expires_at, bool)
        ):
            return {"ok": False, "failure_class": "feishu_broker_action_binding_invalid"}
        action_id = self._new_broker_action_id(action_kind)
        grant_handle = self._new_broker_grant_handle(action_id)
        idempotency_key_hash = self._sha256_ref_text(str(idempotency_key))
        event = {
            "type": "feishu_broker_action_created",
            "timestamp": int(time.time()),
            "action_id": action_id,
            "grant_handle": grant_handle,
            "action_kind": action_kind,
            "route_partition_hash": route_partition_hash,
            "route_snapshot_hash": route_snapshot_hash,
            "operator_hash": operator_hash,
            "contract_hash": contract_hash,
            "payload_hash": payload_hash,
            "expires_at": expires_at,
            "idempotency_key_hash": idempotency_key_hash,
        }
        result = gateway_event_ledger.apply_gateway_event(
            event,
            self._gateway_event_state_dir,
        )
        if not self._gateway_event_apply_succeeded(result):
            return {
                "ok": False,
                "failure_class": getattr(result, "failure_class", None)
                or "feishu_broker_action_create_failed",
            }
        record = (result.action or {}).get("record") if result.action else None
        if not isinstance(record, dict):
            return {
                "ok": False,
                "failure_class": "feishu_broker_action_create_failed",
            }
        action_id = str(record["action_id"])
        grant_handle = str(record["grant_handle"])
        payload_hash = str(record["payload_hash"])
        action_kind = str(record["action_kind"])
        return {
            "ok": True,
            "action_id": action_id,
            "grant_handle": grant_handle,
            "action_kind": action_kind,
            "payload_hash": payload_hash,
            "card": {
                "type": action_kind,
                "value": {
                    "action_id": action_id,
                    "payload_hash": payload_hash,
                },
            },
        }

    async def resolve_brokered_card_callback(
        self,
        *,
        action_value: Any,
        route_partition_hash: str,
        route_snapshot_hash: str,
        operator_hash: str,
        contract_hash: str,
        now: int | float | None = None,
        on_resolved: Any = None,
    ) -> Dict[str, Any]:
        callback_hash = self._sha256_ref_text(
            json.dumps(
                self._sanitize_broker_callback_for_hash(action_value),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if not isinstance(action_value, dict):
            return await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_callback_material_invalid",
                callback_hash=callback_hash,
            )
        action_id = action_value.get("action_id")
        payload_hash = action_value.get("payload_hash")
        if self._broker_callback_contains_raw_or_grant_material(action_value):
            return await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_callback_material_invalid",
                callback_hash=callback_hash,
                action_id=action_id,
                payload_hash=payload_hash,
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
        if not self._is_broker_action_id(action_id):
            return await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_unknown",
                callback_hash=callback_hash,
                payload_hash=payload_hash,
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
        record = await self._broker_action_record(str(action_id))
        if record is None:
            return await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_unknown",
                callback_hash=callback_hash,
                action_id=action_id,
                payload_hash=payload_hash,
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
        failure_class = self._broker_callback_binding_failure(
            record=record,
            route_partition_hash=route_partition_hash,
            route_snapshot_hash=route_snapshot_hash,
            operator_hash=operator_hash,
            contract_hash=contract_hash,
            payload_hash=payload_hash,
            now=now,
        )
        if failure_class is not None:
            return await self._deny_brokered_card_callback(
                failure_class=failure_class,
                callback_hash=callback_hash,
                action_id=action_id,
                action_kind=record.get("action_kind"),
                payload_hash=payload_hash if self._is_sha256_ref_text(payload_hash) else record.get("payload_hash"),
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
        action_event = self._broker_action_resolution_event(
            "feishu_broker_action_accepted",
            record=record,
            callback_hash=callback_hash,
            choice_hash=action_value.get("choice_hash"),
        )
        accepted = await gateway_event_ledger.apply_gateway_event_async(
            action_event,
            self._gateway_event_state_dir,
        )
        if not self._gateway_event_apply_succeeded(accepted):
            return {
                "ok": False,
                "failure_class": getattr(accepted, "failure_class", None)
                or "feishu_broker_action_accept_failed",
            }
        accepted_action = getattr(accepted, "action", None)
        if isinstance(accepted_action, dict) and accepted_action.get("outcome") == "duplicate":
            return await self._record_brokered_card_replay(
                record=record,
                callback_hash=callback_hash,
                choice_hash=action_value.get("choice_hash"),
            )
        try:
            with feishu_broker_context(
                str(record["grant_handle"]),
                action_id=str(record["action_id"]),
                contract_hash=str(record["contract_hash"]),
                route_partition_key=self._broker_context_route_key(
                    str(record["route_partition_hash"])
                ),
            ):
                if on_resolved is not None:
                    maybe_result = on_resolved(dict(record))
                    if asyncio.iscoroutine(maybe_result):
                        await maybe_result
        except Exception:
            await self._deny_brokered_card_callback(
                failure_class="feishu_broker_action_side_effect_failed",
                callback_hash=callback_hash,
                action_id=action_id,
                action_kind=record.get("action_kind"),
                payload_hash=record.get("payload_hash"),
                route_partition_hash=route_partition_hash,
                route_snapshot_hash=route_snapshot_hash,
                operator_hash=operator_hash,
                contract_hash=contract_hash,
            )
            return {"ok": False, "failure_class": "feishu_broker_action_side_effect_failed"}
        resolved_event = self._broker_action_resolution_event(
            "feishu_broker_action_resolved",
            record=record,
            callback_hash=callback_hash,
            choice_hash=action_value.get("choice_hash"),
        )
        resolved = await gateway_event_ledger.apply_gateway_event_async(
            resolved_event,
            self._gateway_event_state_dir,
        )
        if not self._gateway_event_apply_succeeded(resolved):
            return {
                "ok": False,
                "failure_class": getattr(resolved, "failure_class", None)
                or "feishu_broker_action_resolve_failed",
            }
        return {
            "ok": True,
            "action_id": str(record["action_id"]),
            "action_kind": str(record["action_kind"]),
            "payload_hash": str(record["payload_hash"]),
        }

    @classmethod
    def _sanitize_broker_callback_for_hash(cls, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: Dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                normalized = re.sub(r"[^a-z0-9]+", "", key_text.lower())
                if normalized in {
                    "body",
                    "content",
                    "path",
                    "rawbody",
                    "rawpayloadpath",
                    "sdkrequest",
                    "feishubrokergrant",
                }:
                    sanitized[key_text] = cls._sha256_ref_text(f"redacted:{normalized}")
                else:
                    sanitized[key_text] = cls._sanitize_broker_callback_for_hash(item)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_broker_callback_for_hash(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return cls._sha256_ref_text(type(value).__name__)

    @classmethod
    def _broker_callback_contains_raw_or_grant_material(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
                if normalized in {
                    "body",
                    "method",
                    "path",
                    "rawbody",
                    "rawpayloadpath",
                    "sdkrequest",
                    "feishubrokergrant",
                }:
                    return True
                if cls._broker_callback_contains_raw_or_grant_material(item):
                    return True
            return False
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return any(cls._broker_callback_contains_raw_or_grant_material(item) for item in value)
        return False

    async def _broker_action_record(self, action_id: str) -> Optional[Dict[str, Any]]:
        if self._gateway_event_state_dir is None:
            return None
        try:
            return await asyncio.to_thread(
                gateway_event_ledger.feishu_broker_action_record,
                self._gateway_event_state_dir,
                action_id,
            )
        except Exception:
            return None

    def _broker_callback_binding_failure(
        self,
        *,
        record: Dict[str, Any],
        route_partition_hash: str,
        route_snapshot_hash: str,
        operator_hash: str,
        contract_hash: str,
        payload_hash: Any,
        now: int | float | None,
    ) -> Optional[str]:
        if route_partition_hash != record.get("route_partition_hash"):
            return "feishu_broker_action_route_mismatch"
        if route_snapshot_hash != record.get("route_snapshot_hash"):
            return "feishu_broker_action_route_mismatch"
        if operator_hash != record.get("operator_hash"):
            return "feishu_broker_action_operator_mismatch"
        if contract_hash != record.get("contract_hash"):
            return "feishu_broker_action_contract_mismatch"
        if payload_hash != record.get("payload_hash"):
            return "feishu_broker_action_payload_mismatch"
        try:
            observed_now = float(now if now is not None else time.time())
            expires_at = float(record.get("expires_at"))
        except (TypeError, ValueError):
            return "feishu_broker_action_expiry_invalid"
        if observed_now > expires_at:
            return "feishu_broker_action_expired"
        return None

    def _broker_action_resolution_event(
        self,
        event_type: str,
        *,
        record: Dict[str, Any],
        callback_hash: str,
        choice_hash: Any,
    ) -> Dict[str, Any]:
        event = {
            "type": event_type,
            "timestamp": int(time.time()),
            "action_id": str(record["action_id"]),
            "action_kind": str(record["action_kind"]),
            "route_partition_hash": str(record["route_partition_hash"]),
            "route_snapshot_hash": str(record["route_snapshot_hash"]),
            "operator_hash": str(record["operator_hash"]),
            "contract_hash": str(record["contract_hash"]),
            "payload_hash": str(record["payload_hash"]),
            "callback_hash": callback_hash,
        }
        if event_type in {"feishu_broker_action_resolved", "feishu_broker_action_replayed"}:
            event["choice_hash"] = (
                str(choice_hash)
                if self._is_sha256_ref_text(choice_hash)
                else self._sha256_ref_text("choice:missing")
            )
        return event

    async def _record_brokered_card_replay(
        self,
        *,
        record: Dict[str, Any],
        callback_hash: str,
        choice_hash: Any,
    ) -> Dict[str, Any]:
        replay_event = self._broker_action_resolution_event(
            "feishu_broker_action_replayed",
            record=record,
            callback_hash=callback_hash,
            choice_hash=choice_hash,
        )
        replay_event["failure_class"] = "feishu_broker_action_duplicate"
        replayed = await gateway_event_ledger.apply_gateway_event_async(
            replay_event,
            self._gateway_event_state_dir,
        )
        if not self._gateway_event_apply_succeeded(replayed):
            return {
                "ok": False,
                "failure_class": getattr(replayed, "failure_class", None)
                or "feishu_broker_action_replay_audit_failed",
            }
        return {
            "ok": False,
            "failure_class": "feishu_broker_action_duplicate",
            "replayed": True,
        }

    async def _deny_brokered_card_callback(
        self,
        *,
        failure_class: str,
        callback_hash: str,
        action_id: Any = None,
        action_kind: Any = None,
        payload_hash: Any = None,
        route_partition_hash: Any = None,
        route_snapshot_hash: Any = None,
        operator_hash: Any = None,
        contract_hash: Any = None,
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "type": "feishu_broker_action_denied",
            "timestamp": int(time.time()),
            "callback_hash": callback_hash,
            "failure_class": failure_class,
        }
        if self._is_broker_action_id(action_id):
            event["action_id"] = str(action_id)
        if action_kind in {"clarification", "confirmation"}:
            event["action_kind"] = str(action_kind)
        for field, value in (
            ("payload_hash", payload_hash),
            ("route_partition_hash", route_partition_hash),
            ("route_snapshot_hash", route_snapshot_hash),
            ("operator_hash", operator_hash),
            ("contract_hash", contract_hash),
        ):
            if self._is_sha256_ref_text(value):
                event[field] = str(value)
        if self._gateway_event_state_dir is None:
            return {
                "ok": False,
                "failure_class": "feishu_broker_action_denial_audit_failed",
            }
        denied = await gateway_event_ledger.apply_gateway_event_async(
            event,
            self._gateway_event_state_dir,
        )
        if not self._gateway_event_apply_succeeded(denied):
            return {
                "ok": False,
                "failure_class": "feishu_broker_action_denial_audit_failed",
            }
        return {"ok": False, "failure_class": failure_class}

    def _build_feishu_legacy_descriptor_denied_event(
        self,
        *,
        surface: str,
        failure_class: str = "feishu_legacy_descriptor_requires_broker",
        correlation_id: Optional[str] = None,
        descriptor_seed: str = "",
    ) -> Dict[str, Any]:
        return {
            "type": "feishu_legacy_descriptor_denied",
            "timestamp": time.time(),
            "correlation_id": self._safe_feishu_audit_correlation_id(correlation_id),
            "descriptor_hash": self._feishu_audit_hash(
                f"{surface}:{failure_class}:{descriptor_seed}"
            ),
            "surface": surface,
            "failure_class": failure_class,
        }

    def _build_feishu_action_denied_event(
        self,
        *,
        action: str,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        failure_class = "feishu_action_requires_broker"
        return {
            "type": "feishu_action_denied",
            "timestamp": time.time(),
            "correlation_id": self._safe_feishu_audit_correlation_id(correlation_id),
            "action_hash": self._feishu_audit_hash(f"{action}:{failure_class}"),
            "action": action,
            "failure_class": failure_class,
        }

    async def _apply_feishu_legacy_descriptor_denied(
        self,
        *,
        surface: str,
        failure_class: str = "feishu_legacy_descriptor_requires_broker",
        correlation_id: Optional[str] = None,
        descriptor_seed: str = "",
    ) -> bool:
        if self._gateway_event_state_dir is None:
            return False
        return bool(
            await self._apply_gateway_event(
                self._build_feishu_legacy_descriptor_denied_event(
                    surface=surface,
                    failure_class=failure_class,
                    correlation_id=correlation_id,
                    descriptor_seed=descriptor_seed,
                )
            )
        )

    def _apply_feishu_audit_event_sync(self, event: Dict[str, Any]) -> bool:
        if self._gateway_event_state_dir is None:
            return False
        try:
            result = gateway_event_ledger.apply_gateway_event(
                event,
                self._gateway_event_state_dir,
            )
        except Exception as exc:
            logger.warning(
                "[Feishu] sync audit apply failed: event_type=%s failure_class=%s",
                event.get("type") or "unknown",
                exc.__class__.__name__,
            )
            return False
        return self._gateway_event_apply_succeeded(result)

    def _apply_feishu_action_denied_sync(
        self,
        *,
        action: str,
        correlation_id: Optional[str] = None,
    ) -> bool:
        return self._apply_feishu_audit_event_sync(
            self._build_feishu_action_denied_event(
                action=action,
                correlation_id=correlation_id,
            )
        )

    def _apply_feishu_legacy_descriptor_denied_sync(
        self,
        *,
        surface: str,
        failure_class: str = "feishu_legacy_descriptor_requires_broker",
        correlation_id: Optional[str] = None,
        descriptor_seed: str = "",
    ) -> bool:
        return self._apply_feishu_audit_event_sync(
            self._build_feishu_legacy_descriptor_denied_event(
                surface=surface,
                failure_class=failure_class,
                correlation_id=correlation_id,
                descriptor_seed=descriptor_seed,
            )
        )

    def _feishu_legacy_descriptor_entry_gate_sync(
        self,
        *,
        surface: str,
        failure_class: str = "feishu_legacy_descriptor_requires_broker",
        correlation_id: Optional[str] = None,
        descriptor_seed: str = "",
        drop_label: str,
    ) -> bool:
        if self._gateway_event_state_dir is None:
            logger.warning(
                "[Feishu] Dropping %s before submit: "
                "reason=feishu_legacy_descriptor_audit_state_missing surface=%s failure_class=%s",
                drop_label,
                surface,
                failure_class,
            )
            return False
        if self._feishu_broker_context_present():
            return True
        audited = self._apply_feishu_legacy_descriptor_denied_sync(
            surface=surface,
            failure_class=failure_class,
            correlation_id=correlation_id,
            descriptor_seed=descriptor_seed,
        )
        if not audited:
            logger.warning(
                "[Feishu] Dropping %s before submit after audit failure: "
                "reason=feishu_legacy_descriptor_denied_apply_failed surface=%s failure_class=%s",
                drop_label,
                surface,
                failure_class,
            )
        return False

    def _handle_approval_card_action(self, *, event: Any, action_value: Dict[str, Any], loop: Any) -> Any:
        """Schedule approval resolution and build the synchronous callback response."""
        approval_id = action_value.get("approval_id")
        if approval_id is None:
            logger.debug("[Feishu] Card action missing approval_id, ignoring")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        state = self._approval_state.get(approval_id)
        if not state:
            logger.debug("[Feishu] Approval %s already resolved or unknown", approval_id)
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        if self._gateway_event_state_dir is None:
            logger.warning(
                "[Feishu] Dropping approval callback before submit: "
                "reason=feishu_action_audit_state_missing action=approval_prompt_card_action"
            )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        choice = _APPROVAL_CHOICE_MAP.get(action_value.get("hermes_action"), "deny")
        if not self._feishu_broker_context_present():
            if not self._apply_feishu_action_denied_sync(
                action="approval_prompt_card_action",
                correlation_id=state.get("correlation_id"),
            ):
                logger.warning(
                    "[Feishu] Dropping approval callback after audit failure: "
                    "reason=feishu_action_denied_apply_failed"
                )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        sender_id = SimpleNamespace(open_id=open_id, user_id=str(getattr(operator, "user_id", "") or ""))
        if not self._allow_group_message(sender_id, state.get("chat_id", ""), is_bot=False):
            logger.warning("[Feishu] Unauthorized approval click by %s", open_id or "<unknown>")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        callback_chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "")
        expected_chat_id = str(state.get("chat_id", "") or "")
        if callback_chat_id and expected_chat_id and callback_chat_id != expected_chat_id:
            logger.warning(
                "[Feishu] Approval callback chat mismatch for %s (expected=%s, got=%s)",
                approval_id,
                expected_chat_id,
                callback_chat_id,
            )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        user_name = self._get_cached_sender_name(open_id) or open_id

        chat_context = getattr(event, "context", None)
        chat_id = str(getattr(chat_context, "open_chat_id", "") or "")
        if not self._submit_on_loop(
            loop,
            self._resolve_approval(
                approval_id=approval_id,
                choice=choice,
                user_name=user_name,
                open_id=open_id,
                chat_id=chat_id,
            ),
        ):
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        if P2CardActionTriggerResponse is None:
            return None
        response = P2CardActionTriggerResponse()
        if self._gateway_event_state_dir is not None:
            return response
        if CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = self._build_resolved_approval_card(choice=choice, user_name=user_name)
            response.card = card
        return response

    def _handle_update_prompt_card_action(self, *, event: Any, action_value: Dict[str, Any], loop: Any) -> Any:
        """Schedule update prompt resolution and build the synchronous callback response."""
        prompt_id = action_value.get("update_prompt_id")
        if prompt_id is None:
            logger.debug("[Feishu] Card action missing update_prompt_id, ignoring")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        if prompt_id not in self._update_prompt_state:
            logger.debug("[Feishu] Update prompt %s already resolved or unknown", prompt_id)
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        state = self._update_prompt_state[prompt_id]
        if self._gateway_event_state_dir is None:
            logger.warning(
                "[Feishu] Dropping update prompt callback before submit: "
                "reason=feishu_action_audit_state_missing action=update_prompt_card_action"
            )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        if not self._feishu_broker_context_present():
            if not self._apply_feishu_action_denied_sync(
                action="update_prompt_card_action",
                correlation_id=state.get("correlation_id"),
            ):
                logger.warning(
                    "[Feishu] Dropping update prompt callback after audit failure: "
                    "reason=feishu_action_denied_apply_failed"
                )
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        answer = str(action_value.get("hermes_update_prompt_action", "") or "").strip().lower()
        if answer not in {"y", "n"}:
            logger.debug("[Feishu] Card action has invalid update prompt answer=%r", answer)
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not self._is_interactive_operator_authorized(open_id):
            logger.warning("[Feishu] Unauthorized update prompt click by %s", open_id or "<unknown>")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        user_name = self._get_cached_sender_name(open_id) or open_id
        if not self._submit_on_loop(loop, self._resolve_update_prompt(prompt_id, answer, user_name)):
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        if P2CardActionTriggerResponse is None:
            return None
        response = P2CardActionTriggerResponse()
        if self._gateway_event_state_dir is not None:
            return response
        if CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = self._build_resolved_update_prompt_card(answer=answer, user_name=user_name)
            response.card = card
        return response

    @staticmethod
    def _claim_audited_prompt_resolution(
        state: Dict[str, Any],
        *,
        choice: str,
        user_name: str,
    ) -> Optional[Dict[str, Any]]:
        if state.get("resolution_claim") is not None:
            return None
        claim = {
            "choice": str(choice),
            "user_name": str(user_name),
            "timestamp": int(time.time()),
        }
        state["resolution_claim"] = claim
        return claim

    @staticmethod
    def _release_audited_prompt_resolution_claim(
        state: Dict[str, Any],
        claim: Dict[str, Any],
    ) -> None:
        if state.get("resolution_claim") == claim:
            state.pop("resolution_claim", None)

    @staticmethod
    def _next_prompt_resolution_attempt(state: Dict[str, Any]) -> int:
        try:
            current = int(state.get("resolution_attempt", 0))
        except (TypeError, ValueError):
            current = 0
        attempt = current + 1
        state["resolution_attempt"] = attempt
        return attempt

    @staticmethod
    def _prompt_card_from_state(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        card = state.get("prompt_card")
        return copy.deepcopy(card) if isinstance(card, dict) else None

    async def _audited_prompt_card_update(
        self,
        *,
        operation: str,
        state: Dict[str, Any],
        prompt_id: Any,
        choice: str,
        card: Dict[str, Any],
    ) -> bool:
        """Patch a prompt card through the delivery ledger."""
        if not self._client:
            logger.warning("[Feishu] Cannot audit %s: client not connected", operation)
            return False

        message_id = str(state.get("message_id") or "").strip()
        if not self._valid_feishu_message_id(message_id):
            logger.warning(
                "[Feishu] Cannot audit %s: invalid or missing message_id for prompt %s",
                operation,
                prompt_id,
            )
            return False

        session_key = str(state.get("session_key") or "").strip()
        payload = json.dumps(card, ensure_ascii=False)
        body = self._build_update_message_body(msg_type="interactive", content=payload)
        request = self._build_update_message_request(message_id=message_id, request_body=body)
        delivery_id = self._delivery_id_for(
            operation,
            metadata=None,
            parts=[message_id, session_key, str(prompt_id), choice],
        )
        response_or_result = await self._audited_delivery(
            delivery_id=delivery_id,
            operation=operation,
            target=f"feishu:message:{message_id}",
            inbound_id=self._delivery_metadata(state, "inbound_id", message_id),
            session_id=self._delivery_metadata(state, "session_id", session_key or "session"),
            correlation_id=self._delivery_metadata(state, "correlation_id", delivery_id),
            network_call=lambda _uuid_value: asyncio.to_thread(
                self._client.im.v1.message.update,
                request,
            ),
            require_returned_message_id=False,
            existing_message_id=message_id,
        )
        if isinstance(response_or_result, SendResult):
            if response_or_result.success:
                return True
            logger.warning(
                "[Feishu] Audited %s failed for prompt card update: %s",
                operation,
                response_or_result.error or "unknown",
            )
            return False
        return True

    async def _restore_audited_prompt_card_after_side_effect_failure(
        self,
        *,
        operation: str,
        state: Dict[str, Any],
        prompt_id: Any,
        choice: str,
        attempt: int,
    ) -> None:
        card = self._prompt_card_from_state(state)
        if card is None:
            logger.warning(
                "[Feishu] Cannot restore prompt %s after side-effect failure: missing prompt_card",
                prompt_id,
            )
            return
        restored = await self._audited_prompt_card_update(
            operation=operation,
            state=state,
            prompt_id=prompt_id,
            choice=f"{choice}:attempt:{attempt}:retry",
            card=card,
        )
        if not restored:
            logger.warning(
                "[Feishu] Failed to restore actionable prompt card for prompt %s",
                prompt_id,
            )

    async def _resolve_approval(
        self,
        approval_id: Any,
        choice: str,
        user_name: str,
        *,
        open_id: str = "",
        chat_id: str = "",
    ) -> None:
        """Pop approval state and unblock the waiting agent thread."""
        state = self._approval_state.get(approval_id)
        if not state:
            logger.debug("[Feishu] Approval %s already resolved or unknown", approval_id)
            return
        if not self._is_interactive_operator_authorized(open_id):
            logger.warning("[Feishu] Unauthorized approval click by %s for approval %s", open_id or "<unknown>", approval_id)
            return
        expected_chat_id = str(state.get("chat_id", "") or "")
        if expected_chat_id and chat_id and expected_chat_id != chat_id:
            logger.warning(
                "[Feishu] Approval %s chat mismatch (expected=%s, got=%s)",
                approval_id, expected_chat_id, chat_id,
            )
            return
        if self._gateway_event_state_dir is not None:
            claim = self._claim_audited_prompt_resolution(
                state,
                choice=choice,
                user_name=user_name,
            )
            if claim is None:
                logger.debug("[Feishu] Approval %s resolution already in progress", approval_id)
                return
            attempt = self._next_prompt_resolution_attempt(state)
            card_updated = await self._audited_prompt_card_update(
                operation="approval_prompt_card_update",
                state=state,
                prompt_id=approval_id,
                choice=f"{choice}:attempt:{attempt}",
                card=self._build_resolved_approval_card(choice=choice, user_name=user_name),
            )
            if not card_updated:
                self._release_audited_prompt_resolution_claim(state, claim)
                return
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(state["session_key"], choice)
            logger.info(
                "Feishu button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                count, state["session_key"], choice, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve gateway approval from Feishu button: %s", exc)
            if self._gateway_event_state_dir is not None:
                await self._restore_audited_prompt_card_after_side_effect_failure(
                    operation="approval_prompt_card_update",
                    state=state,
                    prompt_id=approval_id,
                    choice=choice,
                    attempt=attempt,
                )
                self._release_audited_prompt_resolution_claim(state, claim)
            return
        self._approval_state.pop(approval_id, None)

    async def _resolve_update_prompt(self, prompt_id: Any, answer: str, user_name: str) -> None:
        """Persist an update prompt answer for the detached update process."""
        state = self._update_prompt_state.get(prompt_id)
        if not state:
            logger.debug("[Feishu] Update prompt %s already resolved or unknown", prompt_id)
            return
        if self._gateway_event_state_dir is not None:
            claim = self._claim_audited_prompt_resolution(
                state,
                choice=answer,
                user_name=user_name,
            )
            if claim is None:
                logger.debug("[Feishu] Update prompt %s resolution already in progress", prompt_id)
                return
            attempt = self._next_prompt_resolution_attempt(state)
            card_updated = await self._audited_prompt_card_update(
                operation="update_prompt_card_update",
                state=state,
                prompt_id=prompt_id,
                choice=f"{answer}:attempt:{attempt}",
                card=self._build_resolved_update_prompt_card(answer=answer, user_name=user_name),
            )
            if not card_updated:
                self._release_audited_prompt_resolution_claim(state, claim)
                return
        try:
            self._write_update_prompt_response(answer)
            logger.info(
                "Feishu update prompt resolved for session %s (answer=%s, user=%s)",
                state["session_key"], answer, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve Feishu update prompt: %s", exc)
            if self._gateway_event_state_dir is not None:
                await self._restore_audited_prompt_card_after_side_effect_failure(
                    operation="update_prompt_card_update",
                    state=state,
                    prompt_id=prompt_id,
                    choice=answer,
                    attempt=attempt,
                )
                self._release_audited_prompt_resolution_claim(state, claim)
            return
        self._update_prompt_state.pop(prompt_id, None)

    async def _handle_reaction_event(self, event_type: str, data: Any) -> None:
        """Fetch the reacted-to message; if it was sent by this bot, emit a synthetic text event."""
        if not self._client:
            return
        event = getattr(data, "event", None)
        reaction_event_id = self._feishu_event_id(data)
        message_id = str(getattr(event, "message_id", "") or "")
        if not reaction_event_id or not message_id:
            logger.warning(
                "[Feishu] Dropping malformed reaction event: "
                "reason=feishu_reaction_missing_stable_id event_id_present=%s target_message_id_present=%s",
                bool(reaction_event_id),
                bool(message_id),
            )
            return
        if not self._feishu_broker_context_present():
            audited = await self._apply_feishu_legacy_descriptor_denied(
                surface="feishu.reaction",
                correlation_id=reaction_event_id,
                descriptor_seed=reaction_event_id,
            )
            if not audited:
                logger.warning(
                    "[Feishu] Dropping reaction after audit failure: "
                    "reason=feishu_legacy_descriptor_denied_apply_failed"
                )
            return

        # Fetch the target message to verify it was sent by us and to obtain chat context.
        try:
            request = self._build_get_message_request(message_id)
            response = await asyncio.to_thread(self._client.im.v1.message.get, request)
            if not response or not getattr(response, "success", lambda: False)():
                return
            items = getattr(getattr(response, "data", None), "items", None) or []
            msg = items[0] if items else None
            if not msg:
                return
            # GET im/v1/messages returns sender.id=app_id for bot messages —
            # peer bots and us share sender_type="app" but differ on app_id.
            sender = getattr(msg, "sender", None)
            if str(getattr(sender, "id", "") or "") != self._app_id:
                return  # only route reactions on this bot's own messages
            chat_id = str(getattr(msg, "chat_id", "") or "")
            chat_type_raw = str(getattr(msg, "chat_type", "p2p") or "p2p")
            if not chat_id:
                return
            thread_id = getattr(msg, "thread_id", None) or None
            reply_to_message_id = (
                getattr(msg, "parent_id", None)
                or getattr(msg, "upper_message_id", None)
                or getattr(msg, "root_id", None)
                or None
            )
        except Exception:
            logger.debug("[Feishu] Failed to fetch message for reaction routing", exc_info=True)
            return

        user_id_obj = getattr(event, "user_id", None)
        reaction_type_obj = getattr(event, "reaction_type", None)
        emoji_type = str(getattr(reaction_type_obj, "emoji_type", "") or "UNKNOWN")
        action = "added" if "created" in event_type else "removed"
        synthetic_text = f"reaction:{action}:{emoji_type}"

        sender_profile = await self._resolve_sender_profile(user_id_obj)
        chat_info = await self.get_chat_info(chat_id)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=self._resolve_source_chat_type(
                chat_info=chat_info,
                event_chat_type=chat_type_raw,
                prefer_event_chat_type=True,
            ),
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=thread_id,
            user_id_alt=sender_profile["user_id_alt"],
        )
        synthetic_event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=reaction_event_id,
            reply_to_message_id=reply_to_message_id,
            timestamp=datetime.now(),
        )
        logger.info("[Feishu] Routing reaction %s:%s on bot message %s as synthetic event", action, emoji_type, message_id)
        await self._handle_message_with_guards(synthetic_event)

    def _is_card_action_duplicate(self, token: str) -> bool:
        """Return True if this card action token was already processed within the dedup window."""
        now = time.time()
        # Prune expired tokens lazily each call.
        expired = [t for t, ts in self._card_action_tokens.items() if now - ts > _FEISHU_CARD_ACTION_DEDUP_TTL_SECONDS]
        for t in expired:
            del self._card_action_tokens[t]
        if token in self._card_action_tokens:
            return True
        self._card_action_tokens[token] = now
        return False

    async def _handle_card_action_event(self, data: Any) -> None:
        """Route Feishu interactive card button clicks as synthetic COMMAND events."""
        event = getattr(data, "event", None)
        token = str(getattr(event, "token", "") or "")
        if not token:
            logger.warning(
                "[Feishu] Dropping card action without stable token: "
                "reason=feishu_card_action_missing_token"
            )
            return
        if not self._feishu_broker_context_present():
            audited = await self._apply_feishu_legacy_descriptor_denied(
                surface="feishu.card_action",
                failure_class="feishu_legacy_card_action_requires_broker",
                correlation_id=token,
                descriptor_seed=token,
            )
            if not audited:
                logger.warning(
                    "[Feishu] Dropping card action after audit failure: "
                    "reason=feishu_legacy_descriptor_denied_apply_failed"
                )
            return
        if self._is_card_action_duplicate(token):
            logger.debug("[Feishu] Dropping duplicate card action token: %s", token)
            return

        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        thread_id = getattr(context, "thread_id", None) or None
        reply_to_message_id = (
            getattr(context, "parent_id", None)
            or getattr(context, "upper_message_id", None)
            or getattr(context, "root_id", None)
            or None
        )
        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not chat_id or not open_id:
            logger.debug("[Feishu] Card action missing chat_id or operator open_id, dropping")
            return

        action = getattr(event, "action", None)
        action_tag = str(getattr(action, "tag", "") or "button")
        action_value = getattr(action, "value", {}) or {}

        synthetic_text = f"/card {action_tag}"
        if action_value:
            try:
                synthetic_text += f" {json.dumps(action_value, ensure_ascii=False)}"
            except Exception:
                pass

        sender_id = SimpleNamespace(open_id=open_id, user_id=None, union_id=None)
        sender_profile = await self._resolve_sender_profile(sender_id)
        chat_info = await self.get_chat_info(chat_id)
        resolved_chat_type = self._resolve_card_action_chat_type(
            chat_info=chat_info,
            action_value=action_value,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        if not resolved_chat_type:
            logger.warning(
                "[Feishu] Dropping ambiguous card action without reliable chat scope: "
                "reason=feishu_card_scope_ambiguous chat_id=%s token_present=%s",
                chat_id,
                bool(token),
            )
            return
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=resolved_chat_type,
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=thread_id,
            user_id_alt=sender_profile["user_id_alt"],
        )
        synthetic_event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.COMMAND,
            source=source,
            raw_message=data,
            message_id=token,
            reply_to_message_id=reply_to_message_id,
            timestamp=datetime.now(),
        )
        logger.info("[Feishu] Routing card action %r from %s in %s as synthetic command", action_tag, open_id, chat_id)
        await self._handle_message_with_guards(synthetic_event)

    # =========================================================================
    # Per-chat serialization and typing indicator
    # =========================================================================

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-chat asyncio.Lock for serial message processing.

        The cache is LRU-bounded so long-running gateways that see many chats
        do not retain one lock per historical chat forever. Held locks are
        skipped during eviction. If all cached locks are held, the cache may
        temporarily exceed the cap instead of breaking per-chat serialization.
        """
        lock = self._chat_locks.get(chat_id)
        if lock is not None:
            self._chat_locks.move_to_end(chat_id)
            return lock
        if len(self._chat_locks) >= self.CHAT_LOCK_MAX_SIZE:
            for key in list(self._chat_locks):
                if not self._chat_locks[key].locked():
                    self._chat_locks.pop(key)
                    break
        lock = asyncio.Lock()
        self._chat_locks[chat_id] = lock
        return lock

    async def _handle_message_with_guards(
        self,
        event: MessageEvent,
        *,
        current_conversation_contract: ConversationContract | None = None,
        transport_kind: str | None = None,
        mention_required: bool | None = None,
    ) -> None:
        """Dispatch a single event through the agent pipeline with per-chat serialization
        before handing the event off to the agent.

        Per-chat lock ensures messages in the same chat are processed one at a
        time (matches openclaw's createChatQueue serial queue behaviour).
        """
        if not self._admit_current_conversation_for_event(
            event,
            current_conversation_contract=current_conversation_contract,
            transport_kind=transport_kind,
            mention_required=mention_required,
        ):
            return

        chat_id = getattr(event.source, "chat_id", "") or "" if event.source else ""
        chat_lock = self._get_chat_lock(chat_id)
        async with chat_lock:
            admission_record = getattr(
                event,
                "feishu_current_conversation_admission",
                None,
            )
            token = None
            if isinstance(admission_record, dict):
                token = _FEISHU_CURRENT_ADMISSION_CONTEXT.set(dict(admission_record))
            try:
                if not getattr(event, "feishu_inbound_gateway_event_preapplied", False):
                    if not await self._apply_inbound_gateway_event(event):
                        return
                await self.handle_message(event)
            except InvalidLiveSessionSource as exc:
                logger.warning("[Feishu] Ignoring invalid live inbound source: reason=%s", exc.reason)
            finally:
                if token is not None:
                    _FEISHU_CURRENT_ADMISSION_CONTEXT.reset(token)

    def _admit_current_conversation_for_event(
        self,
        event: MessageEvent,
        *,
        current_conversation_contract: ConversationContract | None = None,
        transport_kind: str | None = None,
        mention_required: bool | None = None,
    ) -> bool:
        if current_conversation_contract is None:
            current_conversation_contract = getattr(
                event,
                "feishu_current_conversation_contract",
                None,
            )
        if transport_kind is None:
            transport_kind = getattr(
                event,
                "feishu_current_transport_kind",
                None,
            )
        if mention_required is None:
            mention_required = getattr(
                event,
                "feishu_current_mention_required",
                False,
            )
        if current_conversation_contract is None:
            return True
        admission = self._admit_current_conversation_event(
            event,
            transport_kind=transport_kind or self._transport_kind_for_event(event),
            expected_contract=current_conversation_contract,
            mention_required=bool(mention_required),
        )
        if not admission.ok:
            setattr(
                event,
                "feishu_current_conversation_denial",
                self._sanitized_current_conversation_denial(admission),
            )
            logger.warning(
                "[Feishu] Dropping current-conversation inbound before dispatch: failure_class=%s",
                admission.failure_class,
            )
            return False
        setattr(event, "feishu_current_conversation_admission", admission.record)
        return True

    def _admit_current_conversation_event(
        self,
        event: MessageEvent,
        *,
        transport_kind: str,
        expected_contract: ConversationContract | None = None,
        mention_required: bool = False,
    ) -> FeishuCurrentConversationAdmissionResult:
        transport = str(transport_kind or "").strip().lower()
        if transport not in {"dm", "group", "thread", "webhook", "websocket"}:
            return self._current_conversation_denied(
                "feishu_current_transport_unsupported",
                transport_kind=transport or "unknown",
            )
        source = getattr(event, "source", None)
        if source is None or getattr(source, "platform", None) != Platform.FEISHU:
            return self._current_conversation_denied(
                "feishu_current_route_evidence_missing",
                transport_kind=transport,
            )
        if expected_contract is not None and not isinstance(
            expected_contract, ConversationContract
        ):
            return self._current_conversation_denied(
                "feishu_current_contract_missing",
                transport_kind=transport,
            )
        if expected_contract is not None and expected_contract.evidence_state != "current":
            return self._current_conversation_denied(
                "feishu_current_route_evidence_stale",
                contract_hash=getattr(expected_contract, "contract_hash", None),
                transport_kind=transport,
            )
        if (
            expected_contract is not None
            and expected_contract.scope_assignment_status != "scoped"
        ):
            return self._current_conversation_denied(
                "feishu_current_scope_not_authorizable",
                contract_hash=getattr(expected_contract, "contract_hash", None),
                transport_kind=transport,
                scope_assignment_status=getattr(
                    expected_contract,
                    "scope_assignment_status",
                    None,
                ),
            )
        if mention_required and not self._event_has_mention_evidence(event):
            return self._current_conversation_denied(
                "feishu_current_route_evidence_missing",
                transport_kind=transport,
            )
        if transport == "thread":
            thread_failure = self._thread_anchor_failure(event, expected_contract)
            if thread_failure is not None:
                return self._current_conversation_denied(
                    thread_failure,
                    contract_hash=getattr(expected_contract, "contract_hash", None),
                    transport_kind=transport,
                )

        reply_anchor_ref = self._reply_anchor_ref(event)
        if reply_anchor_ref is None:
            return self._current_conversation_denied(
                "feishu_current_reply_anchor_missing",
                contract_hash=getattr(expected_contract, "contract_hash", None),
                transport_kind=transport,
            )

        observed_contract = self._build_current_conversation_contract(
            event,
            reply_anchor_ref=reply_anchor_ref,
            expected_contract=expected_contract,
        )
        if observed_contract is None:
            return self._current_conversation_denied(
                "feishu_current_route_evidence_missing",
                transport_kind=transport,
            )
        if observed_contract.evidence_state != "current":
            return self._current_conversation_denied(
                "feishu_current_route_evidence_stale",
                contract_hash=observed_contract.contract_hash,
                transport_kind=transport,
            )
        if observed_contract.scope_assignment_status != "scoped":
            return self._current_conversation_denied(
                "feishu_current_scope_not_authorizable",
                contract_hash=observed_contract.contract_hash,
                transport_kind=transport,
                scope_assignment_status=observed_contract.scope_assignment_status,
            )
        if expected_contract is not None:
            mismatch = self._current_contract_mismatch(
                observed_contract,
                expected_contract,
            )
            if mismatch is not None:
                return self._current_conversation_denied(
                    mismatch,
                    contract_hash=expected_contract.contract_hash,
                    transport_kind=transport,
                )

        contract = expected_contract or observed_contract
        authority = contract.authority_subject_ref
        thread_anchor = (
            contract.thread_anchor_ref.value_hash
            if contract.thread_anchor_ref is not None
            else None
        )
        canonical_event_ref = self._canonical_event_ref(event)
        if not canonical_event_ref:
            return self._current_conversation_denied(
                "feishu_inbound_idempotency_unknown",
                event_identity_state="missing",
                transport_kind=transport,
            )

        record = {
            "contract_hash": contract.contract_hash,
            "route_partition_key": contract.route_partition_key,
            "route_session_key_snapshot": contract.route_session_key_snapshot,
            "actor_ref": contract.actor_ref.value_hash,
            "authority_subject_ref": authority.value_hash if authority else None,
            "transport_kind": transport,
            "reply_anchor_ref": reply_anchor_ref.value_hash,
            "thread_anchor_ref": thread_anchor,
            "canonical_event_ref": canonical_event_ref,
        }
        if transport == "thread":
            explicit_reply_anchor_ref = self._explicit_thread_reply_anchor_ref(event)
            if explicit_reply_anchor_ref is not None:
                record["explicit_thread_reply_anchor_ref"] = (
                    explicit_reply_anchor_ref.value_hash
                )
        return FeishuCurrentConversationAdmissionResult(ok=True, record=record)

    def _build_current_conversation_contract(
        self,
        event: MessageEvent,
        *,
        reply_anchor_ref: Any | None,
        expected_contract: ConversationContract | None,
    ) -> ConversationContract | None:
        source = getattr(event, "source", None)
        if source is None:
            return None
        isolation = self._effective_session_isolation()
        account_id = self._current_feishu_platform_account_id()
        identity = conversation_identity(
            source,
            platform_account_id=account_id,
            **isolation,
        )
        if identity is None:
            return None
        actor_ref = feishu_hashed_ref(
            "feishu_actor",
            getattr(source, "user_id_alt", None) or getattr(source, "user_id", None),
        )
        authority_ref = feishu_hashed_ref(
            "feishu_authority_subject",
            getattr(source, "user_id_alt", None) or getattr(source, "user_id", None),
        )
        if actor_ref is None or authority_ref is None:
            return None
        thread_anchor_ref = feishu_hashed_ref(
            "feishu_thread",
            getattr(source, "thread_id", None),
        )
        session_id = (
            getattr(expected_contract, "session_id", None)
            if expected_contract is not None
            else None
        ) or "session:current-conversation"
        tenant_partition_key = (
            getattr(expected_contract, "tenant_partition_key", None)
            if expected_contract is not None
            else None
        ) or self._current_feishu_partition_key("tenant_partition_key", "tenant:feishu")
        app_partition_key = (
            getattr(expected_contract, "app_partition_key", None)
            if expected_contract is not None
            else None
        ) or self._current_feishu_partition_key("app_partition_key", "app:feishu")
        return build_feishu_conversation_contract(
            scope_identity=identity,
            route_partition_key=route_partition_key(source, **isolation),
            route_session_key_snapshot=build_session_key(
                source,
                **isolation,
                require_conversation_identity=True,
            ),
            scope_assignment_status="scoped",
            actor_ref=actor_ref,
            authority_subject_ref=authority_ref,
            identity_evidence_set=(actor_ref, authority_ref),
            session_id=session_id,
            tenant_partition_key=tenant_partition_key,
            app_partition_key=app_partition_key,
            thread_anchor_ref=thread_anchor_ref,
            root_anchor_ref=reply_anchor_ref,
            evidence_state="current",
        )

    @staticmethod
    def _current_contract_mismatch(
        observed: ConversationContract,
        expected: ConversationContract,
    ) -> str | None:
        if observed.route_partition_key != expected.route_partition_key:
            return "feishu_current_route_mismatch"
        if (
            observed.route_session_key_snapshot
            != expected.route_session_key_snapshot
        ):
            return "feishu_current_route_snapshot_mismatch"
        if observed.actor_ref != expected.actor_ref:
            return "feishu_current_actor_mismatch"
        if observed.authority_subject_ref != expected.authority_subject_ref:
            return "feishu_current_actor_mismatch"
        if observed.conversation_scope_id != expected.conversation_scope_id:
            return "feishu_current_route_mismatch"
        return None

    def _thread_anchor_failure(
        self,
        event: MessageEvent,
        expected_contract: ConversationContract | None,
    ) -> str | None:
        source = getattr(event, "source", None)
        source_thread = str(getattr(source, "thread_id", "") or "")
        if not source_thread:
            return "feishu_current_thread_detached"
        raw_message = self._raw_feishu_message(event)
        raw_thread = str(getattr(raw_message, "thread_id", "") or "")
        if not raw_thread or raw_thread != source_thread:
            return "feishu_current_thread_detached"
        if self._explicit_thread_reply_anchor_ref(event) is None:
            return "feishu_current_reply_anchor_missing"
        if (
            expected_contract is not None
            and expected_contract.thread_anchor_ref is None
        ):
            return "feishu_current_thread_detached"
        return None

    @staticmethod
    def _event_has_mention_evidence(event: MessageEvent) -> bool:
        raw_message = FeishuAdapter._raw_feishu_message(event)
        mentions = getattr(raw_message, "mentions", None) or []
        if mentions:
            return True
        raw_content = str(getattr(raw_message, "content", "") or "")
        return "@_user_" in raw_content or "@_all" in raw_content

    @staticmethod
    def _reply_anchor_value(event: MessageEvent) -> str | None:
        anchor = _reply_anchor_for_event(event)
        anchor = str(anchor or "").strip()
        return anchor or None

    @staticmethod
    def _reply_anchor_ref(event: MessageEvent) -> Any | None:
        anchor = FeishuAdapter._reply_anchor_value(event)
        return feishu_hashed_ref("feishu_reply_anchor", anchor)

    @staticmethod
    def _explicit_thread_reply_anchor_ref(event: MessageEvent) -> Any | None:
        raw_message = FeishuAdapter._raw_feishu_message(event)
        anchor = (
            getattr(event, "reply_to_message_id", None)
            or getattr(raw_message, "parent_id", None)
            or getattr(raw_message, "upper_message_id", None)
            or getattr(raw_message, "root_id", None)
        )
        anchor = str(anchor or "").strip()
        if not anchor:
            return None
        return feishu_hashed_ref("feishu_reply_anchor", anchor)

    @staticmethod
    def _raw_feishu_message(event: MessageEvent) -> Any:
        raw = getattr(event, "raw_message", None)
        raw_event = getattr(raw, "event", None)
        return getattr(raw_event, "message", None) or SimpleNamespace()

    def _canonical_event_ref(self, event: MessageEvent) -> str:
        event_id = self._feishu_event_id(getattr(event, "raw_message", None))
        if not event_id:
            return ""
        ref = feishu_hashed_ref("feishu_event", event_id)
        return ref.value_hash if ref is not None else ""

    def _transport_kind_for_event(self, event: MessageEvent) -> str:
        source = getattr(event, "source", None)
        if getattr(source, "thread_id", None):
            return "thread"
        chat_type = str(getattr(source, "chat_type", "") or "")
        return "dm" if chat_type == "dm" else "group"

    def _current_feishu_platform_account_id(self) -> str:
        configured = self._current_feishu_partition_key("platform_account_id", "")
        return feishu_platform_account_id(
            platform_account_id=configured or None,
            app_id=getattr(self, "_app_id", None),
            config=getattr(self, "config", None),
        ) or "feishu_app:unknown"

    def _current_feishu_partition_key(self, key: str, default: str) -> str:
        extra = getattr(getattr(self, "config", None), "extra", {}) or {}
        value = extra.get(key)
        return str(value) if value else default

    @staticmethod
    def _current_conversation_denied(
        failure_class: str,
        **evidence: Any,
    ) -> FeishuCurrentConversationAdmissionResult:
        sanitized = {
            key: value
            for key, value in evidence.items()
            if value is None
            or isinstance(value, (bool, int, float))
            or (isinstance(value, str) and not value.startswith(("oc_", "ou_", "om_")))
        }
        return FeishuCurrentConversationAdmissionResult(
            ok=False,
            failure_class=failure_class,
            evidence=sanitized,
        )

    @staticmethod
    def _sanitized_current_conversation_denial(
        admission: FeishuCurrentConversationAdmissionResult,
    ) -> Dict[str, Any]:
        return {
            "type": "feishu_current_conversation_admission_denied",
            "failure_class": admission.failure_class,
            "evidence": dict(admission.evidence),
        }

    @staticmethod
    def _string_field(obj: Any, field: str) -> str:
        if isinstance(obj, dict):
            value = obj.get(field)
        else:
            value = getattr(obj, field, None)
        return str(value).strip() if value is not None else ""

    @classmethod
    def _message_id_list_field(cls, obj: Any, field: str) -> List[str]:
        if isinstance(obj, dict):
            value = obj.get(field)
        else:
            value = getattr(obj, field, None)
        if not isinstance(value, list) or not value:
            return []
        message_ids: List[str] = []
        for item in value:
            message_id = str(item).strip() if item is not None else ""
            if not message_id:
                return []
            message_ids.append(message_id)
        return message_ids

    @classmethod
    def _feishu_event_id(cls, data: Any) -> str:
        header = getattr(data, "header", None)
        if isinstance(data, dict):
            header = data.get("header")
        for owner, field in (
            (header, "event_id"),
            (data, "event_id"),
            (getattr(data, "event", None), "event_id"),
        ):
            value = cls._string_field(owner, field)
            if value:
                return value
        return ""

    async def _apply_inbound_gateway_event(self, event: MessageEvent) -> bool:
        if self._gateway_event_state_dir is None:
            return True
        inbound_id = str(getattr(event, "message_id", "") or "").strip()
        if not inbound_id:
            logger.warning(
                "[Feishu] Dropping inbound apply without reliable id: "
                "reason=feishu_inbound_missing_message_id"
            )
            return False
        timestamp = self._event_timestamp_seconds(getattr(event, "timestamp", None))
        event_payload = {
            "type": "feishu_inbound",
            "inbound_id": inbound_id,
            "message_id": inbound_id,
            "message_type": getattr(getattr(event, "message_type", None), "value", "unknown"),
            "timestamp": timestamp,
        }
        admission = getattr(event, "feishu_current_conversation_admission", None)
        if isinstance(admission, dict):
            event_payload.update(
                {
                    "idempotency_evidence_state": "current_admitted",
                    "canonical_event_ref": str(admission.get("canonical_event_ref") or ""),
                    "route_partition_key": str(admission.get("route_partition_key") or ""),
                    "contract_hash": str(admission.get("contract_hash") or ""),
                    "transport_kind": str(admission.get("transport_kind") or ""),
                }
            )
        elif getattr(
            event,
            "feishu_current_inbound_requires_idempotency_evidence",
            False,
        ):
            event_payload["idempotency_evidence_state"] = "current_required"
        elif not hasattr(event, "feishu_current_conversation_contract"):
            event_payload["idempotency_evidence_state"] = "legacy_unscoped"
        return await self._apply_gateway_event(event_payload)

    @staticmethod
    def _event_timestamp_seconds(timestamp: Any) -> int:
        if isinstance(timestamp, datetime):
            return int(timestamp.timestamp())
        if isinstance(timestamp, (int, float)):
            return int(timestamp)
        return int(time.time())

    async def _apply_gateway_event(self, event_payload: Dict[str, Any]) -> Any:
        if self._gateway_event_state_dir is None:
            return True
        try:
            result = await gateway_event_ledger.apply_gateway_event_async(
                event_payload,
                self._gateway_event_state_dir,
            )
        except Exception as exc:
            logger.warning(
                "[Feishu] gateway-event apply failed: event_type=%s failure_class=%s",
                event_payload.get("type") or "unknown",
                exc.__class__.__name__,
            )
            return False
        if getattr(result, "ok", False):
            action = getattr(result, "action", None)
            if event_payload.get("type") == "delivery_pending":
                if (
                    getattr(result, "event_type", None) == "delivery_pending"
                    and isinstance(action, dict)
                    and action.get("type") == "delivery_record"
                ):
                    return result
                return False
            if event_payload.get("type") == "feishu_inbound":
                if (
                    getattr(result, "event_type", None) == "feishu_inbound"
                    and isinstance(action, dict)
                    and action.get("type") == "inbound_admission"
                ):
                    if (
                        action.get("duplicate") is True
                        or action.get("decision") == "feishu_inbound_duplicate"
                    ):
                        logger.debug(
                            "[Feishu] Dropping duplicate inbound event admitted by ledger"
                        )
                        return False
                    if action.get("decision") != "continue":
                        return False
                    return True
                return False
            return True
        logger.warning(
            "[Feishu] gateway-event apply failed: event_type=%s failure_class=%s reason=%s diagnostics=%s",
            getattr(result, "event_type", None) or event_payload.get("type") or "unknown",
            getattr(result, "failure_class", None) or "gateway_event_apply_failed",
            getattr(result, "reason", None) or "",
            getattr(result, "diagnostics", None) or "",
        )
        return False

    # =========================================================================
    # Processing status reactions
    # =========================================================================

    def _reactions_enabled(self) -> bool:
        return os.getenv("FEISHU_REACTIONS", "true").strip().lower() not in {"false", "0", "no"}

    async def _add_reaction(self, message_id: str, emoji_type: str) -> Optional[str]:
        """Return the reaction_id on success, else None. The id is needed later for deletion."""
        if not self._client or not message_id or not emoji_type:
            return None
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
            )
            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type({"emoji_type": emoji_type})
                .build()
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = await asyncio.to_thread(self._client.im.v1.message_reaction.create, request)
            if response and getattr(response, "success", lambda: False)():
                data = getattr(response, "data", None)
                return getattr(data, "reaction_id", None)
            logger.debug(
                "[Feishu] Add reaction %s on %s rejected: code=%s msg=%s",
                emoji_type,
                message_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
        except Exception:
            logger.warning(
                "[Feishu] Add reaction %s on %s raised",
                emoji_type,
                message_id,
                exc_info=True,
            )
        return None

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        if not self._client or not message_id or not reaction_id:
            return False
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            response = await asyncio.to_thread(self._client.im.v1.message_reaction.delete, request)
            if response and getattr(response, "success", lambda: False)():
                return True
            logger.debug(
                "[Feishu] Remove reaction %s on %s rejected: code=%s msg=%s",
                reaction_id,
                message_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
        except Exception:
            logger.warning(
                "[Feishu] Remove reaction %s on %s raised",
                reaction_id,
                message_id,
                exc_info=True,
            )
        return False

    def _remember_processing_reaction(self, message_id: str, reaction_id: str) -> None:
        cache = self._pending_processing_reactions
        cache[message_id] = reaction_id
        cache.move_to_end(message_id)
        while len(cache) > _FEISHU_PROCESSING_REACTION_CACHE_SIZE:
            cache.popitem(last=False)

    def _pop_processing_reaction(self, message_id: str) -> Optional[str]:
        return self._pending_processing_reactions.pop(message_id, None)

    async def on_processing_start(self, event: MessageEvent) -> None:
        if not self._reactions_enabled():
            return
        message_id = event.message_id
        if not message_id or message_id in self._pending_processing_reactions:
            return
        reaction_id = await self._add_reaction(message_id, _FEISHU_REACTION_IN_PROGRESS)
        if reaction_id:
            self._remember_processing_reaction(message_id, reaction_id)

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        if not self._reactions_enabled():
            return
        message_id = event.message_id
        if not message_id:
            return

        start_reaction_id = self._pending_processing_reactions.get(message_id)
        if start_reaction_id:
            if not await self._remove_reaction(message_id, start_reaction_id):
                # Don't stack a second badge on top of a Typing we couldn't
                # remove — UI would read as both "working" and "done/failed"
                # simultaneously. Keep the handle so LRU eventually evicts it.
                return
            self._pop_processing_reaction(message_id)

        if outcome is ProcessingOutcome.FAILURE:
            await self._add_reaction(message_id, _FEISHU_REACTION_FAILURE)

    # =========================================================================
    # Webhook server and security
    # =========================================================================

    def _record_webhook_anomaly(self, remote_ip: str, status: str) -> None:
        """Increment the anomaly counter for remote_ip and emit a WARNING every threshold hits.

        Mirrors openclaw's createWebhookAnomalyTracker: TTL 6 hours, log every 25 consecutive
        error responses from the same IP.
        """
        now = time.time()
        entry = self._webhook_anomaly_counts.get(remote_ip)
        if entry is not None:
            count, _last_status, first_seen = entry
            if now - first_seen < _FEISHU_WEBHOOK_ANOMALY_TTL_SECONDS:
                count += 1
                if count % _FEISHU_WEBHOOK_ANOMALY_THRESHOLD == 0:
                    logger.warning(
                        "[Feishu] Webhook anomaly: %d consecutive error responses (%s) from %s "
                        "over the last %.0fs",
                        count,
                        status,
                        remote_ip,
                        now - first_seen,
                    )
                self._webhook_anomaly_counts[remote_ip] = (count, status, first_seen)
                return
        # Either first occurrence or TTL expired — start fresh.
        self._webhook_anomaly_counts[remote_ip] = (1, status, now)

    def _clear_webhook_anomaly(self, remote_ip: str) -> None:
        """Reset the anomaly counter for remote_ip after a successful request."""
        self._webhook_anomaly_counts.pop(remote_ip, None)

    # =========================================================================
    # Inbound processing pipeline
    # =========================================================================

    async def _process_inbound_message(
        self,
        *,
        data: Any,
        message: Any,
        sender_id: Any,
        chat_type: str,
        message_id: str,
        is_bot: bool = False,
        transport_kind: str = "websocket",
    ) -> None:
        chat_id = getattr(message, "chat_id", "") or ""
        if not chat_id:
            logger.warning(
                "[Feishu] Ignoring inbound message without chat identity: id=%s chat_type=%s reason=%s",
                message_id,
                chat_type,
                "feishu_missing_chat_identity",
            )
            return

        text, inbound_type, media_urls, media_types, mentions = await self._extract_message_content(message)

        if inbound_type == MessageType.TEXT:
            text = _strip_edge_self_mentions(text, mentions)
            if text.startswith("/"):
                inbound_type = MessageType.COMMAND

        # Guard runs post-strip so a pure "@Bot" message (stripped to "") is dropped.
        if inbound_type == MessageType.TEXT and not text and not media_urls:
            logger.debug("[Feishu] Ignoring empty text message id=%s", message_id)
            return

        if inbound_type != MessageType.COMMAND:
            hint = _build_mention_hint(mentions)
            if hint:
                text = f"{hint}\n\n{text}" if text else hint

        thread_id = getattr(message, "thread_id", None) or None
        reply_to_message_id = (
            getattr(message, "parent_id", None)
            or getattr(message, "upper_message_id", None)
            or getattr(message, "root_id", None)
            or None
        )
        reply_to_text = await self._fetch_message_text(reply_to_message_id) if reply_to_message_id else None

        sender_primary = (
            getattr(sender_id, "open_id", None)
            or getattr(sender_id, "user_id", None)
            or getattr(sender_id, "union_id", None)
            or "<unknown>"
        )
        logger.info(
            "[Feishu] Inbound %s message received: id=%s type=%s chat_id=%s sender=%s:%s text=%r media=%d",
            "dm" if chat_type == "p2p" else "group",
            message_id,
            inbound_type.value,
            getattr(message, "chat_id", "") or "",
            "bot" if is_bot else "user",
            sender_primary,
            text[:120],
            len(media_urls),
        )

        chat_info = await self.get_chat_info(chat_id)
        sender_profile = await self._resolve_sender_profile(sender_id, is_bot=is_bot)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=self._resolve_source_chat_type(
                chat_info=chat_info,
                event_chat_type=chat_type,
                prefer_event_chat_type=True,
            ),
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=thread_id,
            user_id_alt=sender_profile["user_id_alt"],
            is_bot=is_bot,
        )
        normalized = MessageEvent(
            text=text,
            message_type=inbound_type,
            source=source,
            raw_message=data,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            timestamp=datetime.now(),
        )
        expected_contract = self._build_current_conversation_contract(
            normalized,
            reply_anchor_ref=self._reply_anchor_ref(normalized),
            expected_contract=None,
        )
        setattr(
            normalized,
            "feishu_current_conversation_contract",
            expected_contract,
        )
        setattr(
            normalized,
            "feishu_current_transport_kind",
            self._normalize_inbound_transport_kind(transport_kind),
        )
        setattr(
            normalized,
            "feishu_current_mention_required",
            source.chat_type != "dm" and self._require_mention_for(chat_id),
        )
        await self._dispatch_inbound_event(normalized)

    async def _dispatch_inbound_event(self, event: MessageEvent) -> None:
        """Apply Feishu-specific burst protection before entering the base adapter."""
        if not self._admit_current_conversation_for_event(event):
            return
        setattr(event, "feishu_current_inbound_requires_idempotency_evidence", True)
        if not await self._apply_inbound_gateway_event(event):
            return
        setattr(event, "feishu_inbound_gateway_event_preapplied", True)
        if event.message_type == MessageType.TEXT and not event.is_command():
            await self._enqueue_text_event(event)
            return
        if self._should_batch_media_event(event):
            await self._enqueue_media_event(event)
            return
        await self._handle_message_with_guards(event)

    # =========================================================================
    # Media batching
    # =========================================================================

    def _should_batch_media_event(self, event: MessageEvent) -> bool:
        return bool(
            event.media_urls
            and event.message_type in {MessageType.PHOTO, MessageType.VIDEO, MessageType.DOCUMENT, MessageType.AUDIO}
        )

    def _media_batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        session_key = build_session_key(
            event.source,
            **self._effective_session_isolation(),
            require_conversation_identity=True,
        )
        return f"{session_key}:media:{event.message_type.value}"

    @staticmethod
    def _media_batch_is_compatible(existing: MessageEvent, incoming: MessageEvent) -> bool:
        return (
            existing.message_type == incoming.message_type
            and existing.reply_to_message_id == incoming.reply_to_message_id
            and existing.reply_to_text == incoming.reply_to_text
            and existing.source.thread_id == incoming.source.thread_id
        )

    async def _enqueue_media_event(self, event: MessageEvent) -> None:
        key = self._media_batch_key(event)
        existing = self._pending_media_batches.get(key)
        if existing is None:
            self._pending_media_batches[key] = event
            self._schedule_media_batch_flush(key)
            return
        if not self._media_batch_is_compatible(existing, event):
            await self._flush_media_batch_now(key)
            self._pending_media_batches[key] = event
            self._schedule_media_batch_flush(key)
            return
        existing.media_urls.extend(event.media_urls)
        existing.media_types.extend(event.media_types)
        if event.text:
            existing.text = self._merge_caption(existing.text, event.text)
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._schedule_media_batch_flush(key)

    def _schedule_media_batch_flush(self, key: str) -> None:
        self._reschedule_batch_task(
            self._pending_media_batch_tasks,
            key,
            self._flush_media_batch,
        )

    async def _flush_media_batch(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._media_batch_delay_seconds)
            await self._flush_media_batch_now(key)
        finally:
            if self._pending_media_batch_tasks.get(key) is current_task:
                self._pending_media_batch_tasks.pop(key, None)

    async def _flush_media_batch_now(self, key: str) -> None:
        event = self._pending_media_batches.pop(key, None)
        if not event:
            return
        logger.info(
            "[Feishu] Flushing media batch %s with %d attachment(s)",
            key,
            len(event.media_urls),
        )
        await self._handle_message_with_guards(event)

    async def _download_remote_image(self, image_url: str) -> str:
        ext = self._guess_remote_extension(image_url, default=".jpg")
        return await cache_image_from_url(image_url, ext=ext)

    async def _download_remote_document(
        self,
        file_url: str,
        *,
        default_ext: str,
        preferred_name: str,
    ) -> tuple[str, str]:
        from tools.url_safety import is_safe_url
        if not is_safe_url(file_url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {file_url[:80]}")

        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                file_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                    "Accept": "*/*",
                },
            )
            response.raise_for_status()
            # Snapshot Content-Type and body while the client context is
            # still active so pooled connections fully release on exit.
            # See #18451.
            content_type_hdr = str(response.headers.get("Content-Type", ""))
            body = response.content
        filename = self._derive_remote_filename(
            file_url,
            content_type=content_type_hdr,
            default_name=preferred_name,
            default_ext=default_ext,
        )
        cached_path = cache_document_from_bytes(body, filename)
        return cached_path, filename

    @staticmethod
    def _guess_remote_extension(url: str, *, default: str) -> str:
        ext = Path((url or "").split("?", 1)[0]).suffix.lower()
        return ext if ext in (_IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS | set(SUPPORTED_DOCUMENT_TYPES)) else default

    @staticmethod
    def _derive_remote_filename(file_url: str, *, content_type: str, default_name: str, default_ext: str) -> str:
        candidate = Path((file_url or "").split("?", 1)[0]).name or default_name
        ext = Path(candidate).suffix.lower()
        if not ext:
            guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower() or "") or default_ext
            candidate = f"{candidate}{guessed}"
        return candidate

    @staticmethod
    def _namespace_from_mapping(value: Any) -> Any:
        if isinstance(value, dict):
            return SimpleNamespace(**{key: FeishuAdapter._namespace_from_mapping(item) for key, item in value.items()})
        if isinstance(value, list):
            return [FeishuAdapter._namespace_from_mapping(item) for item in value]
        return value

    async def _handle_webhook_request(self, request: Any) -> Any:
        remote_ip = (getattr(request, "remote", None) or "unknown")

        # Rate limiting — composite key: app_id:path:remote_ip (matches openclaw key structure).
        rate_key = f"{self._app_id}:{self._webhook_path}:{remote_ip}"
        if not self._check_webhook_rate_limit(rate_key):
            logger.warning("[Feishu] Webhook rate limit exceeded for %s", remote_ip)
            self._record_webhook_anomaly(remote_ip, "429")
            return web.Response(status=429, text="Too Many Requests")

        # Content-Type guard — Feishu always sends application/json.
        headers = getattr(request, "headers", {}) or {}
        content_type = str(headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            logger.warning("[Feishu] Webhook rejected: unexpected Content-Type %r from %s", content_type, remote_ip)
            self._record_webhook_anomaly(remote_ip, "415")
            return web.Response(status=415, text="Unsupported Media Type")

        # Body size guard — reject early via Content-Length when present.
        content_length = getattr(request, "content_length", None)
        if content_length is not None and content_length > _FEISHU_WEBHOOK_MAX_BODY_BYTES:
            logger.warning("[Feishu] Webhook body too large (%d bytes) from %s", content_length, remote_ip)
            self._record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Request body too large")

        try:
            body_bytes: bytes = await asyncio.wait_for(
                request.read(),
                timeout=_FEISHU_WEBHOOK_BODY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("[Feishu] Webhook body read timed out after %ds from %s", _FEISHU_WEBHOOK_BODY_TIMEOUT_SECONDS, remote_ip)
            self._record_webhook_anomaly(remote_ip, "408")
            return web.Response(status=408, text="Request Timeout")
        except Exception:
            self._record_webhook_anomaly(remote_ip, "400")
            return web.json_response({"code": 400, "msg": "failed to read body"}, status=400)

        if len(body_bytes) > _FEISHU_WEBHOOK_MAX_BODY_BYTES:
            logger.warning("[Feishu] Webhook body exceeds limit (%d bytes) from %s", len(body_bytes), remote_ip)
            self._record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Request body too large")

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._record_webhook_anomaly(remote_ip, "400")
            return web.json_response({"code": 400, "msg": "invalid json"}, status=400)

        # Verification token check — second layer of defence beyond signature (matches openclaw).
        if self._verification_token:
            header = payload.get("header") or {}
            incoming_token = str(header.get("token") or payload.get("token") or "")
            if not incoming_token or not hmac.compare_digest(incoming_token, self._verification_token):
                logger.warning("[Feishu] Webhook rejected: invalid verification token from %s", remote_ip)
                self._record_webhook_anomaly(remote_ip, "401-token")
                return web.Response(status=401, text="Invalid verification token")

        # URL verification challenge — Feishu includes the verification token in
        # challenge requests. Validate the token (above) before reflecting the
        # challenge so an unauthenticated remote request cannot prove endpoint
        # control by getting attacker-supplied challenge data echoed back.
        if payload.get("type") == "url_verification":
            return web.json_response({"challenge": payload.get("challenge", "")})

        # Timing-safe signature verification (only enforced when encrypt_key is set).
        if self._encrypt_key and not self._is_webhook_signature_valid(request.headers, body_bytes):
            logger.warning("[Feishu] Webhook rejected: invalid signature from %s", remote_ip)
            self._record_webhook_anomaly(remote_ip, "401-sig")
            return web.Response(status=401, text="Invalid signature")

        if payload.get("encrypt"):
            logger.error("[Feishu] Encrypted webhook payloads are not supported by Hermes webhook mode")
            self._record_webhook_anomaly(remote_ip, "400-encrypted")
            return web.json_response({"code": 400, "msg": "encrypted webhook payloads are not supported"}, status=400)

        self._clear_webhook_anomaly(remote_ip)

        event_type = str((payload.get("header") or {}).get("event_type") or "")
        data = self._namespace_from_mapping(payload)
        if event_type == "im.message.receive_v1":
            self._on_message_event(data, transport_kind="webhook")
        elif event_type == "im.message.message_read_v1":
            self._on_message_read_event(data)
        elif event_type == "im.chat.member.bot.added_v1":
            self._on_bot_added_to_chat(data)
        elif event_type == "im.chat.member.bot.deleted_v1":
            self._on_bot_removed_from_chat(data)
        elif event_type in {"im.message.reaction.created_v1", "im.message.reaction.deleted_v1"}:
            self._on_reaction_event(event_type, data)
        elif event_type == "card.action.trigger":
            self._on_card_action_trigger(data)
        elif event_type == "drive.notice.comment_add_v1":
            self._on_drive_comment_event(data)
        else:
            logger.debug("[Feishu] Ignoring webhook event type: %s", event_type or "unknown")
        return web.json_response({"code": 0, "msg": "ok"})

    def _is_webhook_signature_valid(self, headers: Any, body_bytes: bytes) -> bool:
        """Verify Feishu webhook signature using timing-safe comparison.

        Feishu signature algorithm:
            SHA256(timestamp + nonce + encrypt_key + body_string)
        Headers checked: x-lark-request-timestamp, x-lark-request-nonce, x-lark-signature.
        """
        timestamp = str(headers.get("x-lark-request-timestamp", "") or "")
        nonce = str(headers.get("x-lark-request-nonce", "") or "")
        signature = str(headers.get("x-lark-signature", "") or "")
        if not timestamp or not nonce or not signature:
            return False
        try:
            body_str = body_bytes.decode("utf-8", errors="replace")
            content = f"{timestamp}{nonce}{self._encrypt_key}{body_str}"
            computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return hmac.compare_digest(computed, signature)
        except Exception:
            logger.debug("[Feishu] Signature verification raised an exception", exc_info=True)
            return False

    def _check_webhook_rate_limit(self, rate_key: str) -> bool:
        """Return False when the composite rate_key has exceeded _FEISHU_WEBHOOK_RATE_LIMIT_MAX.

        The rate_key is composed as "{app_id}:{path}:{remote_ip}" — matching openclaw's key
        structure so the limit is scoped to a specific (account, endpoint, IP) triple rather
        than a bare IP, which causes fewer false-positive denials in multi-tenant setups.

        The tracking dict is capped at _FEISHU_WEBHOOK_RATE_MAX_KEYS entries to prevent unbounded
        memory growth. Stale (expired) entries are pruned when the cap is reached.
        """
        now = time.time()
        # Fast path: existing entry within the current window.
        entry = self._webhook_rate_counts.get(rate_key)
        if entry is not None:
            count, window_start = entry
            if now - window_start < _FEISHU_WEBHOOK_RATE_WINDOW_SECONDS:
                if count >= _FEISHU_WEBHOOK_RATE_LIMIT_MAX:
                    return False
                self._webhook_rate_counts[rate_key] = (count + 1, window_start)
                return True
        # New window for an existing key, or a brand-new key — prune stale entries first.
        if len(self._webhook_rate_counts) >= _FEISHU_WEBHOOK_RATE_MAX_KEYS:
            stale_keys = [
                k for k, (_, ws) in self._webhook_rate_counts.items()
                if now - ws >= _FEISHU_WEBHOOK_RATE_WINDOW_SECONDS
            ]
            for k in stale_keys:
                del self._webhook_rate_counts[k]
            # If still at capacity after pruning, allow through without tracking.
            if rate_key not in self._webhook_rate_counts and len(self._webhook_rate_counts) >= _FEISHU_WEBHOOK_RATE_MAX_KEYS:
                return True
        self._webhook_rate_counts[rate_key] = (1, now)
        return True

    # =========================================================================
    # Text batching
    # =========================================================================

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Return the session-scoped key used for Feishu text aggregation."""
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            **self._effective_session_isolation(),
            require_conversation_identity=True,
        )

    def _session_guard_isolation_options(self) -> Dict[str, bool]:
        return self._effective_session_isolation()

    def _effective_session_isolation(self) -> Dict[str, bool]:
        cfg = self._session_isolation_config
        if cfg is not None and hasattr(cfg, "effective_session_isolation"):
            return dict(cfg.effective_session_isolation(Platform.FEISHU))
        return {
            "group_sessions_per_user": self._group_sessions_per_user(),
            "thread_sessions_per_user": self._thread_sessions_per_user(),
        }

    def _group_sessions_per_user(self) -> bool:
        cfg = self._session_isolation_config
        if cfg is not None and hasattr(cfg, "group_sessions_per_user"):
            return bool(getattr(cfg, "group_sessions_per_user"))
        return bool(self.config.extra.get("group_sessions_per_user", False))

    def _thread_sessions_per_user(self) -> bool:
        cfg = self._session_isolation_config
        if cfg is not None and hasattr(cfg, "thread_sessions_per_user"):
            return bool(getattr(cfg, "thread_sessions_per_user"))
        return bool(self.config.extra.get("thread_sessions_per_user", False))

    @staticmethod
    def _text_batch_is_compatible(existing: MessageEvent, incoming: MessageEvent) -> bool:
        """Only merge text events when reply/thread context is identical."""
        return (
            existing.reply_to_message_id == incoming.reply_to_message_id
            and existing.reply_to_text == incoming.reply_to_text
            and existing.source.thread_id == incoming.source.thread_id
            and FeishuAdapter._reply_anchor_value(existing)
            == FeishuAdapter._reply_anchor_value(incoming)
        )

    async def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Debounce rapid Feishu text bursts into a single MessageEvent."""
        key = self._text_batch_key(event)
        chunk_len = len(event.text or "")
        existing = self._pending_text_batches.get(key)
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        if not self._text_batch_is_compatible(existing, event):
            await self._flush_text_batch_now(key)
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        existing_count = self._pending_text_batch_counts.get(key, 1)
        next_count = existing_count + 1
        appended_text = event.text or ""
        next_text = f"{existing.text}\n{appended_text}" if existing.text and appended_text else (existing.text or appended_text)
        if next_count > self._text_batch_max_messages or len(next_text) > self._text_batch_max_chars:
            await self._flush_text_batch_now(key)
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        existing.text = next_text
        existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._pending_text_batch_counts[key] = next_count
        self._schedule_text_batch_flush(key)

    def _schedule_text_batch_flush(self, key: str) -> None:
        """Reset the debounce timer for a pending Feishu text batch."""
        self._reschedule_batch_task(
            self._pending_text_batch_tasks,
            key,
            self._flush_text_batch,
        )

    @staticmethod
    def _reschedule_batch_task(
        task_map: Dict[str, asyncio.Task],
        key: str,
        flush_fn: Any,
    ) -> None:
        prior_task = task_map.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        task_map[key] = asyncio.create_task(flush_fn(key))

    async def _flush_text_batch(self, key: str) -> None:
        """Flush a pending text batch after the quiet period.

        Uses a longer delay when the latest chunk is near Feishu's ~4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay: if the latest chunk is near the split threshold,
            # a continuation is almost certain — wait longer.
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            await self._flush_text_batch_now(key)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _flush_text_batch_now(self, key: str) -> None:
        """Dispatch the current text batch immediately."""
        event = self._pending_text_batches.pop(key, None)
        self._pending_text_batch_counts.pop(key, None)
        if not event:
            return
        logger.info(
            "[Feishu] Flushing text batch %s (%d chars)",
            key,
            len(event.text or ""),
        )
        await self._handle_message_with_guards(event)

    # =========================================================================
    # Message content extraction and resource download
    # =========================================================================

    async def _extract_message_content(
        self, message: Any
    ) -> tuple[str, MessageType, List[str], List[str], List[FeishuMentionRef]]:
        raw_content = getattr(message, "content", "") or ""
        raw_type = getattr(message, "message_type", "") or ""
        message_id = str(getattr(message, "message_id", "") or "")
        logger.info("[Feishu] Received raw message type=%s message_id=%s", raw_type, message_id)

        normalized = normalize_feishu_message(
            message_type=raw_type,
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        media_urls, media_types = await self._download_feishu_message_resources(
            message_id=message_id,
            normalized=normalized,
        )
        inbound_type = self._resolve_normalized_message_type(normalized, media_types)
        text = normalized.text_content

        if (
            inbound_type in {MessageType.DOCUMENT, MessageType.AUDIO, MessageType.VIDEO, MessageType.PHOTO}
            and len(media_urls) == 1
            and normalized.preferred_message_type in {"document", "audio"}
        ):
            injected = await self._maybe_extract_text_document(media_urls[0], media_types[0])
            if injected:
                text = injected

        return text, inbound_type, media_urls, media_types, list(normalized.mentions)

    async def _download_feishu_message_resources(
        self,
        *,
        message_id: str,
        normalized: FeishuNormalizedMessage,
    ) -> tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []

        for image_key in normalized.image_keys:
            cached_path, media_type = await self._download_feishu_image(
                message_id=message_id,
                image_key=image_key,
            )
            if cached_path:
                media_urls.append(cached_path)
                media_types.append(media_type)

        for media_ref in normalized.media_refs:
            cached_path, media_type = await self._download_feishu_message_resource(
                message_id=message_id,
                file_key=media_ref.file_key,
                resource_type=media_ref.resource_type,
                fallback_filename=media_ref.file_name,
            )
            if cached_path:
                media_urls.append(cached_path)
                media_types.append(media_type)

        return media_urls, media_types

    @staticmethod
    def _resolve_media_message_type(media_type: str, *, default: MessageType) -> MessageType:
        normalized = (media_type or "").lower()
        if normalized.startswith("image/"):
            return MessageType.PHOTO
        if normalized.startswith("audio/"):
            return MessageType.AUDIO
        if normalized.startswith("video/"):
            return MessageType.VIDEO
        return default

    def _resolve_normalized_message_type(
        self,
        normalized: FeishuNormalizedMessage,
        media_types: List[str],
    ) -> MessageType:
        preferred = normalized.preferred_message_type
        if preferred == "photo":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.PHOTO)
        if preferred == "audio":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.AUDIO)
        if preferred == "document":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.DOCUMENT)
        return MessageType.TEXT

    async def _maybe_extract_text_document(self, cached_path: str, media_type: str) -> str:
        if not cached_path or not media_type.startswith("text/"):
            return ""
        try:
            if os.path.getsize(cached_path) > _MAX_TEXT_INJECT_BYTES:
                return ""
            ext = Path(cached_path).suffix.lower()
            if ext not in {".txt", ".md"} and media_type not in {"text/plain", "text/markdown"}:
                return ""
            content = Path(cached_path).read_text(encoding="utf-8")
            display_name = self._display_name_from_cached_path(cached_path)
            return f"[Content of {display_name}]:\n{content}"
        except (OSError, UnicodeDecodeError):
            logger.warning("[Feishu] Failed to inject text document content from %s", cached_path, exc_info=True)
            return ""

    async def _download_feishu_image(self, *, message_id: str, image_key: str) -> tuple[str, str]:
        if not self._client or not message_id:
            return "", ""
        try:
            request = self._build_message_resource_request(
                message_id=message_id,
                file_key=image_key,
                resource_type="image",
            )
            response = await asyncio.to_thread(self._client.im.v1.message_resource.get, request)
            if not response or not response.success():
                logger.warning(
                    "[Feishu] Failed to download image %s: %s %s",
                    image_key,
                    getattr(response, "code", "unknown"),
                    getattr(response, "msg", "request failed"),
                )
                return "", ""
            raw_bytes = self._read_binary_response(response)
            if not raw_bytes:
                return "", ""
            content_type = self._get_response_header(response, "Content-Type")
            filename = getattr(response, "file_name", None) or f"{image_key}.jpg"
            ext = self._guess_extension(filename, content_type, ".jpg", allowed=_IMAGE_EXTENSIONS)
            cached_path = cache_image_from_bytes(raw_bytes, ext=ext)
            media_type = self._normalize_media_type(content_type, default=self._default_image_media_type(ext))
            return cached_path, media_type
        except Exception:
            logger.warning("[Feishu] Failed to cache image resource %s", image_key, exc_info=True)
            return "", ""

    async def _download_feishu_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        fallback_filename: str,
    ) -> tuple[str, str]:
        if not self._client or not message_id:
            return "", ""

        request_types = [resource_type]
        if resource_type in {"audio", "media"}:
            request_types.append("file")

        for request_type in request_types:
            try:
                request = self._build_message_resource_request(
                    message_id=message_id,
                    file_key=file_key,
                    resource_type=request_type,
                )
                response = await asyncio.to_thread(self._client.im.v1.message_resource.get, request)
                if not response or not response.success():
                    logger.debug(
                        "[Feishu] Resource download failed for %s/%s via type=%s: %s %s",
                        message_id,
                        file_key,
                        request_type,
                        getattr(response, "code", "unknown"),
                        getattr(response, "msg", "request failed"),
                    )
                    continue

                raw_bytes = self._read_binary_response(response)
                if not raw_bytes:
                    continue
                content_type = self._get_response_header(response, "Content-Type")
                response_filename = getattr(response, "file_name", None) or ""
                filename = response_filename or fallback_filename or f"{request_type}_{file_key}"
                media_type = self._normalize_media_type(
                    content_type,
                    default=self._guess_media_type_from_filename(filename),
                )

                if media_type.startswith("image/"):
                    ext = self._guess_extension(filename, content_type, ".jpg", allowed=_IMAGE_EXTENSIONS)
                    cached_path = cache_image_from_bytes(raw_bytes, ext=ext)
                    logger.info("[Feishu] Cached message image resource at %s", cached_path)
                    return cached_path, media_type or self._default_image_media_type(ext)

                if request_type == "audio" or media_type.startswith("audio/"):
                    ext = self._guess_extension(filename, content_type, ".ogg", allowed=_AUDIO_EXTENSIONS)
                    cached_path = cache_audio_from_bytes(raw_bytes, ext=ext)
                    logger.info("[Feishu] Cached message audio resource at %s", cached_path)
                    return cached_path, (media_type or f"audio/{ext.lstrip('.') or 'ogg'}")

                if media_type.startswith("video/"):
                    if not Path(filename).suffix:
                        filename = f"{filename}.mp4"
                    cached_path = cache_document_from_bytes(raw_bytes, filename)
                    logger.info("[Feishu] Cached message video resource at %s", cached_path)
                    return cached_path, media_type

                if not Path(filename).suffix and media_type in _DOCUMENT_MIME_TO_EXT:
                    filename = f"{filename}{_DOCUMENT_MIME_TO_EXT[media_type]}"
                cached_path = cache_document_from_bytes(raw_bytes, filename)
                logger.info("[Feishu] Cached message document resource at %s", cached_path)
                return cached_path, (media_type or self._guess_document_media_type(filename))
            except Exception:
                logger.warning(
                    "[Feishu] Failed to cache message resource %s/%s",
                    message_id,
                    file_key,
                    exc_info=True,
                )
        return "", ""

    # =========================================================================
    # Static helpers — extension / media-type guessing
    # =========================================================================

    @staticmethod
    def _read_binary_response(response: Any) -> bytes:
        file_obj = getattr(response, "file", None)
        if file_obj is None:
            return b""
        if hasattr(file_obj, "getvalue"):
            return bytes(file_obj.getvalue())
        return bytes(file_obj.read())

    @staticmethod
    def _get_response_header(response: Any, name: str) -> str:
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", {}) or {}
        return str(headers.get(name, headers.get(name.lower(), "")) or "").split(";", 1)[0].strip().lower()

    @staticmethod
    def _guess_extension(filename: str, content_type: str, default: str, *, allowed: set[str]) -> str:
        ext = Path(filename or "").suffix.lower()
        if ext in allowed:
            return ext
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower() or "")
        if guessed in allowed:
            return guessed
        return default

    @staticmethod
    def _normalize_media_type(content_type: str, *, default: str) -> str:
        normalized = (content_type or "").split(";", 1)[0].strip().lower()
        return normalized or default

    @staticmethod
    def _guess_document_media_type(filename: str) -> str:
        ext = Path(filename or "").suffix.lower()
        return SUPPORTED_DOCUMENT_TYPES.get(ext, mimetypes.guess_type(filename or "")[0] or "application/octet-stream")

    @staticmethod
    def _display_name_from_cached_path(path: str) -> str:
        basename = os.path.basename(path)
        parts = basename.split("_", 2)
        display_name = parts[2] if len(parts) >= 3 else basename
        return re.sub(r"[^\w.\- ]", "_", display_name)

    @staticmethod
    def _guess_media_type_from_filename(filename: str) -> str:
        guessed = (mimetypes.guess_type(filename or "")[0] or "").lower()
        if guessed:
            return guessed
        ext = Path(filename or "").suffix.lower()
        if ext in _VIDEO_EXTENSIONS:
            return f"video/{ext.lstrip('.')}"
        if ext in _AUDIO_EXTENSIONS:
            return f"audio/{ext.lstrip('.')}"
        if ext in _IMAGE_EXTENSIONS:
            return FeishuAdapter._default_image_media_type(ext)
        return ""

    @staticmethod
    def _map_chat_type(raw_chat_type: str) -> str:
        normalized = (raw_chat_type or "").strip().lower()
        if normalized == "p2p":
            return "dm"
        if "topic" in normalized or "thread" in normalized or "forum" in normalized:
            return "forum"
        if normalized == "group":
            return "group"
        return "dm"

    @staticmethod
    def _resolve_source_chat_type(
        *,
        chat_info: Dict[str, Any],
        event_chat_type: str,
        prefer_event_chat_type: bool = False,
    ) -> str:
        if prefer_event_chat_type:
            mapped_event_type = FeishuAdapter._map_chat_type(event_chat_type)
            if mapped_event_type in {"dm", "group", "forum"}:
                return mapped_event_type
        resolved = str(chat_info.get("type") or "").strip().lower()
        if resolved in {"group", "forum"}:
            return resolved
        if resolved in {"dm", "p2p"}:
            return "dm"
        if event_chat_type == "p2p":
            return "dm"
        return "group"

    async def _resolve_sender_profile(
        self,
        sender_id: Any,
        *,
        is_bot: bool = False,
    ) -> Dict[str, Optional[str]]:
        """Map Feishu's three-tier user IDs onto Hermes' SessionSource fields.

        Preference order for the primary ``user_id`` field:
          1. user_id  (tenant-scoped, most stable — requires permission scope)
          2. open_id  (app-scoped, always available — different per bot app)

        ``user_id_alt`` carries the union_id (developer-scoped, stable across
        all apps by the same developer).  Session-key generation prefers
        user_id_alt when present, so participant isolation stays stable even
        if the primary ID is the app-scoped open_id.
        """
        open_id = getattr(sender_id, "open_id", None) or None
        user_id = getattr(sender_id, "user_id", None) or None
        union_id = getattr(sender_id, "union_id", None) or None
        # Prefer tenant-scoped user_id; fall back to app-scoped open_id.
        primary_id = user_id or open_id
        # bot/v3/bots/basic_batch only accepts open_id.
        name_lookup_id = open_id if is_bot else (primary_id or union_id)
        display_name = await self._resolve_sender_name_from_api(
            name_lookup_id, is_bot=is_bot,
        )
        return {
            "user_id": primary_id,
            "user_name": display_name,
            "user_id_alt": union_id,
        }

    def _get_cached_sender_name(self, sender_id: Optional[str]) -> Optional[str]:
        """Return a cached sender name only while its TTL is still valid."""
        if not sender_id:
            return None
        cached = self._sender_name_cache.get(sender_id)
        if cached is None:
            return None
        name, expire_at = cached
        if time.time() < expire_at:
            return name
        self._sender_name_cache.pop(sender_id, None)
        return None

    async def _resolve_sender_name_from_api(
        self,
        sender_id: Optional[str],
        *,
        is_bot: bool = False,
    ) -> Optional[str]:
        """Bots divert to bot/basic_batch — contact API doesn't return bot names.
        Failures are silent so the pipeline never blocks on name resolution.
        """
        if not sender_id or not self._client:
            return None
        trimmed = sender_id.strip()
        if not trimmed:
            return None
        now = time.time()
        cached_name = self._get_cached_sender_name(trimmed)
        if cached_name is not None:
            return cached_name or None  # "" cached means "known nameless"
        if is_bot:
            names = await self._fetch_bot_names([trimmed])
            if names is None:
                return None
            expire_at = now + _FEISHU_SENDER_NAME_TTL_SECONDS
            for oid, name in names.items():
                self._sender_name_cache[oid] = (name, expire_at)
            hit = self._sender_name_cache.get(trimmed)
            return (hit[0] or None) if hit else None
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest  # lazy import
            if trimmed.startswith("ou_"):
                id_type = "open_id"
            elif trimmed.startswith("on_"):
                id_type = "union_id"
            else:
                id_type = "user_id"
            request = GetUserRequest.builder().user_id(trimmed).user_id_type(id_type).build()
            response = await asyncio.to_thread(self._client.contact.v3.user.get, request)
            if not response or not response.success():
                return None
            user = getattr(getattr(response, "data", None), "user", None)
            name = (
                getattr(user, "name", None)
                or getattr(user, "display_name", None)
                or getattr(user, "nickname", None)
                or getattr(user, "en_name", None)
            )
            if name and isinstance(name, str):
                name = name.strip()
                if name:
                    self._sender_name_cache[trimmed] = (name, now + _FEISHU_SENDER_NAME_TTL_SECONDS)
                    return name
        except Exception:
            logger.debug("[Feishu] Failed to resolve sender name for %s", sender_id, exc_info=True)
        return None

    async def _fetch_bot_names(self, bot_ids: List[str]) -> Optional[Dict[str, str]]:
        if not self._client or not bot_ids:
            return None
        try:
            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri("/open-apis/bot/v3/bots/basic_batch")
                .queries([("bot_ids", oid) for oid in bot_ids])
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = await asyncio.to_thread(self._client.request, req)
            content = getattr(getattr(resp, "raw", None), "content", None)
            if not content:
                return None
            payload = json.loads(content)
            if payload.get("code") != 0:
                return None
            bots = (payload.get("data") or {}).get("bots") or {}
            return {
                oid: str(info.get("name") or "").strip()
                for oid, info in bots.items()
                if oid
            }
        except Exception:
            logger.debug("[Feishu] Failed to fetch bot names for %s", bot_ids, exc_info=True)
            return None

    async def _fetch_message_text(self, message_id: str) -> Optional[str]:
        if not self._client or not message_id:
            return None
        if message_id in self._message_text_cache:
            return self._message_text_cache[message_id]
        try:
            request = self._build_get_message_request(message_id)
            response = await asyncio.to_thread(self._client.im.v1.message.get, request)
            if not response or getattr(response, "success", lambda: False)() is False:
                code = getattr(response, "code", "unknown")
                msg = getattr(response, "msg", "message lookup failed")
                logger.warning("[Feishu] Failed to fetch parent message %s: [%s] %s", message_id, code, msg)
                return None
            items = getattr(getattr(response, "data", None), "items", None) or []
            parent = items[0] if items else None
            body = getattr(parent, "body", None)
            msg_type = getattr(parent, "msg_type", "") or ""
            raw_content = getattr(body, "content", "") or ""
            parent_mentions = getattr(parent, "mentions", None) if parent else None
            text = self._extract_text_from_raw_content(
                msg_type=msg_type,
                raw_content=raw_content,
                mentions=parent_mentions,
            )
            self._message_text_cache[message_id] = text
            return text
        except Exception:
            logger.warning("[Feishu] Failed to fetch parent message %s", message_id, exc_info=True)
            return None

    def _extract_text_from_raw_content(
        self,
        *,
        msg_type: str,
        raw_content: str,
        mentions: Optional[Sequence[Any]] = None,
    ) -> Optional[str]:
        normalized = normalize_feishu_message(
            message_type=msg_type,
            raw_content=raw_content,
            mentions=mentions,
            bot=self._bot_identity(),
        )
        if normalized.text_content:
            return normalized.text_content
        placeholder = normalized.metadata.get("placeholder_text") if isinstance(normalized.metadata, dict) else None
        return str(placeholder).strip() or None

    @staticmethod
    def _default_image_media_type(ext: str) -> str:
        normalized_ext = (ext or "").lower()
        if normalized_ext in {".jpg", ".jpeg"}:
            return "image/jpeg"
        return f"image/{normalized_ext.lstrip('.') or 'jpeg'}"

    @staticmethod
    def _log_background_failure(future: Any) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("[Feishu] Background inbound processing failed")

    # =========================================================================
    # Inbound admission
    # =========================================================================

    def _admit(self, sender: Any, message: Any) -> Optional[RejectReason]:
        sender_ids = _sender_identity(sender)
        self_ids = frozenset(v for v in (self._bot_open_id, self._bot_user_id) if v)
        is_bot = _is_bot_sender(sender)
        is_group = getattr(message, "chat_type", "p2p") != "p2p"
        chat_id = getattr(message, "chat_id", "") or ""
        require_mention = is_group and self._require_mention_for(chat_id)

        # Defensive only — Feishu doesn't echo our outbound back as inbound,
        # and open_id is always populated on both sides.
        if self_ids and sender_ids & self_ids:
            return "self_echo"

        if is_bot:
            mode = self._allow_bots
            if mode != "mentions" and mode != "all":
                return "bots_disabled"
            # Defensive: pre-hydration or malformed payloads.
            if not self_ids or not sender_ids:
                return "self_ids_unknown"
            # Step 4 covers mention enforcement for groups when require_mention
            # is on; check here only on paths step 4 won't reach.
            if mode == "mentions" and not require_mention and not self._mentions_self(message):
                return "bot_not_mentioned"

        if not is_group:
            return None

        if not self._allow_group_message(
            getattr(sender, "sender_id", None), chat_id, is_bot=is_bot,
        ):
            return "group_policy_rejected"
        if require_mention and not self._mentions_self(message):
            return "group_policy_rejected"
        return None

    def _require_mention_for(self, chat_id: str) -> bool:
        rule = self._group_rules.get(chat_id) if chat_id else None
        if rule and rule.require_mention is not None:
            return rule.require_mention
        return self._require_mention

    # --- Group policy ---------------------------------------------------------

    def _allow_group_message(
        self,
        sender_id: Any,
        chat_id: str = "",
        *,
        is_bot: bool = False,
    ) -> bool:
        """Per-group policy gate for non-DM traffic."""
        sender_open_id = getattr(sender_id, "open_id", None)
        sender_user_id = getattr(sender_id, "user_id", None)
        sender_ids = {sender_open_id, sender_user_id} - {None}

        if sender_ids and self._admins and (sender_ids & self._admins):
            return True

        rule = self._group_rules.get(chat_id) if chat_id else None
        if rule:
            policy = rule.policy
            allowlist = rule.allowlist
            blacklist = rule.blacklist
        else:
            policy = self._default_group_policy or self._group_policy
            allowlist = self._allowed_group_users
            blacklist = set()

        # Channel locks apply to everyone; allowlist/blacklist only gate humans
        # (bots were already cleared upstream by FEISHU_ALLOW_BOTS).
        if policy == "disabled":
            return False
        if policy == "open":
            return True
        if policy == "admin_only":
            return False
        if is_bot:
            return True

        if policy == "allowlist":
            return bool(sender_ids and (sender_ids & allowlist))
        if policy == "blacklist":
            return bool(sender_ids and not (sender_ids & blacklist))

        return bool(sender_ids and (sender_ids & self._allowed_group_users))

    # --- Mention detection ----------------------------------------------------

    def _mentions_self(self, message: Any) -> bool:
        # @_all is Feishu's @everyone placeholder.
        raw_content = getattr(message, "content", "") or ""
        if "@_all" in raw_content:
            return True
        mentions = getattr(message, "mentions", None) or []
        if mentions and self._message_mentions_bot(mentions):
            return True
        normalized = normalize_feishu_message(
            message_type=getattr(message, "message_type", "") or "",
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        return self._post_mentions_bot(normalized.mentions)

    def _message_mentions_bot(self, mentions: List[Any]) -> bool:
        # IDs trump names: when both sides have open_id (or both user_id),
        # match requires equal IDs. Name fallback only when either side
        # lacks an ID.
        for mention in mentions:
            mention_id = getattr(mention, "id", None)
            mention_open_id = (getattr(mention_id, "open_id", None) or "").strip()
            mention_user_id = (getattr(mention_id, "user_id", None) or "").strip()
            mention_name = (getattr(mention, "name", None) or "").strip()

            if mention_open_id and self._bot_open_id:
                if mention_open_id == self._bot_open_id:
                    return True
                continue  # IDs differ — not the bot; skip name fallback.
            if mention_user_id and self._bot_user_id:
                if mention_user_id == self._bot_user_id:
                    return True
                continue
            if self._bot_name and mention_name == self._bot_name:
                return True

        return False

    def _post_mentions_bot(self, mentions: List[FeishuMentionRef]) -> bool:
        return any(m.is_self for m in mentions)

    def _bot_identity(self) -> _FeishuBotIdentity:
        return _FeishuBotIdentity(
            open_id=self._bot_open_id,
            user_id=self._bot_user_id,
            name=self._bot_name,
        )

    async def _hydrate_bot_identity(self) -> None:
        """Best-effort discovery of bot identity for precise group mention gating
        and self-sent bot event filtering.

        Populates ``_bot_open_id`` and ``_bot_name`` from /open-apis/bot/v3/info
        (no extra scopes required beyond the tenant access token). The probe
        always runs when a client is available so stale env vars from app/bot
        migrations do not break group @mention gating. Falls back to the
        application info endpoint for ``_bot_name`` only when the first probe
        doesn't return it. If the probe fails, env-provided values are preserved.
        """
        if not self._client:
            return

        # Primary probe: /open-apis/bot/v3/info — returns bot_name + open_id, no
        # extra scopes required. This is the same endpoint the onboarding wizard
        # uses via probe_bot().
        try:
            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = await asyncio.to_thread(self._client.request, req)
            content = getattr(getattr(resp, "raw", None), "content", None)
            if content:
                payload = json.loads(content)
                parsed = _parse_bot_response(payload) or {}
                open_id = (parsed.get("bot_open_id") or "").strip()
                bot_name = (parsed.get("bot_name") or "").strip()
                if open_id:
                    if self._bot_open_id and self._bot_open_id != open_id:
                        logger.warning(
                            "[Feishu] FEISHU_BOT_OPEN_ID is stale; using /bot/v3/info open_id for group @mention gating."
                        )
                    self._bot_open_id = open_id
                if bot_name:
                    if self._bot_name and self._bot_name != bot_name:
                        logger.info(
                            "[Feishu] FEISHU_BOT_NAME differs from /bot/v3/info; using hydrated bot name for group @mention gating."
                        )
                    self._bot_name = bot_name
        except Exception:
            logger.debug(
                "[Feishu] /bot/v3/info probe failed during hydration",
                exc_info=True,
            )

        # Fallback probe for _bot_name only: application info endpoint. Needs
        # admin:app.info:readonly or application:application:self_manage scope,
        # so it's best-effort.
        if self._bot_name:
            return
        try:
            request = self._build_get_application_request(app_id=self._app_id, lang="en_us")
            response = await asyncio.to_thread(self._client.application.v6.application.get, request)
            if not response or not response.success():
                code = getattr(response, "code", None)
                if code == 99991672:
                    logger.warning(
                        "[Feishu] Unable to hydrate bot name from application info. "
                        "Grant admin:app.info:readonly or application:application:self_manage "
                        "so group @mention gating can resolve the bot name precisely."
                    )
                return
            app = getattr(getattr(response, "data", None), "app", None)
            app_name = (getattr(app, "app_name", None) or "").strip()
            if app_name and not self._bot_name:
                self._bot_name = app_name
        except Exception:
            logger.debug("[Feishu] Failed to hydrate bot name from application info", exc_info=True)

    # =========================================================================
    # Deduplication — seen message ID cache (persistent)
    # =========================================================================

    def _load_seen_message_ids(self) -> None:
        try:
            payload = json.loads(self._dedup_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            logger.warning("[Feishu] Failed to load persisted dedup state from %s", self._dedup_state_path, exc_info=True)
            return
        seen_data = payload.get("message_ids", {}) if isinstance(payload, dict) else {}
        now = time.time()
        ttl = _FEISHU_DEDUP_TTL_SECONDS
        # Backward-compat: old format stored a plain list of IDs (no timestamps).
        if isinstance(seen_data, list):
            entries: Dict[str, float] = {str(item).strip(): 0.0 for item in seen_data if str(item).strip()}
        elif isinstance(seen_data, dict):
            entries = {}
            for key, value in seen_data.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                try:
                    entries[key] = float(value)
                except (TypeError, ValueError):
                    continue
        else:
            return
        # Filter out TTL-expired entries (entries saved with ts=0.0 are treated as immortal
        # for one migration cycle to avoid nuking old data on first upgrade).
        valid: Dict[str, float] = {
            msg_id: ts for msg_id, ts in entries.items()
            if ts == 0.0 or ttl <= 0 or now - ts < ttl
        }
        # Apply size cap; keep the most recently seen IDs.
        sorted_ids = sorted(valid, key=lambda k: valid[k], reverse=True)[:self._dedup_cache_size]
        self._seen_message_order = list(reversed(sorted_ids))
        self._seen_message_ids = {k: valid[k] for k in sorted_ids}

    def _persist_seen_message_ids(self) -> None:
        try:
            self._dedup_state_path.parent.mkdir(parents=True, exist_ok=True)
            recent = self._seen_message_order[-self._dedup_cache_size:]
            # Save as {msg_id: timestamp} so TTL filtering works across restarts.
            payload = {"message_ids": {k: self._seen_message_ids[k] for k in recent if k in self._seen_message_ids}}
            atomic_json_write(self._dedup_state_path, payload, indent=None)
        except OSError:
            logger.warning("[Feishu] Failed to persist dedup state to %s", self._dedup_state_path, exc_info=True)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        ttl = _FEISHU_DEDUP_TTL_SECONDS
        with self._dedup_lock:
            seen_at = self._seen_message_ids.get(message_id)
            if seen_at is not None and (ttl <= 0 or now - seen_at < ttl):
                return True
            # Record with current wall-clock timestamp so TTL works across restarts.
            self._seen_message_ids[message_id] = now
            self._seen_message_order.append(message_id)
            while len(self._seen_message_order) > self._dedup_cache_size:
                stale = self._seen_message_order.pop(0)
                self._seen_message_ids.pop(stale, None)
            self._persist_seen_message_ids()
            return False

    def _remember_legacy_seen_message_id(self, message_id: str) -> None:
        """Maintain the old seen-id file without using it as admission authority."""
        now = time.time()
        with self._dedup_lock:
            self._seen_message_ids[message_id] = now
            if message_id in self._seen_message_order:
                self._seen_message_order.remove(message_id)
            self._seen_message_order.append(message_id)
            while len(self._seen_message_order) > self._dedup_cache_size:
                stale = self._seen_message_order.pop(0)
                self._seen_message_ids.pop(stale, None)
            self._persist_seen_message_ids()

    # =========================================================================
    # Outbound payload construction and send pipeline
    # =========================================================================

    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        # Feishu post-type 'md' elements do not render markdown tables; sending
        # table content as post causes the message to appear blank on the client.
        # Force plain text for anything that looks like a markdown table.
        if _MARKDOWN_TABLE_RE.search(content):
            text_payload = {"text": content}
            return "text", json.dumps(text_payload, ensure_ascii=False)
        if _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        text_payload = {"text": content}
        return "text", json.dumps(text_payload, ensure_ascii=False)

    @staticmethod
    def _attachment_mime_class_for_message_type(message_type: str) -> str:
        if message_type == "audio":
            return "audio"
        if message_type == "media":
            return "media"
        return "file"

    async def _preflight_attachment_upload(
        self,
        *,
        chat_id: str,
        file_path: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        declared_mime_class: str,
        upload_kind: str,
    ) -> Optional[SendResult]:
        del upload_kind
        failure_class = self._attachment_provenance_failure(
            file_path=file_path,
            metadata=metadata,
            declared_mime_class=declared_mime_class,
        )
        if failure_class is not None:
            denial_recorded = await self._record_attachment_upload_denial(
                metadata=metadata,
                failure_class=failure_class,
                declared_mime_class=declared_mime_class,
            )
            if not denial_recorded:
                return SendResult(
                    success=False,
                    error="feishu_attachment_denial_apply_failed",
                )
            return SendResult(success=False, error=failure_class)

        if self._gateway_event_state_dir is None:
            return None
        operation, operation_error = self._send_delivery_operation(
            reply_to=reply_to,
            metadata=metadata,
        )
        if operation_error is not None:
            return SendResult(success=False, error=operation_error)
        explicit_delivery_id = (metadata or {}).get("delivery_id")
        if not isinstance(explicit_delivery_id, str) or not explicit_delivery_id:
            denial_recorded = await self._record_attachment_upload_denial(
                metadata=metadata,
                failure_class="feishu_attachment_provenance_mismatch",
                declared_mime_class=declared_mime_class,
            )
            if not denial_recorded:
                return SendResult(
                    success=False,
                    error="feishu_attachment_denial_apply_failed",
                )
            return SendResult(
                success=False,
                error="feishu_attachment_provenance_mismatch",
            )
        delivery_id = self._delivery_id_for(
            operation,
            metadata=metadata,
            parts=[chat_id, reply_to or "", declared_mime_class],
        )
        reservation_refs = self._attachment_upload_reservation_refs(
            chat_id=chat_id,
            reply_to=reply_to,
            metadata=metadata,
            delivery_id=delivery_id,
            declared_mime_class=declared_mime_class,
        )
        pending_result = await self._apply_delivery_pending(
            delivery_id=delivery_id,
            operation=operation,
            inbound_id=reservation_refs["inbound_id"],
            target=reservation_refs["target"],
            session_id=reservation_refs["session_id"],
            correlation_id=reservation_refs["correlation_id"],
        )
        replay = self._send_result_from_delivery_record_apply(
            pending_result,
            delivery_id=delivery_id,
            inbound_id=reservation_refs["inbound_id"],
            target=reservation_refs["target"],
            session_id=reservation_refs["session_id"],
            correlation_id=reservation_refs["correlation_id"],
        )
        if replay is not None:
            return replay
        if not self._gateway_event_apply_succeeded(pending_result):
            return SendResult(success=False, error="delivery_pending apply failed")
        return None

    async def _deny_raw_remote_attachment_upload(
        self,
        *,
        metadata: Optional[Dict[str, Any]],
        declared_mime_class: str,
    ) -> SendResult:
        failure_class = "feishu_arbitrary_local_upload_denied"
        denial_recorded = await self._record_attachment_upload_denial(
            metadata=metadata,
            failure_class=failure_class,
            declared_mime_class=declared_mime_class,
        )
        if not denial_recorded:
            return SendResult(
                success=False,
                error="feishu_attachment_denial_apply_failed",
            )
        return SendResult(success=False, error=failure_class)

    def _attachment_upload_reservation_refs(
        self,
        *,
        chat_id: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        delivery_id: str,
        declared_mime_class: Optional[str] = None,
    ) -> Dict[str, str]:
        provenance_hash = (metadata or {}).get("feishu_attachment_provenance_hash")
        if not self._is_sha256_ref_text(provenance_hash):
            return {
                "inbound_id": self._delivery_metadata(
                    metadata, "inbound_id", reply_to or chat_id
                ),
                "target": f"feishu:chat:{chat_id}",
                "session_id": self._delivery_metadata(metadata, "session_id", "session"),
                "correlation_id": self._delivery_metadata(
                    metadata, "correlation_id", delivery_id
                ),
            }
        route_evidence = self._attachment_route_evidence(metadata) or {}
        route_partition_hash = route_evidence.get("route_partition_hash")
        if not self._is_sha256_ref_text(route_partition_hash):
            route_partition_hash = self._delivery_ref_hash(
                "feishu_attachment_route_partition", "missing"
            )
        contract_hash = route_evidence.get("contract_hash")
        if not self._is_sha256_ref_text(contract_hash):
            contract_hash = self._delivery_ref_hash(
                "feishu_attachment_contract", "missing"
            )
        target_ref_hash = self._delivery_ref_hash(
            "feishu_target",
            f"feishu:chat:{chat_id}",
        )
        reply_ref_hash = self._delivery_ref_hash(
            "feishu_reply_anchor",
            reply_to or "none",
        )
        reservation_hash = self._sha256_ref_text(
            "\x1f".join(
                (
                    "feishu_attachment_upload_reservation",
                    delivery_id,
                    str(provenance_hash),
                    target_ref_hash,
                    str(route_partition_hash),
                    str(contract_hash),
                    reply_ref_hash,
                    str(declared_mime_class or "attachment"),
                )
            )
        )
        return {
            "inbound_id": f"feishu_attachment_inbound:{reservation_hash[7:47]}",
            "target": f"feishu_attachment_target:{target_ref_hash[7:47]}",
            "session_id": f"feishu_attachment_route:{str(route_partition_hash)[7:47]}",
            "correlation_id": f"feishu_attachment_contract:{str(contract_hash)[7:47]}",
        }

    def _attachment_upload_ledger_message_id(self, message_id: str) -> str:
        digest = self._delivery_ref_hash("feishu_message", message_id)[7:47]
        return f"om_attachment_{digest}"

    @staticmethod
    def _is_attachment_upload_ledger_message_id(message_id: str) -> bool:
        return bool(re.fullmatch(r"om_attachment_[a-f0-9]{40}", message_id))

    async def _terminalize_attachment_upload_failure(
        self,
        *,
        chat_id: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        declared_mime_class: str,
        failure_class: str,
    ) -> Optional[SendResult]:
        if self._gateway_event_state_dir is None:
            return None
        if not self._is_sha256_ref_text(
            (metadata or {}).get("feishu_attachment_provenance_hash")
        ):
            return None
        operation, operation_error = self._send_delivery_operation(
            reply_to=reply_to,
            metadata=metadata,
        )
        if operation_error is not None:
            return SendResult(success=False, error=operation_error)
        explicit_delivery_id = (metadata or {}).get("delivery_id")
        if not isinstance(explicit_delivery_id, str) or not explicit_delivery_id:
            return None
        delivery_id = self._delivery_id_for(
            operation,
            metadata=metadata,
            parts=[chat_id, reply_to or "", declared_mime_class],
        )
        if not await self._apply_delivery_failed(delivery_id, failure_class):
            return SendResult(success=False, error="delivery_failed apply failed")
        return None

    def _attachment_provenance_failure(
        self,
        *,
        file_path: str,
        metadata: Optional[Dict[str, Any]],
        declared_mime_class: str,
    ) -> Optional[str]:
        del file_path
        if self._gateway_event_state_dir is None:
            return "feishu_arbitrary_local_upload_denied"
        provenance_hash = (metadata or {}).get("feishu_attachment_provenance_hash")
        if not self._is_sha256_ref_text(provenance_hash):
            return "feishu_arbitrary_local_upload_denied"
        reader = getattr(
            gateway_event_ledger,
            "feishu_attachment_provenance_record",
            None,
        )
        if not callable(reader):
            return "feishu_attachment_provenance_missing"
        try:
            record = reader(self._gateway_event_state_dir, str(provenance_hash))
        except Exception:
            return "feishu_attachment_provenance_mismatch"
        if not isinstance(record, dict):
            return "feishu_attachment_provenance_mismatch"
        failure_class = self._attachment_provenance_record_failure(
            record=record,
            metadata=metadata,
            declared_mime_class=declared_mime_class,
            size_class=(metadata or {}).get("feishu_attachment_size_class"),
            content_hash=(metadata or {}).get("feishu_attachment_content_hash"),
        )
        if failure_class is not None:
            return failure_class
        return "feishu_arbitrary_local_upload_denied"

    def _attachment_provenance_record_failure(
        self,
        *,
        record: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
        declared_mime_class: str,
        size_class: Any,
        content_hash: Any,
    ) -> Optional[str]:
        route_evidence = self._attachment_route_evidence(metadata)
        if route_evidence is None:
            return "feishu_attachment_provenance_mismatch"
        route_hash = route_evidence["route_partition_hash"]
        route_snapshot_hash = route_evidence["route_snapshot_hash"]
        contract_hash = route_evidence["contract_hash"]
        required_matches = (
            (record.get("declared_mime_class"), declared_mime_class),
            (record.get("size_class"), size_class),
            (record.get("route_partition_hash"), route_hash),
            (record.get("route_snapshot_hash"), route_snapshot_hash),
            (record.get("contract_hash"), contract_hash),
            (
                record.get("declared_mime_class"),
                (metadata or {}).get("feishu_attachment_declared_mime_class"),
            ),
            (
                record.get("size_class"),
                (metadata or {}).get("feishu_attachment_size_class"),
            ),
        )
        for actual, expected in required_matches:
            if actual != expected:
                return "feishu_attachment_provenance_mismatch"
        if record.get("sensitivity_state") != "current":
            return "feishu_attachment_provenance_mismatch"
        if record.get("retention_state") != "current":
            return "feishu_attachment_provenance_mismatch"
        if record.get("redaction_state") not in {"redacted", "not_required"}:
            return "feishu_attachment_provenance_mismatch"
        if record.get("provenance_kind") == "generated":
            return self._generated_attachment_provenance_failure(
                record=record,
                metadata=metadata,
                content_hash=content_hash,
            )
        if record.get("provenance_kind") == "inbound_user_attachment":
            return self._inbound_attachment_provenance_failure(
                record=record,
                metadata=metadata,
            )
        return "feishu_attachment_provenance_mismatch"

    def _inbound_attachment_provenance_failure(
        self,
        *,
        record: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        source_event_hash = record.get("source_event_hash")
        file_key_hash = record.get("file_key_hash")
        if not self._is_sha256_ref_text(source_event_hash):
            return "feishu_attachment_provenance_mismatch"
        if not self._is_sha256_ref_text(file_key_hash):
            return "feishu_attachment_provenance_mismatch"
        if record.get("retention_class") not in {
            "ephemeral",
            "session",
            "retained",
        }:
            return "feishu_attachment_provenance_mismatch"
        admission = (metadata or {}).get("feishu_current_admission")
        if not isinstance(admission, dict) or admission.get("evidence_state") != "current":
            return "feishu_attachment_provenance_mismatch"
        if (metadata or {}).get("feishu_attachment_source_event_hash") != source_event_hash:
            return "feishu_attachment_provenance_mismatch"
        if (metadata or {}).get("feishu_attachment_file_key_hash") != file_key_hash:
            return "feishu_attachment_provenance_mismatch"
        return None

    def _attachment_route_evidence(
        self,
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        values = metadata or {}
        admission = values.get("feishu_current_admission")
        if isinstance(admission, dict):
            route_partition_hash = admission.get("route_partition_hash")
            route_snapshot_hash = admission.get("route_snapshot_hash", route_partition_hash)
            contract_hash = admission.get("contract_hash")
        else:
            route_partition_hash = values.get("feishu_attachment_route_partition_hash")
            route_snapshot_hash = values.get(
                "feishu_attachment_route_snapshot_hash",
                route_partition_hash,
            )
            contract_hash = values.get("feishu_attachment_contract_hash")
        if (
            not self._is_sha256_ref_text(route_partition_hash)
            or not self._is_sha256_ref_text(route_snapshot_hash)
            or not self._is_sha256_ref_text(contract_hash)
        ):
            return None
        return {
            "route_partition_hash": str(route_partition_hash),
            "route_snapshot_hash": str(route_snapshot_hash),
            "contract_hash": str(contract_hash),
        }

    def _generated_attachment_provenance_failure(
        self,
        *,
        record: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
        content_hash: str,
    ) -> Optional[str]:
        if record.get("safe_output_root_state", "current") != "current":
            return "feishu_attachment_provenance_mismatch"
        if record.get("source_grant_state", "current") != "current":
            return "feishu_attachment_provenance_mismatch"
        if record.get("generator_state") != "complete":
            return "feishu_attachment_provenance_mismatch"
        if record.get("retention_policy") not in {"ephemeral", "session", "retained"}:
            return "feishu_attachment_provenance_mismatch"
        if record.get("content_hash") != content_hash:
            return "feishu_attachment_provenance_mismatch"
        if record.get("content_hash") != (metadata or {}).get("feishu_attachment_content_hash"):
            return "feishu_attachment_provenance_mismatch"
        if record.get("safe_output_root_proof_hash") != (metadata or {}).get(
            "feishu_attachment_safe_output_root_proof_hash"
        ):
            return "feishu_attachment_provenance_mismatch"
        if record.get("producing_tool_action_hash") != (metadata or {}).get(
            "feishu_attachment_producing_tool_action_hash"
        ):
            return "feishu_attachment_provenance_mismatch"
        source_grants = record.get("source_grant_handles")
        metadata_grants = (metadata or {}).get("feishu_attachment_source_grant_handles")
        if isinstance(metadata_grants, tuple):
            metadata_grants = list(metadata_grants)
        if not isinstance(source_grants, list) or not source_grants:
            return "feishu_attachment_provenance_mismatch"
        if source_grants != metadata_grants:
            return "feishu_attachment_provenance_mismatch"
        return None

    @staticmethod
    def _attachment_size_class(size: int) -> str:
        if size <= 10 * 1024 * 1024:
            return "small"
        if size <= 100 * 1024 * 1024:
            return "medium"
        return "large"

    @staticmethod
    def _sha256_ref_bytes(value: bytes) -> str:
        return f"sha256:{hashlib.sha256(value).hexdigest()}"

    async def _record_attachment_upload_denial(
        self,
        *,
        metadata: Optional[Dict[str, Any]],
        failure_class: str,
        declared_mime_class: str,
    ) -> bool:
        if self._gateway_event_state_dir is None:
            return True
        route_evidence = self._attachment_route_evidence(metadata) or {}
        event = {
            "type": "feishu_attachment_upload_denied",
            "provenance_hash": (
                metadata or {}
            ).get("feishu_attachment_provenance_hash")
            if self._is_sha256_ref_text(
                (metadata or {}).get("feishu_attachment_provenance_hash")
            )
            else self._delivery_ref_hash("feishu_attachment_provenance", "missing"),
            "failure_class": self._stable_failure_class(failure_class),
            "route_partition_hash": route_evidence.get("route_partition_hash")
            if self._is_sha256_ref_text(route_evidence.get("route_partition_hash"))
            else self._delivery_ref_hash("feishu_route_partition", "missing"),
            "contract_hash": route_evidence.get("contract_hash")
            if self._is_sha256_ref_text(route_evidence.get("contract_hash"))
            else self._delivery_ref_hash("feishu_contract", "missing"),
            "declared_mime_class": declared_mime_class
            if declared_mime_class in {"image", "file", "audio", "media", "document"}
            else "file",
            "size_class": (metadata or {}).get("feishu_attachment_size_class")
            if (metadata or {}).get("feishu_attachment_size_class")
            in {"small", "medium", "large"}
            else "small",
            "timestamp": int(time.time()),
        }
        result = await self._apply_gateway_event(event)
        return self._gateway_event_apply_succeeded(result)

    async def _send_uploaded_file_message(
        self,
        *,
        chat_id: str,
        file_path: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        outbound_message_type: str = "file",
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")
        preflight = await self._preflight_attachment_upload(
            chat_id=chat_id,
            file_path=file_path,
            reply_to=reply_to,
            metadata=metadata,
            declared_mime_class=self._attachment_mime_class_for_message_type(
                outbound_message_type
            ),
            upload_kind="file",
        )
        if preflight is not None:
            return preflight
        if not os.path.exists(file_path):
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class=self._attachment_mime_class_for_message_type(
                    outbound_message_type
                ),
                failure_class="feishu_attachment_local_file_missing",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error="File not found")

        display_name = file_name or os.path.basename(file_path)
        upload_file_type, resolved_message_type = self._resolve_outbound_file_routing(
            file_path=display_name,
            requested_message_type=outbound_message_type,
        )
        try:
            with open(file_path, "rb") as file_obj:
                body = self._build_file_upload_body(
                    file_type=upload_file_type,
                    file_name=display_name,
                    file=file_obj,
                )
                request = self._build_file_upload_request(body)
                upload_response = await asyncio.to_thread(self._client.im.v1.file.create, request)
            file_key = self._extract_response_field(upload_response, "file_key")
        except Exception as exc:
            logger.error(
                "[Feishu] Failed to upload file attachment: failure_class=%s",
                exc.__class__.__name__,
            )
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class=self._attachment_mime_class_for_message_type(
                    outbound_message_type
                ),
                failure_class="feishu_file_upload_exception",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error=exc.__class__.__name__)
        if not file_key:
            terminalized = await self._terminalize_attachment_upload_failure(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=metadata,
                declared_mime_class=self._attachment_mime_class_for_message_type(
                    outbound_message_type
                ),
                failure_class="feishu_file_upload_missing_file_key",
            )
            if terminalized is not None:
                return terminalized
            return SendResult(success=False, error="Feishu file upload missing file_key")

        if caption:
            media_tag = {
                "tag": "media",
                "file_key": file_key,
                "file_name": display_name,
            }
            return await self._send_audited_or_legacy_message(
                chat_id=chat_id,
                msg_type="post",
                payload=self._build_media_post_payload(caption=caption, media_tag=media_tag),
                reply_to=reply_to,
                metadata=metadata,
                default_message="file send failed",
            )
        return await self._send_audited_or_legacy_message(
            chat_id=chat_id,
            msg_type=resolved_message_type,
            payload=json.dumps({"file_key": file_key}, ensure_ascii=False),
            reply_to=reply_to,
            metadata=metadata,
            default_message="file send failed",
        )

    async def _send_audited_or_legacy_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        default_message: str,
    ) -> SendResult:
        if self._gateway_event_state_dir is None:
            return self._delivery_audit_state_missing_result()

        operation, operation_error = self._send_delivery_operation(
            reply_to=reply_to,
            metadata=metadata,
        )
        if operation_error is not None:
            return SendResult(success=False, error=operation_error)
        delivery_id = self._delivery_id_for(
            operation,
            metadata=metadata,
            parts=[chat_id, reply_to or "", msg_type, payload],
        )
        attachment_reservation_refs = self._attachment_upload_reservation_refs(
            chat_id=chat_id,
            reply_to=reply_to,
            metadata=metadata,
            delivery_id=delivery_id,
            declared_mime_class=(metadata or {}).get(
                "feishu_attachment_declared_mime_class"
            ),
        )
        use_attachment_reservation_refs = self._is_sha256_ref_text(
            (metadata or {}).get("feishu_attachment_provenance_hash")
        )
        response_or_result = await self._audited_delivery(
            delivery_id=delivery_id,
            operation=operation,
            target=f"feishu:chat:{chat_id}",
            metadata=metadata,
            inbound_id=self._delivery_metadata(metadata, "inbound_id", reply_to or chat_id),
            session_id=self._delivery_metadata(metadata, "session_id", "session"),
            correlation_id=self._delivery_metadata(metadata, "correlation_id", delivery_id),
            ledger_target=attachment_reservation_refs["target"]
            if use_attachment_reservation_refs
            else None,
            ledger_inbound_id=attachment_reservation_refs["inbound_id"]
            if use_attachment_reservation_refs
            else None,
            ledger_session_id=attachment_reservation_refs["session_id"]
            if use_attachment_reservation_refs
            else None,
            ledger_correlation_id=attachment_reservation_refs["correlation_id"]
            if use_attachment_reservation_refs
            else None,
            network_call=lambda uuid_value: self._send_raw_message(
                chat_id=chat_id,
                msg_type=msg_type,
                payload=payload,
                reply_to=reply_to,
                metadata=metadata,
                uuid_value=uuid_value,
            ),
            require_returned_message_id=True,
        )
        if isinstance(response_or_result, SendResult):
            return response_or_result
        return self._finalize_send_result(response_or_result, default_message)

    async def execute_feishu_request_descriptor(
        self,
        descriptor: Dict[str, Any],
        *,
        delivery_id: str,
        inbound_id: str,
        session_id: str,
        correlation_id: str,
    ) -> SendResult:
        """Deny raw Feishu request descriptors before SDK builders can run."""
        if not self._feishu_broker_context_present():
            audited = await self._apply_feishu_legacy_descriptor_denied(
                surface="feishu.descriptor",
                correlation_id=correlation_id,
                descriptor_seed=delivery_id,
            )
            if not audited:
                return SendResult(
                    success=False,
                    error="feishu_legacy_descriptor_denied_apply_failed",
                )
            return SendResult(
                success=False,
                error="feishu_legacy_descriptor_requires_broker",
            )
        if self._gateway_event_state_dir is None:
            logger.warning(
                "[Feishu] Refusing descriptor before SDK delivery: "
                "reason=feishu_legacy_descriptor_audit_state_missing surface=feishu.descriptor"
            )
            return SendResult(
                success=False,
                error="feishu_legacy_descriptor_audit_state_missing",
            )
        validated = self._validate_feishu_request_descriptor(descriptor)
        target = "feishu:descriptor"
        if validated is None:
            pending_result = await self._apply_delivery_pending(
                delivery_id=delivery_id,
                operation="invalid_feishu_request_descriptor",
                inbound_id=inbound_id,
                target=target,
                session_id=session_id,
                correlation_id=correlation_id,
            )
            if self._gateway_event_apply_succeeded(
                pending_result
            ) and not self._delivery_record_apply_is_reusable(
                pending_result,
                delivery_id=delivery_id,
                inbound_id=inbound_id,
                target=target,
                session_id=session_id,
                correlation_id=correlation_id,
            ):
                await self._apply_delivery_failed(
                    delivery_id,
                    "invalid_feishu_request_descriptor",
                )
            return SendResult(
                success=False,
                error="invalid Feishu request descriptor",
            )
        audited = await self._apply_feishu_legacy_descriptor_denied(
            surface="feishu.descriptor",
            failure_class="feishu_raw_descriptor_execution_denied",
            correlation_id=correlation_id,
            descriptor_seed=delivery_id,
        )
        if not audited:
            return SendResult(
                success=False,
                error="feishu_raw_descriptor_denial_apply_failed",
            )
        return SendResult(
            success=False,
            error="feishu_raw_descriptor_execution_denied",
        )

    async def execute_status_card_action(
        self,
        action: Dict[str, Any],
        *,
        delivery_id: str,
        inbound_id: str,
        session_id: str,
        correlation_id: str,
    ) -> SendResult:
        """Execute the Feishu request embedded in a validated status_card action."""
        if not self._feishu_broker_context_present():
            audited = await self._apply_feishu_legacy_descriptor_denied(
                surface="feishu.status_card",
                correlation_id=correlation_id,
                descriptor_seed=delivery_id,
            )
            if not audited:
                return SendResult(
                    success=False,
                    error="feishu_legacy_descriptor_denied_apply_failed",
                )
            return SendResult(
                success=False,
                error="feishu_legacy_descriptor_requires_broker",
            )

        card_action = self._validated_status_card_action_for_send(action)
        if card_action is None:
            return SendResult(success=False, error="invalid status_card action")

        feishu_request = card_action.get("feishu_request")
        if feishu_request is None:
            return SendResult(
                success=True,
                raw_response={
                    "type": "status_card_noop",
                    "reason": card_action["type"],
                },
            )

        return await self.execute_feishu_request_descriptor(
            feishu_request,
            delivery_id=delivery_id,
            inbound_id=inbound_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _sha256_ref_text(value: str) -> str:
        return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    @classmethod
    def _delivery_ref_hash(cls, kind: str, value: Any) -> str:
        return cls._sha256_ref_text(f"{kind}\x1f{value}")

    @classmethod
    def _delivery_lifecycle_correlation_hash(
        cls,
        metadata: Optional[Dict[str, Any]],
        fallback: str,
    ) -> str:
        value = (metadata or {}).get("correlation_id")
        text = str(value if value is not None else fallback).strip()
        return cls._delivery_ref_hash("feishu_lifecycle_correlation", text)

    @staticmethod
    def _is_sha256_ref_text(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 71
            and value.startswith("sha256:")
            and all(char in "0123456789abcdef" for char in value[7:])
        )

    @classmethod
    def _delivery_action_for_operation(cls, operation: str) -> str:
        lowered = str(operation or "").lower()
        return "edit" if "edit" in lowered or "patch" in lowered else "send"

    def _current_delivery_lifecycle_context(
        self,
        *,
        delivery_id: str,
        action: str,
        target: str,
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        admission = (metadata or {}).get("feishu_current_admission")
        if not isinstance(admission, dict):
            return None
        contract_hash = admission.get("contract_hash")
        route_partition_key = admission.get("route_partition_key")
        route_snapshot = admission.get("route_session_key_snapshot")
        if (
            not self._is_sha256_ref_text(contract_hash)
            or not isinstance(route_partition_key, str)
            or not route_partition_key
            or not isinstance(route_snapshot, str)
            or not route_snapshot
        ):
            return None
        evidence_state = str(admission.get("evidence_state") or "current")
        if evidence_state not in {"current", "missing", "stale", "unknown", "denied"}:
            evidence_state = "unknown"
        return {
            "delivery_hash": self._delivery_ref_hash("feishu_delivery", delivery_id),
            "action": action,
            "target_ref_hash": self._delivery_ref_hash("feishu_target", target),
            "route_partition_hash": self._delivery_ref_hash(
                "feishu_route_partition", route_partition_key
            ),
            "route_snapshot_hash": self._delivery_ref_hash(
                "feishu_route_snapshot", route_snapshot
            ),
            "contract_hash": str(contract_hash),
            "evidence_state": evidence_state,
            "correlation_hash": self._delivery_lifecycle_correlation_hash(
                metadata, delivery_id
            ),
        }

    def _minimal_delivery_lifecycle_context(
        self,
        *,
        delivery_id: str,
        action: str,
        target: str,
        metadata: Optional[Dict[str, Any]],
        evidence_state: str,
    ) -> Dict[str, str]:
        return {
            "delivery_hash": self._delivery_ref_hash("feishu_delivery", delivery_id),
            "action": action,
            "target_ref_hash": self._delivery_ref_hash("feishu_target", target),
            "evidence_state": evidence_state,
            "correlation_hash": self._delivery_lifecycle_correlation_hash(
                metadata, delivery_id
            ),
        }

    def _bot_ownership_hash(
        self,
        *,
        delivery_hash: str,
        message_ref_hash: str,
        route_partition_hash: str,
        contract_hash: str,
    ) -> str:
        return self._sha256_ref_text(
            "\x1f".join(
                (
                    "feishu_bot_ownership",
                    delivery_hash,
                    message_ref_hash,
                    route_partition_hash,
                    contract_hash,
                )
            )
        )

    async def _apply_feishu_delivery_lifecycle_event(
        self,
        event_type: str,
        context: Dict[str, str],
        *,
        message_id: Optional[str] = None,
        failure_class: Optional[str] = None,
        original_proof: Optional[Dict[str, Any]] = None,
        bot_ownership_hash: Optional[str] = None,
    ) -> bool:
        event: Dict[str, Any] = {
            "type": event_type,
            "timestamp": int(time.time()),
            "delivery_hash": context["delivery_hash"],
            "action": context["action"],
            "target_ref_hash": context["target_ref_hash"],
            "evidence_state": context["evidence_state"],
            "correlation_hash": context["correlation_hash"],
        }
        for field in ("route_partition_hash", "route_snapshot_hash", "contract_hash"):
            if field in context:
                event[field] = context[field]
        if message_id:
            event["message_ref_hash"] = self._delivery_ref_hash(
                "feishu_message", message_id
            )
        if failure_class is not None:
            event["failure_class"] = self._stable_failure_class(failure_class)
        if original_proof is not None:
            original_delivery_hash = original_proof.get("delivery_hash")
            original_ownership_hash = original_proof.get("bot_ownership_hash")
            if isinstance(original_delivery_hash, str):
                event["original_delivery_hash"] = original_delivery_hash
            if bot_ownership_hash is None and isinstance(original_ownership_hash, str):
                bot_ownership_hash = original_ownership_hash
        if bot_ownership_hash is not None:
            event["bot_ownership_hash"] = bot_ownership_hash
        return bool(await self._apply_gateway_event(event))

    async def _apply_feishu_delivery_lifecycle_failed(
        self,
        *,
        delivery_id: str,
        action: str,
        target: str,
        metadata: Optional[Dict[str, Any]],
        failure_class: str,
        message_id: Optional[str] = None,
        original_proof: Optional[Dict[str, Any]] = None,
    ) -> bool:
        context = self._current_delivery_lifecycle_context(
            delivery_id=delivery_id,
            action=action,
            target=target,
            metadata=metadata,
        )
        if context is None:
            evidence_state = "missing"
            admission = (metadata or {}).get("feishu_current_admission")
            if isinstance(admission, dict) and admission.get("evidence_state") == "stale":
                evidence_state = "stale"
            context = self._minimal_delivery_lifecycle_context(
                delivery_id=delivery_id,
                action=action,
                target=target,
                metadata=metadata,
                evidence_state=evidence_state,
            )
        elif failure_class == "feishu_current_route_evidence_stale":
            context = {**context, "evidence_state": "stale"}
        elif context.get("evidence_state") != "current":
            context = {**context, "evidence_state": context.get("evidence_state", "unknown")}
        return await self._apply_feishu_delivery_lifecycle_event(
            "feishu_delivery_failed",
            context,
            message_id=message_id,
            failure_class=failure_class,
            original_proof=original_proof,
        )

    async def _fail_if_lifecycle_failed_append_missing(
        self,
        *,
        delivery_id: str,
        action: str,
        target: str,
        metadata: Optional[Dict[str, Any]],
        failure_class: str,
        message_id: Optional[str] = None,
        original_proof: Optional[Dict[str, Any]] = None,
    ) -> Optional[SendResult]:
        lifecycle_failed_ok = await self._apply_feishu_delivery_lifecycle_failed(
            delivery_id=delivery_id,
            action=action,
            target=target,
            metadata=metadata,
            failure_class=failure_class,
            message_id=message_id,
            original_proof=original_proof,
        )
        if lifecycle_failed_ok:
            return None
        return SendResult(
            success=False,
            error="feishu_delivery_lifecycle_apply_failed",
        )

    def _current_edit_ownership_proof(
        self,
        *,
        chat_id: str,
        message_id: str,
        metadata: Optional[Dict[str, Any]],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        context = self._current_delivery_lifecycle_context(
            delivery_id=self._delivery_metadata(metadata, "delivery_id", "message_edit"),
            action="edit",
            target=f"feishu:message:{message_id}",
            metadata=metadata,
        )
        admission = (metadata or {}).get("feishu_current_admission")
        if not isinstance(admission, dict):
            return None, "feishu_current_reply_admission_missing"
        if admission.get("evidence_state") == "stale":
            return None, "feishu_current_route_evidence_stale"
        if context is None:
            return None, "feishu_current_reply_admission_incomplete"
        message_ref_hash = self._delivery_ref_hash("feishu_message", message_id)
        proof = self._bot_owned_delivery_proof(message_ref_hash)
        if proof is None:
            return None, "feishu_delivery_bot_ownership_missing"
        expected_chat_ref = self._delivery_ref_hash(
            "feishu_target", f"feishu:chat:{chat_id}"
        )
        if proof.get("target_ref_hash") != expected_chat_ref:
            return proof, "feishu_current_route_mismatch"
        for field in ("route_partition_hash", "route_snapshot_hash", "contract_hash"):
            if proof.get(field) != context.get(field):
                return proof, "feishu_current_route_mismatch"
        return proof, None

    def _bot_owned_delivery_proof(
        self,
        message_ref_hash: str,
    ) -> Optional[Dict[str, Any]]:
        state_dir = self._gateway_event_state_dir
        reader = getattr(
            gateway_event_ledger,
            "feishu_delivery_lifecycle_events_for_readiness",
            None,
        )
        if state_dir is None or not callable(reader):
            return None
        try:
            events = reader(state_dir)
        except Exception:
            return None
        for event in reversed(events):
            if (
                isinstance(event, dict)
                and event.get("type") == "feishu_delivery_sent"
                and event.get("action") == "send"
                and event.get("message_ref_hash") == message_ref_hash
                and event.get("evidence_state") == "current"
                and isinstance(event.get("bot_ownership_hash"), str)
            ):
                return dict(event)
        return None

    @staticmethod
    def _validated_status_card_action_for_send(
        action: Any,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(action, dict):
            return None
        if action.get("type") == "status_card":
            validated = hermes_tools_gateway_event._validated_action(action)
        else:
            validated = hermes_tools_gateway_event._validated_action(
                {"type": "status_card", "card_action": action}
            )
        if not isinstance(validated, dict) or validated.get("type") != "status_card":
            return None
        card_action = validated.get("card_action")
        return card_action if isinstance(card_action, dict) else None

    async def _audited_delivery(
        self,
        *,
        delivery_id: str,
        operation: str,
        target: str,
        inbound_id: str,
        session_id: str,
        correlation_id: str,
        network_call: Any,
        require_returned_message_id: bool,
        metadata: Optional[Dict[str, Any]] = None,
        existing_message_id: Optional[str] = None,
        current_delivery_proof: Optional[Dict[str, Any]] = None,
        terminal_exception_matcher: Any = None,
        ledger_target: Optional[str] = None,
        ledger_inbound_id: Optional[str] = None,
        ledger_session_id: Optional[str] = None,
        ledger_correlation_id: Optional[str] = None,
        ledger_message_id: Optional[str] = None,
    ) -> Any | SendResult:
        delivery_record_target = ledger_target or target
        delivery_record_inbound_id = ledger_inbound_id or inbound_id
        delivery_record_session_id = ledger_session_id or session_id
        delivery_record_correlation_id = ledger_correlation_id or correlation_id
        pending_result = await self._apply_delivery_pending(
            delivery_id=delivery_id,
            operation=operation,
            inbound_id=delivery_record_inbound_id,
            target=delivery_record_target,
            session_id=delivery_record_session_id,
            correlation_id=delivery_record_correlation_id,
        )
        replay_result = self._send_result_from_delivery_record_apply(
            pending_result,
            delivery_id=delivery_id,
            inbound_id=delivery_record_inbound_id,
            target=delivery_record_target,
            session_id=delivery_record_session_id,
            correlation_id=delivery_record_correlation_id,
        )
        if replay_result is not None:
            return replay_result
        if not self._gateway_event_apply_succeeded(pending_result):
            return SendResult(
                success=False,
                error="delivery_pending apply failed",
            )

        lifecycle_action = self._delivery_action_for_operation(operation)
        lifecycle_context = self._current_delivery_lifecycle_context(
            delivery_id=delivery_id,
            action=lifecycle_action,
            target=target,
            metadata=metadata,
        )
        if lifecycle_context is not None:
            if lifecycle_context.get("evidence_state") != "current":
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="feishu_current_route_evidence_stale",
                    message_id=existing_message_id,
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
                return SendResult(
                    success=False,
                    error="feishu_current_route_evidence_stale",
                )
            attempted_ok = await self._apply_feishu_delivery_lifecycle_event(
                "feishu_delivery_attempted",
                lifecycle_context,
                message_id=existing_message_id,
                original_proof=current_delivery_proof,
            )
            if not attempted_ok:
                return SendResult(
                    success=False,
                    error="feishu_delivery_lifecycle_apply_failed",
                )

        try:
            response = await network_call(self._idempotency_key_for_delivery(delivery_id))
        except Exception as exc:
            if terminal_exception_matcher is not None and terminal_exception_matcher(exc):
                if not await self._apply_delivery_failed(
                    delivery_id,
                    "feishu_terminal_non_acceptance",
                ):
                    return SendResult(
                        success=False,
                        error="delivery_failed apply failed",
                    )
                if lifecycle_context is not None:
                    lifecycle_failed_ok = await self._apply_feishu_delivery_lifecycle_failed(
                        delivery_id=delivery_id,
                        action=lifecycle_action,
                        target=target,
                        metadata=metadata,
                        failure_class="feishu_terminal_non_acceptance",
                        message_id=existing_message_id,
                        original_proof=current_delivery_proof,
                    )
                    if not lifecycle_failed_ok:
                        return SendResult(
                            success=False,
                            error="feishu_delivery_lifecycle_apply_failed",
                        )
                return SendResult(
                    success=False,
                    error="content format of the post type is incorrect",
                )
            if lifecycle_context is not None:
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="sdk_exception_after_admission",
                    message_id=existing_message_id,
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
            if not await self._apply_unknown_delivery_state(
                delivery_id,
                "sdk_exception_after_admission",
                message_id=existing_message_id,
            ):
                return SendResult(
                    success=False,
                    error="unknown_delivery_state apply failed",
                )
            return SendResult(success=False, error=exc.__class__.__name__)

        if self._response_is_ambiguous_non_acceptance(response):
            if lifecycle_context is not None:
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="retryable_non_acceptance_after_admission",
                    message_id=existing_message_id,
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
            if not await self._apply_unknown_delivery_state(
                delivery_id,
                "retryable_non_acceptance_after_admission",
                message_id=existing_message_id,
            ):
                return SendResult(
                    success=False,
                    error="unknown_delivery_state apply failed",
                    raw_response=response,
                )
            return self._response_error_result(
                response,
                default_message=f"{operation} ambiguous",
                override_error="retryable_non_acceptance_after_admission",
            )

        if not self._response_succeeded(response):
            if not await self._apply_delivery_failed(
                delivery_id,
                "feishu_terminal_non_acceptance",
            ):
                return SendResult(
                    success=False,
                    error="delivery_failed apply failed",
                )
            if lifecycle_context is not None:
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="feishu_terminal_non_acceptance",
                    message_id=existing_message_id,
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
            return self._response_error_result(
                response,
                default_message=f"{operation} failed",
            )

        message_id = (
            self._extract_response_field(response, "message_id")
            if require_returned_message_id
            else existing_message_id
        )
        if not message_id or not self._valid_feishu_message_id(str(message_id)):
            if lifecycle_context is not None:
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="missing_message_id_after_sdk_success",
                    message_id=existing_message_id,
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
            if not await self._apply_unknown_delivery_state(
                delivery_id,
                "missing_message_id_after_sdk_success",
                message_id=existing_message_id,
            ):
                return SendResult(
                    success=False,
                    error="unknown_delivery_state apply failed",
                    raw_response=response,
                )
            return SendResult(
                success=False,
                error="Feishu SDK success missing message_id",
                raw_response=response,
            )

        if ledger_message_id:
            delivery_record_message_id = ledger_message_id
        elif self._is_sha256_ref_text(
            (metadata or {}).get("feishu_attachment_provenance_hash")
        ):
            delivery_record_message_id = self._attachment_upload_ledger_message_id(
                str(message_id)
            )
        else:
            delivery_record_message_id = str(message_id)
        if not await self._apply_delivery_sent(
            delivery_id,
            delivery_record_message_id,
            operation,
        ):
            if lifecycle_context is not None:
                lifecycle_gap_result = await self._fail_if_lifecycle_failed_append_missing(
                    delivery_id=delivery_id,
                    action=lifecycle_action,
                    target=target,
                    metadata=metadata,
                    failure_class="delivery_sent_apply_failed",
                    message_id=str(message_id),
                    original_proof=current_delivery_proof,
                )
                if lifecycle_gap_result is not None:
                    return lifecycle_gap_result
            if not await self._apply_unknown_delivery_state(
                delivery_id,
                "delivery_sent_apply_failed",
                message_id=delivery_record_message_id,
            ):
                return SendResult(
                    success=False,
                    error="unknown_delivery_state apply failed",
                    raw_response=response,
                )
            return SendResult(
                success=False,
                error="delivery_sent apply failed",
                raw_response=response,
            )

        if lifecycle_context is not None:
            message_ref_hash = self._delivery_ref_hash("feishu_message", str(message_id))
            bot_ownership_hash = None
            if lifecycle_action == "send":
                bot_ownership_hash = self._bot_ownership_hash(
                    delivery_hash=lifecycle_context["delivery_hash"],
                    message_ref_hash=message_ref_hash,
                    route_partition_hash=lifecycle_context["route_partition_hash"],
                    contract_hash=lifecycle_context["contract_hash"],
                )
            sent_ok = await self._apply_feishu_delivery_lifecycle_event(
                "feishu_delivery_sent",
                lifecycle_context,
                message_id=str(message_id),
                original_proof=current_delivery_proof,
                bot_ownership_hash=bot_ownership_hash,
            )
            if not sent_ok:
                return SendResult(
                    success=False,
                    error="feishu_delivery_lifecycle_apply_failed",
                    raw_response=response,
                )
            ack_unknown_ok = await self._apply_feishu_delivery_lifecycle_event(
                "feishu_delivery_ack_unknown",
                lifecycle_context,
                message_id=str(message_id),
                failure_class="feishu_delivery_ack_unobservable",
                original_proof=current_delivery_proof,
                bot_ownership_hash=bot_ownership_hash,
            )
            if not ack_unknown_ok:
                return SendResult(
                    success=False,
                    error="feishu_delivery_ack_unknown_apply_failed",
                    raw_response=response,
                )

        return response

    async def _apply_delivery_pending(
        self,
        *,
        delivery_id: str,
        operation: str,
        inbound_id: str,
        target: str,
        session_id: str,
        correlation_id: str,
    ) -> Any:
        return await self._apply_gateway_event(
            {
                "type": "delivery_pending",
                "delivery_id": delivery_id,
                "operation": operation,
                "inbound_id": inbound_id,
                "target": target,
                "session_id": session_id,
                "correlation_id": correlation_id,
                "timestamp": int(time.time()),
            }
        )

    @staticmethod
    def _gateway_event_apply_succeeded(result: Any) -> bool:
        if isinstance(result, bool):
            return result
        return bool(getattr(result, "ok", False))

    @staticmethod
    def _delivery_record_action_from_pending_apply(
        result: Any,
    ) -> Optional[Dict[str, Any]]:
        action = getattr(result, "action", None)
        if not isinstance(action, dict) or action.get("type") != "delivery_record":
            return None
        return action

    @classmethod
    def _delivery_record_from_pending_apply(
        cls,
        result: Any,
    ) -> Optional[Dict[str, Any]]:
        action = cls._delivery_record_action_from_pending_apply(result)
        if action is None:
            return None
        record = action.get("record")
        return record if isinstance(record, dict) else None

    def _delivery_record_apply_is_reusable(
        self,
        result: Any,
        *,
        delivery_id: str,
        inbound_id: str,
        target: str,
        session_id: str,
        correlation_id: str,
    ) -> bool:
        record = self._delivery_record_from_pending_apply(result)
        if record is None:
            return False
        message_id = record.get("feishu_message_id")
        return (
            self._delivery_record_identity_matches(
                record,
                delivery_id=delivery_id,
                inbound_id=inbound_id,
                target=target,
                session_id=session_id,
                correlation_id=correlation_id,
            )
            and record.get("status") in {"sent", "acked"}
            and isinstance(message_id, str)
            and (
                self._valid_feishu_message_id(message_id)
                or self._is_sha256_ref_text(message_id)
                or self._is_attachment_upload_ledger_message_id(message_id)
            )
        )

    @classmethod
    def _delivery_identity_ref(cls, field: str, value: str) -> str:
        return cls._delivery_ref_hash(f"delivery_identity.{field}", value)

    @classmethod
    def _delivery_record_identity_matches(
        cls,
        record: Dict[str, Any],
        *,
        delivery_id: str,
        inbound_id: str,
        target: str,
        session_id: str,
        correlation_id: str,
    ) -> bool:
        projected = {
            "inbound_id": cls._delivery_identity_ref("inbound_id", inbound_id),
            "target": cls._delivery_identity_ref("target", target),
            "session_id": cls._delivery_identity_ref("session_id", session_id),
            "correlation_id": cls._delivery_identity_ref(
                "correlation_id", correlation_id
            ),
        }
        return (
            record.get("delivery_id") == delivery_id
            and record.get("inbound_id") == projected["inbound_id"]
            and record.get("target") == projected["target"]
            and record.get("session_id") == projected["session_id"]
            and record.get("correlation_id") == projected["correlation_id"]
        )

    @staticmethod
    def _delivery_record_message_id_matches_target(
        *,
        message_id: str,
        target: str,
    ) -> bool:
        message_target_prefix = "feishu:message:"
        if not target.startswith(message_target_prefix):
            return True
        return message_id == target[len(message_target_prefix):]

    def _delivery_record_replay_message_id(
        self,
        record: Dict[str, Any],
        *,
        target: str,
    ) -> Optional[str]:
        message_id = record.get("feishu_message_id")
        message_id_text = str(message_id or "")
        if self._is_sha256_ref_text(message_id_text):
            return None
        if self._is_attachment_upload_ledger_message_id(message_id_text):
            return None
        if (
            message_id
            and self._valid_feishu_message_id(message_id_text)
            and self._delivery_record_message_id_matches_target(
                message_id=message_id_text,
                target=target,
            )
        ):
            return message_id_text
        if message_id:
            return None

        message_target_prefix = "feishu:message:"
        if target.startswith(message_target_prefix):
            target_message_id = target[len(message_target_prefix):]
            if self._valid_feishu_message_id(target_message_id):
                return target_message_id
        return None

    def _send_result_from_delivery_record_apply(
        self,
        result: Any,
        *,
        delivery_id: str,
        inbound_id: str,
        target: str,
        session_id: str,
        correlation_id: str,
    ) -> Optional[SendResult]:
        action = getattr(result, "action", None)
        if action is None:
            return None
        if not isinstance(action, dict) or action.get("type") != "delivery_record":
            return SendResult(success=False, error="delivery_pending apply failed")
        record = self._delivery_record_from_pending_apply(result)
        if record is None:
            return SendResult(success=False, error="delivery_pending apply failed")

        message_id = record.get("feishu_message_id")
        identity_matches = self._delivery_record_identity_matches(
            record,
            delivery_id=delivery_id,
            inbound_id=inbound_id,
            target=target,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        if (
            identity_matches
            and record.get("status") == "pending"
            and not record.get("feishu_message_id")
        ):
            return None
        if identity_matches and record.get("status") in {"sent", "acked"}:
            message_id_text = self._delivery_record_replay_message_id(
                record,
                target=target,
            )
            if message_id_text is None and self._is_attachment_upload_ledger_message_id(
                str(message_id or "")
            ):
                return SendResult(
                    success=True,
                    message_id=None,
                    raw_response={"type": "delivery_record", "record": record},
                )
            if message_id_text is None and self._is_sha256_ref_text(
                str(message_id or "")
            ):
                return SendResult(
                    success=True,
                    message_id=None,
                    raw_response={"type": "delivery_record", "record": record},
                )
            if message_id_text is not None:
                return SendResult(
                    success=True,
                    message_id=message_id_text,
                    raw_response={"type": "delivery_record", "record": record},
                )

        return SendResult(success=False, error="delivery_pending apply failed")

    async def _apply_delivery_sent(
        self,
        delivery_id: str,
        message_id: str,
        operation: str,
    ) -> bool:
        return await self._apply_gateway_event(
            {
                "type": "delivery_sent",
                "delivery_id": delivery_id,
                "operation": operation,
                "message_id": message_id,
                "timestamp": int(time.time()),
            }
        )

    async def _apply_delivery_failed(
        self,
        delivery_id: str,
        failure_class: str,
    ) -> bool:
        return await self._apply_gateway_event(
            {
                "type": "delivery_failed",
                "delivery_id": delivery_id,
                "failure_class": self._stable_failure_class(failure_class),
                "timestamp": int(time.time()),
            }
        )

    async def _apply_unknown_delivery_state(
        self,
        delivery_id: str,
        failure_class: str,
        *,
        message_id: Optional[str] = None,
    ) -> bool:
        event = {
            "type": "unknown_delivery_state",
            "delivery_id": delivery_id,
            "failure_class": self._stable_failure_class(failure_class),
            "timestamp": int(time.time()),
        }
        if message_id:
            event["message_id"] = str(message_id)
        return await self._apply_gateway_event(event)

    @classmethod
    def _stable_failure_class(cls, value: str) -> str:
        return value if _DELIVERY_FAILURE_CLASS_RE.fullmatch(value) else "feishu_delivery_error"

    @staticmethod
    def _idempotency_key_for_delivery(delivery_id: str) -> str:
        delivery_text = str(delivery_id or "")
        if re.fullmatch(r"^[A-Za-z0-9_.:-]{1,50}$", delivery_text):
            return delivery_text
        digest = hashlib.sha256(delivery_text.encode("utf-8")).hexdigest()[:40]
        return f"hgw-{digest}"

    @staticmethod
    def _delivery_metadata(
        metadata: Optional[Dict[str, Any]],
        key: str,
        fallback: str,
    ) -> str:
        value = (metadata or {}).get(key)
        text = str(value if value is not None else fallback).strip()
        if re.fullmatch(r"^[A-Za-z0-9_.:-]{1,127}$", text):
            return text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"{key}-{digest}"

    @classmethod
    def _send_delivery_operation(
        cls,
        *,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> tuple[str, Optional[str]]:
        if metadata and cls._DELIVERY_OPERATION_HINT_KEY in metadata:
            hint = metadata.get(cls._DELIVERY_OPERATION_HINT_KEY)
            if isinstance(hint, str) and hint in cls._SEND_DELIVERY_OPERATION_HINTS:
                return hint, None
            return "", "unsupported delivery operation hint"
        return ("reply" if reply_to else "normal_final_reply"), None

    def _delivery_id_for(
        self,
        operation: str,
        *,
        metadata: Optional[Dict[str, Any]],
        parts: Sequence[str],
    ) -> str:
        explicit = (metadata or {}).get("delivery_id")
        if explicit is not None:
            explicit_text = str(explicit).strip()
            if re.fullmatch(r"^[A-Za-z0-9_.:-]{1,127}$", explicit_text):
                if operation in self._SEND_DELIVERY_OPERATION_HINTS:
                    digest = hashlib.sha256(
                        f"{operation}\x1f{explicit_text}".encode("utf-8")
                    ).hexdigest()[:24]
                    return f"{operation}-{digest}"
                return explicit_text
        seed = "\x1f".join([operation, *(str(part) for part in parts)])
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return f"{operation}-{digest}"

    def _metadata_for_send_chunk(
        self,
        *,
        operation: str,
        metadata: Optional[Dict[str, Any]],
        chunk_index: int,
        chunk_total: int,
    ) -> Optional[Dict[str, Any]]:
        if (
            operation != "queued_followup_first_reply"
            or chunk_total <= 1
            or metadata is None
        ):
            return metadata
        explicit = metadata.get("delivery_id")
        if explicit is None:
            return metadata
        explicit_text = str(explicit).strip()
        if not re.fullmatch(r"^[A-Za-z0-9_.:-]{1,127}$", explicit_text):
            return metadata
        chunk_metadata = dict(metadata)
        chunk_metadata["delivery_id"] = self._derived_delivery_id(
            explicit_text,
            f"queued_followup_first_reply_chunk_{chunk_index + 1}_of_{chunk_total}",
        )
        return chunk_metadata

    @staticmethod
    def _valid_feishu_message_id(message_id: str) -> bool:
        return bool(_FEISHU_MESSAGE_ID_RE.fullmatch(str(message_id or "")))

    def _validate_feishu_request_descriptor(
        self,
        descriptor: Any,
    ) -> Optional[Dict[str, Any]]:
        validated = hermes_tools_gateway_event._validated_feishu_request(descriptor)
        if not isinstance(validated, dict):
            return None
        operation = validated.get("operation")
        if operation == "send_interactive_message":
            return validated
        if operation == "patch_interactive_message":
            path = validated.get("path")
            path_match = (
                _FEISHU_DESCRIPTOR_PATCH_PATH_RE.fullmatch(path)
                if isinstance(path, str)
                else None
            )
            if path_match is None:
                return None
            message_id = path_match.group(1)
            if not self._valid_feishu_message_id(message_id):
                return None
            return {**validated, "message_id": message_id}
        return None

    @staticmethod
    def _is_post_content_invalid_exception(exc: BaseException) -> bool:
        return bool(_POST_CONTENT_INVALID_RE.search(str(exc)))

    @staticmethod
    def _is_post_content_invalid_result(result: SendResult) -> bool:
        return bool(result.error and _POST_CONTENT_INVALID_RE.search(result.error))

    @staticmethod
    def _derived_delivery_id(delivery_id: str, purpose: str) -> str:
        digest = hashlib.sha256(
            f"{purpose}\x1f{delivery_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"{purpose}-{digest}"

    @staticmethod
    def _response_is_ambiguous_non_acceptance(response: Any) -> bool:
        if FeishuAdapter._response_succeeded(response):
            return False
        if bool(getattr(response, "retryable", False)):
            return True
        code = getattr(response, "code", None)
        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            return False
        return numeric_code in _FEISHU_AMBIGUOUS_NON_ACCEPTANCE_CODES

    async def _send_raw_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        uuid_value: Optional[str] = None,
        allow_admitted_reply_fallback: bool = False,
    ) -> Any:
        self._validate_current_reply_admission_metadata(
            reply_to=reply_to,
            metadata=metadata,
            allow_admitted_reply_fallback=allow_admitted_reply_fallback,
        )
        stable_uuid = uuid_value or str(uuid.uuid4())
        effective_reply_to = reply_to
        if not effective_reply_to and metadata and metadata.get("thread_id"):
            effective_reply_to = metadata.get("reply_to_message_id")
        reply_in_thread = bool((metadata or {}).get("thread_id"))
        if effective_reply_to:
            body = self._build_reply_message_body(
                content=payload,
                msg_type=msg_type,
                reply_in_thread=reply_in_thread,
                uuid_value=stable_uuid,
            )
            request = self._build_reply_message_request(effective_reply_to, body)
            return await asyncio.to_thread(self._client.im.v1.message.reply, request)

        body = self._build_create_message_body(
            receive_id=chat_id,
            msg_type=msg_type,
            content=payload,
            uuid_value=stable_uuid,
        )
        # Detect whether chat_id is a user open_id (DM) or a chat_id (group).
        if chat_id.startswith("ou_"):
            receive_id_type = "open_id"
        else:
            receive_id_type = "chat_id"
        request = self._build_create_message_request(receive_id_type, body)
        return await asyncio.to_thread(self._client.im.v1.message.create, request)

    @staticmethod
    def _validate_current_reply_admission_metadata(
        *,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        allow_admitted_reply_fallback: bool = False,
    ) -> None:
        if not metadata:
            return
        admission = metadata.get("feishu_current_admission")
        if admission is None:
            return
        if not isinstance(admission, dict):
            raise ValueError("feishu_current_reply_admission_invalid")
        required = {
            "contract_hash",
            "route_partition_key",
            "route_session_key_snapshot",
            "actor_ref",
            "authority_subject_ref",
            "transport_kind",
            "reply_anchor_ref",
        }
        if not required.issubset(admission):
            raise ValueError("feishu_current_reply_admission_incomplete")
        if admission.get("transport_kind") == "thread" and not {
            "thread_anchor_ref",
            "explicit_thread_reply_anchor_ref",
        }.issubset(admission):
            raise ValueError("feishu_current_reply_admission_incomplete")
        if not reply_to:
            if allow_admitted_reply_fallback:
                return
            raise ValueError("feishu_current_reply_anchor_missing")
        reply_ref = feishu_hashed_ref("feishu_reply_anchor", reply_to)
        if reply_ref is None or admission.get("reply_anchor_ref") != reply_ref.value_hash:
            raise ValueError("feishu_current_reply_anchor_mismatch")

    @staticmethod
    def _response_succeeded(response: Any) -> bool:
        return bool(response and getattr(response, "success", lambda: False)())

    @staticmethod
    def _extract_response_field(response: Any, field_name: str) -> Any:
        if not FeishuAdapter._response_succeeded(response):
            return None
        data = getattr(response, "data", None)
        return getattr(data, field_name, None) if data else None

    def _response_error_result(
        self,
        response: Any,
        *,
        default_message: str,
        override_error: Optional[str] = None,
    ) -> SendResult:
        if override_error:
            return SendResult(success=False, error=override_error, raw_response=response)
        code = getattr(response, "code", "unknown")
        msg = getattr(response, "msg", default_message)
        return SendResult(success=False, error=f"[{code}] {msg}", raw_response=response)

    def _finalize_send_result(self, response: Any, default_message: str) -> SendResult:
        if not self._response_succeeded(response):
            return self._response_error_result(response, default_message=default_message)
        return SendResult(
            success=True,
            message_id=self._extract_response_field(response, "message_id"),
            raw_response=response,
        )

    # =========================================================================
    # Connection internals — websocket / webhook setup
    # =========================================================================

    async def _connect_with_retry(self) -> None:
        for attempt in range(_FEISHU_CONNECT_ATTEMPTS):
            try:
                if self._connection_mode == "websocket":
                    await self._connect_websocket()
                else:
                    await self._connect_webhook()
                return
            except Exception as exc:
                self._running = False
                self._disable_websocket_auto_reconnect()
                self._ws_future = None
                await self._stop_webhook_server()
                if attempt >= _FEISHU_CONNECT_ATTEMPTS - 1:
                    raise
                wait_seconds = 2 ** attempt
                logger.warning(
                    "[Feishu] Connect attempt %d/%d failed; retrying in %ds: %s",
                    attempt + 1,
                    _FEISHU_CONNECT_ATTEMPTS,
                    wait_seconds,
                    exc,
                )
                await asyncio.sleep(wait_seconds)

    async def _connect_websocket(self) -> None:
        if not FEISHU_WEBSOCKET_AVAILABLE:
            raise RuntimeError("websockets not installed; websocket mode unavailable")
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("failed to build Feishu event handler")
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("adapter loop is not ready")
        await self._hydrate_bot_identity()
        self._ws_client = FeishuWSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self._event_handler,
            domain=domain,
        )
        self._ws_future = loop.run_in_executor(
            None,
            _run_official_feishu_ws_client,
            self._ws_client,
            self,
        )

    async def _connect_webhook(self) -> None:
        if not FEISHU_WEBHOOK_AVAILABLE:
            raise RuntimeError("aiohttp not installed; webhook mode unavailable")
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("failed to build Feishu event handler")
        await self._hydrate_bot_identity()
        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook_request)
        self._webhook_runner = web.AppRunner(app)
        await self._webhook_runner.setup()
        self._webhook_site = web.TCPSite(self._webhook_runner, self._webhook_host, self._webhook_port)
        await self._webhook_site.start()

    def _build_lark_client(self, domain: Any) -> Any:
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    async def _feishu_send_with_retry(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> Any:
        last_error: Optional[Exception] = None
        active_reply_to = reply_to
        for attempt in range(_FEISHU_SEND_ATTEMPTS):
            try:
                response = await self._send_raw_message(
                    chat_id=chat_id,
                    msg_type=msg_type,
                    payload=payload,
                    reply_to=active_reply_to,
                    metadata=metadata,
                )
                # If replying to a message failed because it was withdrawn or not found,
                # fall back to posting a new message directly to the chat.
                if active_reply_to and not self._response_succeeded(response):
                    code = getattr(response, "code", None)
                    if code in _FEISHU_REPLY_FALLBACK_CODES:
                        if (metadata or {}).get("thread_id"):
                            logger.warning(
                                "[Feishu] Reply to %s failed in thread %s (code %s — message withdrawn/missing); "
                                "skipping top-level fallback to avoid creating a new topic",
                                active_reply_to,
                                (metadata or {}).get("thread_id"),
                                code,
                            )
                            return response
                        logger.warning(
                            "[Feishu] Reply to %s failed (code %s — message withdrawn/missing); "
                            "falling back to new message in chat %s",
                            active_reply_to,
                            code,
                            chat_id,
                        )
                        active_reply_to = None
                        response = await self._send_raw_message(
                            chat_id=chat_id,
                            msg_type=msg_type,
                            payload=payload,
                            reply_to=None,
                            metadata=metadata,
                            allow_admitted_reply_fallback=True,
                        )
                return response
            except Exception as exc:
                last_error = exc
                if msg_type == "post" and _POST_CONTENT_INVALID_RE.search(str(exc)):
                    raise
                if attempt >= _FEISHU_SEND_ATTEMPTS - 1:
                    raise
                wait_seconds = 2 ** attempt
                logger.warning(
                    "[Feishu] Send attempt %d/%d failed for chat %s; retrying in %ds: %s",
                    attempt + 1,
                    _FEISHU_SEND_ATTEMPTS,
                    chat_id,
                    wait_seconds,
                    exc,
                )
                await asyncio.sleep(wait_seconds)
        raise last_error or RuntimeError("Feishu send failed")

    async def _release_app_lock(self) -> None:
        if not self._app_lock_identity:
            return
        try:
            release_scoped_lock(_FEISHU_APP_LOCK_SCOPE, self._app_lock_identity)
        except Exception as exc:
            logger.warning("[Feishu] Failed to release app lock: %s", exc, exc_info=True)
        finally:
            self._app_lock_identity = None

    # =========================================================================
    # Lark API request builders
    # =========================================================================

    @staticmethod
    def _build_get_chat_request(chat_id: str) -> Any:
        if "GetChatRequest" in globals():
            return GetChatRequest.builder().chat_id(chat_id).build()
        return SimpleNamespace(chat_id=chat_id)

    @staticmethod
    def _build_get_message_request(message_id: str) -> Any:
        if "GetMessageRequest" in globals():
            return GetMessageRequest.builder().message_id(message_id).build()
        return SimpleNamespace(message_id=message_id)

    @staticmethod
    def _build_message_resource_request(*, message_id: str, file_key: str, resource_type: str) -> Any:
        if "GetMessageResourceRequest" in globals():
            return (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
        return SimpleNamespace(message_id=message_id, file_key=file_key, type=resource_type)

    @staticmethod
    def _build_get_application_request(*, app_id: str, lang: str) -> Any:
        if "GetApplicationRequest" in globals():
            return (
                GetApplicationRequest.builder()
                .app_id(app_id)
                .lang(lang)
                .build()
            )
        return SimpleNamespace(app_id=app_id, lang=lang)

    @staticmethod
    def _build_reply_message_body(*, content: str, msg_type: str, reply_in_thread: bool, uuid_value: str) -> Any:
        if "ReplyMessageRequestBody" in globals():
            return (
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(msg_type)
                .reply_in_thread(reply_in_thread)
                .uuid(uuid_value)
                .build()
            )
        return SimpleNamespace(
            content=content,
            msg_type=msg_type,
            reply_in_thread=reply_in_thread,
            uuid=uuid_value,
        )

    @staticmethod
    def _build_reply_message_request(message_id: str, request_body: Any) -> Any:
        if "ReplyMessageRequest" in globals():
            return (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(request_body)
                .build()
            )
        return SimpleNamespace(message_id=message_id, request_body=request_body)

    @staticmethod
    def _build_update_message_body(*, msg_type: str, content: str) -> Any:
        if "UpdateMessageRequestBody" in globals():
            return (
                UpdateMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
        return SimpleNamespace(msg_type=msg_type, content=content)

    @staticmethod
    def _build_update_message_request(message_id: str, request_body: Any) -> Any:
        if "UpdateMessageRequest" in globals():
            return (
                UpdateMessageRequest.builder()
                .message_id(message_id)
                .request_body(request_body)
                .build()
            )
        return SimpleNamespace(message_id=message_id, request_body=request_body)

    @staticmethod
    def _build_create_message_body(*, receive_id: str, msg_type: str, content: str, uuid_value: str) -> Any:
        if "CreateMessageRequestBody" in globals():
            return (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(uuid_value)
                .build()
            )
        return SimpleNamespace(
            receive_id=receive_id,
            msg_type=msg_type,
            content=content,
            uuid=uuid_value,
        )

    @staticmethod
    def _build_create_message_request(receive_id_type: str, request_body: Any) -> Any:
        if "CreateMessageRequest" in globals():
            return (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(request_body)
                .build()
            )
        return SimpleNamespace(receive_id_type=receive_id_type, request_body=request_body)

    @staticmethod
    def _build_image_upload_body(*, image_type: str, image: Any) -> Any:
        if "CreateImageRequestBody" in globals():
            return (
                CreateImageRequestBody.builder()
                .image_type(image_type)
                .image(image)
                .build()
            )
        return SimpleNamespace(image_type=image_type, image=image)

    @staticmethod
    def _build_image_upload_request(request_body: Any) -> Any:
        if "CreateImageRequest" in globals():
            return CreateImageRequest.builder().request_body(request_body).build()
        return SimpleNamespace(request_body=request_body)

    @staticmethod
    def _build_file_upload_body(*, file_type: str, file_name: str, file: Any) -> Any:
        if "CreateFileRequestBody" in globals():
            return (
                CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(file_name)
                .file(file)
                .build()
            )
        return SimpleNamespace(file_type=file_type, file_name=file_name, file=file)

    @staticmethod
    def _build_file_upload_request(request_body: Any) -> Any:
        if "CreateFileRequest" in globals():
            return CreateFileRequest.builder().request_body(request_body).build()
        return SimpleNamespace(request_body=request_body)

    def _build_post_payload(self, content: str) -> str:
        return _build_markdown_post_payload(content)

    def _build_media_post_payload(self, *, caption: str, media_tag: Dict[str, str]) -> str:
        payload = json.loads(self._build_post_payload(caption))
        content = payload.setdefault("zh_cn", {}).setdefault("content", [])
        content.append([media_tag])
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _resolve_outbound_file_routing(
        *,
        file_path: str,
        requested_message_type: str,
    ) -> tuple[str, str]:
        ext = Path(file_path).suffix.lower()

        if ext in _FEISHU_OPUS_UPLOAD_EXTENSIONS:
            return "opus", "audio"

        if ext in _FEISHU_MEDIA_UPLOAD_EXTENSIONS:
            return "mp4", "media"

        if ext in _FEISHU_DOC_UPLOAD_TYPES:
            return _FEISHU_DOC_UPLOAD_TYPES[ext], "file"

        if requested_message_type == "file":
            return _FEISHU_FILE_UPLOAD_TYPE, "file"

        return _FEISHU_FILE_UPLOAD_TYPE, "file"


# =============================================================================
# QR scan-to-create onboarding
#
# Device-code flow: user scans a QR code with Feishu/Lark mobile app and the
# platform creates a fully configured bot application automatically.
# Called by `hermes gateway setup` via _setup_feishu() in hermes_cli/gateway.py.
# =============================================================================


def _accounts_base_url(domain: str) -> str:
    return _ONBOARD_ACCOUNTS_URLS.get(domain, _ONBOARD_ACCOUNTS_URLS["feishu"])


def _onboard_open_base_url(domain: str) -> str:
    return _ONBOARD_OPEN_URLS.get(domain, _ONBOARD_OPEN_URLS["feishu"])


def _post_registration(base_url: str, body: Dict[str, str]) -> dict:
    """POST form-encoded data to the registration endpoint, return parsed JSON.

    The registration endpoint returns JSON even on 4xx (e.g. poll returns
    authorization_pending as a 400). We always parse the body regardless of
    HTTP status.
    """
    url = f"{base_url}{_REGISTRATION_PATH}"
    data = urlencode(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body_bytes = exc.read()
        if body_bytes:
            try:
                return json.loads(body_bytes.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                raise exc from None
        raise


def _init_registration(domain: str = "feishu") -> None:
    """Verify the environment supports client_secret auth.

    Raises RuntimeError if not supported.
    """
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration environment does not support client_secret auth. "
            f"Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    """Start the device-code flow. Returns device_code, qr_url, user_code, interval, expire_in."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark registration did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    if "?" in qr_url:
        qr_url += "&from=hermes&tp=hermes"
    else:
        qr_url += "?from=hermes&tp=hermes"
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "user_code": res.get("user_code", ""),
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
) -> Optional[dict]:
    """Poll until the user scans the QR code, or timeout/denial.

    Returns dict with app_id, app_secret, domain, open_id on success.
    Returns None on failure.
    """
    deadline = time.monotonic() + expire_in
    current_domain = domain
    domain_switched = False
    poll_count = 0

    while time.monotonic() < deadline:
        base_url = _accounts_base_url(current_domain)
        try:
            res = _post_registration(base_url, {
                "action": "poll",
                "device_code": device_code,
                "tp": "ob_app",
            })
        except (URLError, OSError, json.JSONDecodeError):
            time.sleep(interval)
            continue

        poll_count += 1
        if poll_count == 1:
            print("  Fetching configuration results...", end="", flush=True)
        elif poll_count % 6 == 0:
            print(".", end="", flush=True)

        # Domain auto-detection
        user_info = res.get("user_info") or {}
        tenant_brand = user_info.get("tenant_brand")
        if tenant_brand == "lark" and not domain_switched:
            current_domain = "lark"
            domain_switched = True
            # Fall through — server may return credentials in this same response.

        # Success
        if res.get("client_id") and res.get("client_secret"):
            if poll_count > 0:
                print()  # newline after "Fetching configuration results..." dots
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": current_domain,
                "open_id": user_info.get("open_id"),
            }

        # Terminal errors
        error = res.get("error", "")
        if error in {"access_denied", "expired_token"}:
            if poll_count > 0:
                print()
            logger.warning("[Feishu onboard] Registration %s", error)
            return None

        # authorization_pending or unknown — keep polling
        time.sleep(interval)

    if poll_count > 0:
        print()
    logger.warning("[Feishu onboard] Poll timed out after %ds", expire_in)
    return None


try:
    import qrcode as _qrcode_mod
except (ImportError, TypeError):
    _qrcode_mod = None  # type: ignore[assignment]


def _render_qr(url: str) -> bool:
    """Try to render a QR code in the terminal. Returns True if successful."""
    if _qrcode_mod is None:
        return False
    try:
        qr = _qrcode_mod.QRCode()
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def probe_bot(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    """Verify bot connectivity via /open-apis/bot/v3/info.

    Uses lark_oapi SDK when available, falls back to raw HTTP otherwise.
    Returns {"bot_name": ..., "bot_open_id": ...} on success, None on failure.

    Note: ``bot_open_id`` here is the bot's app-scoped open_id — the same ID
    that Feishu puts in @mention payloads.  It is NOT the app_id.
    """
    if FEISHU_AVAILABLE:
        return _probe_bot_sdk(app_id, app_secret, domain)
    return _probe_bot_http(app_id, app_secret, domain)


def _build_onboard_client(app_id: str, app_secret: str, domain: str) -> Any:
    """Build a lark Client for the given credentials and domain."""
    sdk_domain = LARK_DOMAIN if domain == "lark" else FEISHU_DOMAIN
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(sdk_domain)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _parse_bot_response(data: dict) -> Optional[dict]:
    # /bot/v3/info returns bot.app_name; legacy paths used bot_name — accept both.
    if data.get("code") != 0:
        return None
    bot = data.get("bot") or data.get("data", {}).get("bot") or {}
    return {
        "bot_name": bot.get("app_name") or bot.get("bot_name"),
        "bot_open_id": bot.get("open_id"),
    }


def _probe_bot_sdk(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    """Probe bot info using lark_oapi SDK."""
    try:
        client = _build_onboard_client(app_id, app_secret, domain)
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        resp = client.request(req)
        content = getattr(getattr(resp, "raw", None), "content", None)
        if content is None:
            return None
        return _parse_bot_response(json.loads(content))
    except Exception as exc:
        logger.debug("[Feishu onboard] SDK probe failed: %s", exc)
        return None


def _probe_bot_http(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    """Fallback probe using raw HTTP (when lark_oapi is not installed)."""
    base_url = _onboard_open_base_url(domain)
    try:
        token_data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        token_req = Request(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            data=token_data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(token_req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            token_res = json.loads(resp.read().decode("utf-8"))

        access_token = token_res.get("tenant_access_token")
        if not access_token:
            return None

        bot_req = Request(
            f"{base_url}/open-apis/bot/v3/info",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(bot_req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            bot_res = json.loads(resp.read().decode("utf-8"))

        return _parse_bot_response(bot_res)
    except (URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        logger.debug("[Feishu onboard] HTTP probe failed: %s", exc)
        return None


def qr_register(
    *,
    initial_domain: str = "feishu",
    timeout_seconds: int = 600,
) -> Optional[dict]:
    """Run the Feishu / Lark scan-to-create QR registration flow.

    Returns on success::

        {
            "app_id": str,
            "app_secret": str,
            "domain": "feishu" | "lark",
            "open_id": str | None,
            "bot_name": str | None,
            "bot_open_id": str | None,
        }

    Returns None on expected failures (network, auth denied, timeout).
    Unexpected errors (bugs, protocol regressions) propagate to the caller.
    """
    try:
        return _qr_register_inner(initial_domain=initial_domain, timeout_seconds=timeout_seconds)
    except (RuntimeError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("[Feishu onboard] Registration failed: %s", exc)
        return None


def _qr_register_inner(
    *,
    initial_domain: str,
    timeout_seconds: int,
) -> Optional[dict]:
    """Run init → begin → poll → probe. Raises on network/protocol errors."""
    print("  Connecting to Feishu / Lark...", end="", flush=True)
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)
    print(" done.")

    print()
    qr_url = begin["qr_url"]
    if _render_qr(qr_url):
        print(f"\n  Scan the QR code above, or open this URL directly:\n  {qr_url}")
    else:
        print(f"  Open this URL in Feishu / Lark on your phone:\n\n  {qr_url}\n")
        print("  Tip: pip install qrcode  to display a scannable QR code here next time")
    print()

    result = _poll_registration(
        device_code=begin["device_code"],
        interval=begin["interval"],
        expire_in=min(begin["expire_in"], timeout_seconds),
        domain=initial_domain,
    )
    if not result:
        return None

    # Probe bot — best-effort, don't fail the registration
    bot_info = probe_bot(result["app_id"], result["app_secret"], result["domain"])
    if bot_info:
        result["bot_name"] = bot_info.get("bot_name")
        result["bot_open_id"] = bot_info.get("bot_open_id")
    else:
        result["bot_name"] = None
        result["bot_open_id"] = None

    return result

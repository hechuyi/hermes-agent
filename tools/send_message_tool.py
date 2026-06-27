"""Feishu/Lark send_message tool.

The tool remains a generic agent capability, but this runtime fork only
delivers chat messages through Feishu. Other channel adapters are not
available in the runtime profile.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from agent.redact import redact_sensitive_text
from gateway.config import Platform, PlatformConfig

logger = logging.getLogger(__name__)

_FEISHU_TARGET_RE = re.compile(
    r"^\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*$"
)
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".webm"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".aac"}
_VOICE_EXTS = {".ogg", ".opus"}


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a Feishu/Lark chat, or list known Feishu targets. "
        "When the user names a chat/person rather than giving an explicit ID, "
        "call send_message(action='list') first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list"],
                "description": "'send' sends a message. 'list' returns known Feishu targets.",
            },
            "target": {
                "type": "string",
                "description": (
                    "Feishu delivery target. Use 'feishu' for the configured home chat, "
                    "'feishu:oc_xxx' for a chat, or 'feishu:oc_xxx:message_id' for a reply/thread."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Message text. Include MEDIA:<local_path> to deliver a local file as a native "
                    "Feishu attachment."
                ),
            },
        },
        "required": [],
    },
}


def send_message_tool(args, **_kw):
    """Handle send_message tool calls."""
    action = args.get("action", "send")
    if action == "list":
        return _handle_list()
    return _handle_send(args)


def _handle_list():
    try:
        from gateway.channel_directory import format_directory_for_display

        return json.dumps({"targets": format_directory_for_display()})
    except Exception as exc:
        return json.dumps(_error(f"Failed to load channel directory: {exc}"))


def _handle_send(args):
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    platform_name, target_ref = _split_target(target)
    if platform_name != Platform.FEISHU.value:
        return tool_error(
            f"Unsupported delivery platform: {platform_name}. "
            "This Hermes runtime only delivers send_message targets through Feishu."
        )

    chat_id = None
    thread_id = None
    if target_ref:
        chat_id, thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)
        if target_ref and not is_explicit:
            try:
                from gateway.channel_directory import resolve_channel_name

                resolved = resolve_channel_name(platform_name, target_ref)
            except Exception:
                resolved = None
            if not resolved:
                return json.dumps(
                    {
                        "error": (
                            f"Could not resolve '{target_ref}' on feishu. "
                            "Use send_message(action='list') to see available targets."
                        )
                    }
                )
            chat_id, thread_id, _ = _parse_target_ref(platform_name, resolved)

    from tools.interrupt import is_interrupted

    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
    except Exception as exc:
        return json.dumps(_error(f"Failed to load gateway config: {exc}"))

    platform = Platform.FEISHU
    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return tool_error(
            "Platform 'feishu' is not configured. Set up credentials in "
            "~/.hermes/config.yaml or environment variables."
        )

    from gateway.platforms.base import BasePlatformAdapter

    force_document_attachments = "[[as_document]]" in message
    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if home:
            chat_id = home.chat_id
            thread_id = home.thread_id
            used_home_channel = True
        else:
            return json.dumps(
                {
                    "error": (
                        "No home channel set for feishu to determine where to send the message. "
                        "Specify a target like 'feishu:oc_xxx' or set FEISHU_HOME_CHANNEL."
                    )
                }
            )

    duplicate_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    try:
        from model_tools import _run_async

        result = _run_async(
            _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document_attachments,
            )
        )
    except Exception as exc:
        return json.dumps(_error(f"Send failed: {exc}"))

    if used_home_channel and isinstance(result, dict) and result.get("success"):
        result["note"] = f"Sent to feishu home channel (chat_id: {chat_id})"

    if isinstance(result, dict) and result.get("success") and mirror_text:
        try:
            from gateway.mirror import mirror_to_session
            from gateway.session_context import get_session_env

            source_label = get_session_env("HERMES_SESSION_PLATFORM", "cli")
            user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
            if mirror_to_session(
                platform_name,
                chat_id,
                mirror_text,
                source_label=source_label,
                thread_id=thread_id,
                user_id=user_id,
            ):
                result["mirrored"] = True
        except Exception:
            pass

    if isinstance(result, dict) and "error" in result:
        result["error"] = _sanitize_error_text(result["error"])
    return json.dumps(result)


def _split_target(target: str) -> tuple[str, str | None]:
    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    return platform_name, target_ref


def _parse_target_ref(platform_name: str, target_ref: str):
    """Parse a Feishu tool target into chat_id/thread_id/explicit."""
    if platform_name != Platform.FEISHU.value:
        return None, None, False
    match = _FEISHU_TARGET_RE.fullmatch(target_ref)
    if match:
        return match.group(1), match.group(2), True
    return None, None, False


async def _send_to_platform(
    platform,
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    thread_id: str | None = None,
    media_files: list[tuple[str, bool]] | None = None,
    force_document: bool = False,
):
    """Send a possibly chunked message through Feishu."""
    if platform != Platform.FEISHU:
        return {
            "error": (
                f"Unsupported delivery platform: {getattr(platform, 'value', platform)}. "
                "This Hermes runtime only delivers send_message targets through Feishu."
            )
        }

    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.feishu import FeishuAdapter

    media_files = media_files or []
    chunks = BasePlatformAdapter.truncate_message(
        message,
        FeishuAdapter.MAX_MESSAGE_LENGTH,
    )
    if not chunks:
        chunks = [""]

    last_result: dict[str, Any] | None = None
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        result = await _send_feishu(
            pconfig,
            chat_id,
            chunk,
            media_files=media_files if is_last else [],
            thread_id=thread_id,
        )
        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result
    return last_result or {"error": "No deliverable text or media remained after processing MEDIA tags"}


async def _send_feishu(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    media_files: list[tuple[str, bool]] | None = None,
    thread_id: str | None = None,
):
    """Send via Feishu/Lark using the adapter's send pipeline."""
    try:
        from gateway.platforms.feishu import (
            FEISHU_AVAILABLE,
            FEISHU_DOMAIN,
            LARK_DOMAIN,
            FeishuAdapter,
        )
    except ImportError:
        return {"error": "Feishu dependencies not installed. Run: pip install 'hermes-agent[feishu]'"}

    if not FEISHU_AVAILABLE:
        return {"error": "Feishu dependencies not installed. Run: pip install 'hermes-agent[feishu]'"}

    media_files = media_files or []
    try:
        adapter = FeishuAdapter(pconfig)
        domain_name = getattr(adapter, "_domain_name", "feishu")
        domain = FEISHU_DOMAIN if domain_name != "lark" else LARK_DOMAIN
        adapter._client = adapter._build_lark_client(domain)
        metadata = {"thread_id": thread_id} if thread_id else None

        last_result = None
        if message.strip():
            last_result = await adapter.send(chat_id, message, metadata=metadata)
            if not last_result.success:
                return _error(f"Feishu send failed: {last_result.error}")

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                return _error(f"Media file not found: {media_path}")

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS and not "[[as_document]]" in message:
                last_result = await adapter.send_image_file(chat_id, media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_video(chat_id, media_path, metadata=metadata)
            elif ext in _VOICE_EXTS and is_voice:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            elif ext in _AUDIO_EXTS:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            else:
                last_result = await adapter.send_document(chat_id, media_path, metadata=metadata)

            if not last_result.success:
                return _error(f"Feishu media send failed: {last_result.error}")

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing MEDIA tags"}

        return {
            "success": True,
            "platform": "feishu",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as exc:
        return _error(f"Feishu send failed: {exc}")


def _describe_media_for_mirror(media_files):
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {"platform": platform, "chat_id": chat_id, "thread_id": thread_id}


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None

    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"
    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


def _sanitize_error_text(text) -> str:
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    return {"error": _sanitize_error_text(message)}


def _check_send_message():
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if platform == Platform.FEISHU.value:
        return True
    try:
        from gateway.status import is_gateway_running

        return is_gateway_running()
    except Exception:
        return False


from tools.registry import registry, tool_error

registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=send_message_tool,
    check_fn=_check_send_message,
    emoji="📨",
)

"""Feishu send_message runtime boundary tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from tools.send_message_tool import (
    SEND_MESSAGE_SCHEMA,
    _parse_target_ref,
    _send_to_platform,
    send_message_tool,
)


def _run_async_immediately(coro):
    return asyncio.run(coro)


def test_send_message_delivers_to_feishu_and_rejects_other_platforms():
    feishu_cfg = PlatformConfig(
        enabled=True,
        token="tenant-token",
        extra={"app_id": "cli_a", "app_secret": "sec"},
    )
    config = SimpleNamespace(
        platforms={Platform.FEISHU: feishu_cfg},
        get_home_channel=lambda _platform: None,
    )

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(return_value={"success": True, "message_id": "om_1"}),
         ) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        delivered = json.loads(
            send_message_tool(
                {
                    "action": "send",
                    "target": "feishu:oc_chat:root_msg",
                    "message": "hello",
                }
            )
        )

    rejected = json.loads(
        send_message_tool(
            {
                "action": "send",
                "target": "telegram:-1001",
                "message": "hello",
            }
        )
    )

    assert delivered["success"] is True
    send_mock.assert_awaited_once_with(
        Platform.FEISHU,
        feishu_cfg,
        "oc_chat",
        "hello",
        thread_id="root_msg",
        media_files=[],
        force_document=False,
    )
    assert rejected["error"].startswith("Unsupported delivery platform: telegram")


def test_send_message_rejects_email_before_home_channel_lookup():
    rejected = json.loads(
        send_message_tool(
            {
                "action": "send",
                "target": "email:user@example.com",
                "message": "hello",
            }
        )
    )

    assert rejected["error"].startswith("Unsupported delivery platform: email")
    assert "EMAIL_HOME_CHANNEL" not in rejected["error"]


def test_send_message_schema_is_feishu_only():
    serialized = json.dumps(SEND_MESSAGE_SCHEMA).lower()

    assert "feishu" in serialized
    assert "lark" in serialized
    for old_platform in (
        "discord",
        "telegram",
        "slack",
        "whatsapp",
        "signal",
        "matrix",
        "yuanbao",
        "yb_",
    ):
        assert old_platform not in serialized


def test_feishu_send_chunks_and_attaches_media_to_last_chunk():
    sent_calls = []

    async def fake_send(_pconfig, chat_id, message, media_files=None, thread_id=None):
        sent_calls.append((chat_id, message, media_files or [], thread_id))
        return {
            "success": True,
            "platform": "feishu",
            "chat_id": chat_id,
            "message_id": str(len(sent_calls)),
        }

    with patch("tools.send_message_tool._send_feishu", fake_send), \
         patch("gateway.platforms.feishu.FeishuAdapter.MAX_MESSAGE_LENGTH", 4096):
        result = asyncio.run(
            _send_to_platform(
                Platform.FEISHU,
                PlatformConfig(enabled=True, token="tok", extra={"app_id": "cli_a"}),
                "oc_chat",
                "word " * 1200,
                thread_id="root_msg",
                media_files=[("/tmp/report.pdf", False)],
            )
        )

    assert result["success"] is True
    assert len(sent_calls) >= 2
    assert all(call[2] == [] for call in sent_calls[:-1])
    assert sent_calls[-1][2] == [("/tmp/report.pdf", False)]
    assert _parse_target_ref("feishu", "oc_chat:root_msg") == (
        "oc_chat",
        "root_msg",
        True,
    )
    assert _parse_target_ref("telegram", "-1001")[2] is False

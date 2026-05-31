from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class CaptureAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="notice-1")


def _runner_with(adapter: CaptureAdapter) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.FEISHU: adapter}
    runner.config = SimpleNamespace(get_notice_delivery=lambda platform: "public")
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id="omt_topic",
        user_id="ou_user",
    )


@pytest.mark.asyncio
async def test_current_event_notice_uses_feishu_reply_anchor_metadata():
    adapter = CaptureAdapter()
    runner = _runner_with(adapter)

    await runner._deliver_platform_notice(
        _source(),
        "notice",
        reply_to_message_id="om_current_event",
    )

    assert adapter.calls == [
        {
            "chat_id": "oc_chat",
            "content": "notice",
            "reply_to": None,
            "metadata": {
                "thread_id": "omt_topic",
                "reply_to_message_id": "om_current_event",
            },
        }
    ]


@pytest.mark.asyncio
async def test_background_notice_does_not_bind_stale_feishu_anchor():
    adapter = CaptureAdapter()
    runner = _runner_with(adapter)

    await runner._deliver_platform_notice(_source(), "notice")

    assert adapter.calls == [
        {
            "chat_id": "oc_chat",
            "content": "notice",
            "reply_to": None,
            "metadata": {"thread_id": "omt_topic"},
        }
    ]

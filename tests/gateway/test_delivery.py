"""Tests for the delivery routing module."""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget, _is_silence_narration
from gateway.platforms.base import SendResult
from gateway.session import SessionSource


class TestParseTargetPlatformChat:
    def test_explicit_feishu_chat(self):
        target = DeliveryTarget.parse("feishu:oc_12345")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_12345"
        assert target.is_explicit is True

    def test_platform_only_no_chat_id(self):
        target = DeliveryTarget.parse("feishu")
        assert target.platform == Platform.FEISHU
        assert target.chat_id is None
        assert target.is_explicit is False

    def test_local_target(self):
        target = DeliveryTarget.parse("local")
        assert target.platform == Platform.LOCAL
        assert target.chat_id is None

    def test_origin_with_source(self):
        origin = SessionSource(platform=Platform.FEISHU, chat_id="oc_789", thread_id="omt_42")
        target = DeliveryTarget.parse("origin", origin=origin)
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_789"
        assert target.thread_id == "omt_42"
        assert target.is_origin is True

    def test_origin_without_source(self):
        target = DeliveryTarget.parse("origin")
        assert target.platform == Platform.LOCAL
        assert target.is_origin is True

    def test_unknown_platform(self):
        target = DeliveryTarget.parse("unknown_platform")
        assert target.platform == Platform.LOCAL

    @pytest.mark.parametrize(
        ("raw", "platform", "chat_id"),
        [
            ("telegram:12345", Platform.TELEGRAM, "12345"),
            ("discord:99887766", Platform.DISCORD, "99887766"),
            ("slack:C123ABC", Platform.SLACK, "C123ABC"),
        ],
    )
    def test_legacy_platform_strings_parse_for_historical_data(self, raw, platform, chat_id):
        target = DeliveryTarget.parse(raw)
        assert target.platform == platform
        assert target.chat_id == chat_id
        assert target.is_explicit is True


class TestTargetToStringRoundtrip:
    def test_origin_roundtrip(self):
        origin = SessionSource(platform=Platform.FEISHU, chat_id="oc_111", thread_id="omt_42")
        target = DeliveryTarget.parse("origin", origin=origin)
        assert target.to_string() == "origin"

    def test_local_roundtrip(self):
        target = DeliveryTarget.parse("local")
        assert target.to_string() == "local"

    def test_platform_only_roundtrip(self):
        target = DeliveryTarget.parse("feishu")
        assert target.to_string() == "feishu"

    def test_explicit_chat_roundtrip(self):
        target = DeliveryTarget.parse("feishu:oc_999")
        s = target.to_string()
        assert s == "feishu:oc_999"

        reparsed = DeliveryTarget.parse(s)
        assert reparsed.platform == Platform.FEISHU
        assert reparsed.chat_id == "oc_999"


class TestCaseSensitiveChatIdParsing:
    """Test that chat IDs preserve their original case (issue #11768)."""
    
    def test_feishu_mixed_case_chat_id_preserved(self):
        """Chat IDs should preserve case."""
        target = DeliveryTarget.parse("feishu:oc_ChatABC")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_ChatABC"
        assert target.is_explicit is True
    
    def test_feishu_chat_id_with_thread_preserved(self):
        """Chat:thread IDs should preserve case."""
        target = DeliveryTarget.parse("feishu:oc_ChatABC:omt_Thread123")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_ChatABC"
        assert target.thread_id == "omt_Thread123"
    
    def test_colon_containing_id_segments_preserve_case(self):
        """Colon-containing IDs preserve case within the parser's current structure.
        
        Due to the platform:chat_id:thread_id format, IDs containing colons
        are split after the first two separators. This test covers parser
        mechanics without treating the value as a current runtime target.
        """
        target = DeliveryTarget.parse("feishu:!RoomABC:example.org")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "!RoomABC"
        assert target.thread_id == "example.org"
    
    def test_mixed_case_chat_id_roundtrip(self):
        """Mixed-case chat IDs should survive parse-to_string roundtrip."""
        original = "feishu:ChatId123ABC"
        target = DeliveryTarget.parse(original)
        s = target.to_string()
        reparsed = DeliveryTarget.parse(s)
        assert reparsed.chat_id == "ChatId123ABC"


class TestPlatformNameCaseInsensitivity:
    """Test that platform names are case-insensitive."""
    
    def test_uppercase_platform_name(self):
        """Platform names should be case-insensitive."""
        target = DeliveryTarget.parse("FEISHU:oc_12345")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_12345"
    
    def test_mixed_case_platform_name(self):
        """Mixed-case platform names should work."""
        target = DeliveryTarget.parse("FeiShu:oc_12345")
        assert target.platform == Platform.FEISHU
        assert target.chat_id == "oc_12345"

class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}


@pytest.mark.parametrize(
    "content",
    [
        "*(silent)*",
        "*Silence.*",
        "🔇",
        ".",
        "...",
        "…",
        "(silent)",
        "_silent_",
        "`silent`",
        "~silent~",
        "no response",
        "No Reply.",
    ],
)
def test_is_silence_narration_positive(content):
    assert _is_silence_narration(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "Silence is golden - here is the plan...",
        "Silent install completed",
        "The deployment ran silently in the background",
        "ok",
        "Here is the result:\n\n- item one\n- item two",
        "silent " + "x" * 70,
        "",
        "   ",
        None,
    ],
)
def test_is_silence_narration_negative(content):
    assert _is_silence_narration(content) is False


@pytest.mark.asyncio
async def test_silence_narration_dropped_pre_send(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})
    target = DeliveryTarget.parse("feishu:oc_99887766")

    result = await router._deliver_to_platform(target, "*(silent)*", metadata=None)

    assert adapter.calls == []
    assert result == {
        "success": True,
        "filtered": "silence_narration",
        "delivered": False,
    }


@pytest.mark.asyncio
async def test_silence_filter_config_opt_out_delivers(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    adapter = RecordingAdapter()
    config = GatewayConfig(filter_silence_narration=False)
    router = DeliveryRouter(config, adapters={Platform.FEISHU: adapter})
    target = DeliveryTarget.parse("feishu:oc_99887766")

    result = await router._deliver_to_platform(target, "*(silent)*", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["content"] == "*(silent)*"
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_silence_filter_env_override_disables_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_FILTER_SILENCE_NARRATION", "0")
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})
    target = DeliveryTarget.parse("feishu:oc_99887766")

    result = await router._deliver_to_platform(target, "🔇", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["content"] == "🔇"
    assert result == {"success": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "platform"),
    [
        ("telegram:722341991", Platform.TELEGRAM),
        ("discord:99887766", Platform.DISCORD),
        ("slack:C123ABC", Platform.SLACK),
    ],
)
async def test_legacy_platform_targets_are_not_runtime_delivery_targets(
    raw, platform, tmp_path, monkeypatch
):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={platform: adapter})
    target = DeliveryTarget.parse(raw)

    with pytest.raises(ValueError, match="not an active chat delivery platform"):
        await router._deliver_to_platform(target, "hello", metadata=None)

    assert adapter.calls == []


@pytest.mark.asyncio
async def test_feishu_thread_id_is_forwarded_as_generic_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})
    target = DeliveryTarget.parse("feishu:oc_group:omt_thread")

    await router._deliver_to_platform(target, "hello", metadata={"job_id": "job-1"})

    assert adapter.calls == [
        {
            "chat_id": "oc_group",
            "content": "hello",
            "metadata": {"job_id": "job-1", "thread_id": "omt_thread"},
        }
    ]


@pytest.mark.asyncio
async def test_feishu_thread_id_does_not_override_explicit_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})
    target = DeliveryTarget.parse("feishu:oc_group:omt_from_target")

    await router._deliver_to_platform(target, "hello", metadata={"thread_id": "omt_explicit"})

    assert adapter.calls[0]["metadata"] == {"thread_id": "omt_explicit"}


class FailingAdapter:
    async def send(self, chat_id, content, metadata=None):
        return SendResult(success=False, error="route failed", retryable=False)


@pytest.mark.asyncio
async def test_platform_send_failure_raises_for_delivery_result(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: FailingAdapter()})
    target = DeliveryTarget.parse("feishu:oc_group:omt_thread")

    with pytest.raises(RuntimeError, match="route failed"):
        await router._deliver_to_platform(target, "hello", metadata=None)

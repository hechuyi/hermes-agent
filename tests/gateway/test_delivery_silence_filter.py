"""Tests for outbound silence-narration filtering in gateway delivery."""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.delivery import DeliveryRouter, DeliveryTarget, _is_silence_narration


POSITIVE_CASES = [
    "*(silent)*",
    "*Silence.*",
    "\U0001F507",
    ".",
    "\u2026",
    "...",
    "(silent)",
    "_silent_",
    "`silent`",
    "~silent~",
    "Silence",
    "no response",
    "No Reply.",
]

NEGATIVE_CASES = [
    "Silence is golden - here is the plan...",
    "Silent install completed",
    "The deployment ran silently in the background",
    "ok",
    "Here is the result:\n\n- item one\n- item two",
    "I have nothing to add, but here is why: the build is green.",
    "silently",
    "no responses were collected from the survey",
    "silent " + "x" * 70,
    "",
    "   ",
]


@pytest.mark.parametrize("content", POSITIVE_CASES)
def test_is_silence_narration_positive(content):
    assert _is_silence_narration(content) is True


@pytest.mark.parametrize("content", NEGATIVE_CASES)
def test_is_silence_narration_negative(content):
    assert _is_silence_narration(content) is False


def test_is_silence_narration_none_safe():
    assert _is_silence_narration(None) is False


def test_length_guard_rejects_long_strings():
    assert _is_silence_narration("." * 64) is True
    assert _is_silence_narration("." * 65) is False


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "metadata": metadata}
        )
        return {"success": True}


def _feishu_target() -> DeliveryTarget:
    return DeliveryTarget.parse("feishu:oc_99887766")


@pytest.mark.asyncio
async def test_silence_narration_dropped_pre_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})

    result = await router._deliver_to_platform(
        _feishu_target(), "*(silent)*", metadata=None
    )

    assert adapter.calls == []
    assert result == {
        "success": True,
        "filtered": "silence_narration",
        "delivered": False,
    }


@pytest.mark.asyncio
async def test_real_message_is_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})

    result = await router._deliver_to_platform(
        _feishu_target(), "Silence is golden - here is the plan...", metadata=None
    )

    assert adapter.calls == [
        {
            "chat_id": "oc_99887766",
            "content": "Silence is golden - here is the plan...",
            "metadata": None,
        }
    ]
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_config_opt_out_lets_silence_through(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    adapter = RecordingAdapter()
    config = GatewayConfig(filter_silence_narration=False)
    router = DeliveryRouter(config, adapters={Platform.FEISHU: adapter})

    result = await router._deliver_to_platform(
        _feishu_target(), "*(silent)*", metadata=None
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["content"] == "*(silent)*"
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_env_override_disables_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_FILTER_SILENCE_NARRATION", "0")
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.FEISHU: adapter})

    result = await router._deliver_to_platform(
        _feishu_target(), "\U0001F507", metadata=None
    )

    assert len(adapter.calls) == 1
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_env_override_enables_filter_over_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_FILTER_SILENCE_NARRATION", "1")
    adapter = RecordingAdapter()
    config = GatewayConfig(filter_silence_narration=False)
    router = DeliveryRouter(config, adapters={Platform.FEISHU: adapter})

    result = await router._deliver_to_platform(
        _feishu_target(), "*(silent)*", metadata=None
    )

    assert adapter.calls == []
    assert result["filtered"] == "silence_narration"


@pytest.mark.asyncio
async def test_local_delivery_not_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)
    router = DeliveryRouter(GatewayConfig(), adapters={})

    results = await router.deliver(
        content="*(silent)*",
        targets=[DeliveryTarget.parse("local")],
        job_id="silence-job",
    )

    local_result = results["local"]
    assert local_result["success"] is True
    assert local_result["result"]["path"].endswith(".md")


def test_config_flag_defaults_true():
    assert GatewayConfig().filter_silence_narration is True


def test_config_from_dict_parses_flag():
    cfg = GatewayConfig.from_dict({"filter_silence_narration": False})
    assert cfg.filter_silence_narration is False


def test_config_to_dict_roundtrip():
    cfg = GatewayConfig(filter_silence_narration=False)
    restored = GatewayConfig.from_dict(cfg.to_dict())
    assert restored.filter_silence_narration is False


def test_load_gateway_config_reads_top_level_silence_filter(tmp_path, monkeypatch):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "filter_silence_narration: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert load_gateway_config().filter_silence_narration is False


def test_load_gateway_config_reads_nested_gateway_silence_filter(
    tmp_path, monkeypatch
):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "gateway:\n  filter_silence_narration: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert load_gateway_config().filter_silence_narration is False

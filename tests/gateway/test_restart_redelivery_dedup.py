"""Legacy Telegram /restart redelivery guard.

When PTB's graceful-shutdown ACK call (the final `get_updates` on exit) fails
with a network error, Telegram re-delivers the `/restart` message to the new
gateway process.  Without a dedup guard, the new gateway would process
`/restart` again and immediately restart in a self-perpetuating loop.

This is not the current Feishu/API/headless idempotency mechanism.  Feishu
uses stable event identities in its inbound gateway-event ledger; the legacy
numeric ``platform_update_id`` marker is intentionally scoped to Telegram.
"""
import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _make_restart_event(update_id: int | None = 100) -> MessageEvent:
    return MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=update_id,
    )


@pytest.mark.asyncio
async def test_legacy_telegram_restart_writes_dedup_marker_with_update_id(tmp_path, monkeypatch):
    """First Telegram /restart writes the triggering update_id compatibility marker."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    event = _make_restart_event(update_id=12345)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    marker_path = tmp_path / ".restart_last_processed.json"
    assert marker_path.exists()
    data = json.loads(marker_path.read_text())
    assert data["platform"] == "telegram"
    assert data["update_id"] == 12345
    assert isinstance(data["requested_at"], (int, float))


@pytest.mark.asyncio
async def test_legacy_telegram_redelivery_with_same_update_id_is_ignored(tmp_path, monkeypatch):
    """Telegram /restart with update_id <= marker is silently ignored as redelivery."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    # Previous legacy Telegram gateway recorded update_id=12345 a few seconds ago.
    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 5,
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock()

    event = _make_restart_event(update_id=12345)  # same update_id: redelivery
    result = await runner._handle_restart_command(event)

    assert result == ""  # silently ignored
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_telegram_redelivery_with_older_update_id_is_ignored(tmp_path, monkeypatch):
    """A Telegram update_id lower than the marker is also treated as stale."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 5,
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock()

    # An older update should not normally appear after restart, but if Telegram
    # redelivers one, it is stale relative to the recorded offset.
    event = _make_restart_event(update_id=12344)
    result = await runner._handle_restart_command(event)

    assert result == ""
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_telegram_restart_with_higher_update_id_is_processed(tmp_path, monkeypatch):
    """A newer Telegram update_id is a fresh /restart and bypasses the marker."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    # Previous legacy Telegram restart recorded update_id=12345.
    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 5,
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    event = _make_restart_event(update_id=12346)  # strictly higher: fresh
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()

    # Marker is overwritten with the new update_id.
    data = json.loads(marker.read_text())
    assert data["update_id"] == 12346


@pytest.mark.asyncio
async def test_legacy_telegram_stale_marker_older_than_5min_does_not_block(tmp_path, monkeypatch):
    """A marker older than the 5-minute window does not block /restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 600,  # 10 minutes ago
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    # Same update_id as the stale marker, but the marker is too old to trust.
    event = _make_restart_event(update_id=12345)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_telegram_no_marker_file_allows_restart(tmp_path, monkeypatch):
    """Clean gateway start with no prior marker processes /restart normally."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    event = _make_restart_event(update_id=100)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_telegram_corrupt_marker_file_is_treated_as_absent(tmp_path, monkeypatch):
    """Malformed JSON in the marker file doesn't crash — /restart proceeds."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text("not-json{")

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    event = _make_restart_event(update_id=100)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_telegram_event_without_update_id_bypasses_dedup(tmp_path, monkeypatch):
    """Events without platform_update_id are outside the legacy Telegram guard."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 999999,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    # No update_id: the legacy marker should not kick in.
    event = _make_restart_event(update_id=None)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_feishu_restart_uses_current_event_identity_not_legacy_update_marker(
    tmp_path,
    monkeypatch,
):
    """A Feishu event_id is not treated as the legacy Telegram update marker."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    feishu_source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_feishu_chat",
        chat_type="dm",
        user_id="ou_user",
    )
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=feishu_source,
        message_id="m1",
        raw_message={"header": {"event_id": "evt_restart_redelivery"}},
        platform_update_id=12345,
    )
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()

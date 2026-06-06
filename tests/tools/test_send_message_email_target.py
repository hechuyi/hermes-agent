"""Focused send_message email target tests without telegram optional deps."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform
from tools.send_message_tool import _parse_target_ref, send_message_tool


class TestParseTargetRefEmail:
    """_parse_target_ref recognizes email addresses as explicit email targets."""

    def test_standard_email_is_explicit(self):
        chat_id, thread_id, is_explicit = _parse_target_ref("email", "user@example.com")
        assert chat_id == "user@example.com"
        assert thread_id is None
        assert is_explicit is True

    def test_email_with_dots_in_local_part(self):
        chat_id, _, is_explicit = _parse_target_ref("email", "first.last@example.co.uk")
        assert chat_id == "first.last@example.co.uk"
        assert is_explicit is True

    def test_email_with_plus_tag(self):
        chat_id, _, is_explicit = _parse_target_ref("email", "user+tag@gmail.com")
        assert chat_id == "user+tag@gmail.com"
        assert is_explicit is True

    def test_email_strips_whitespace(self):
        chat_id, _, is_explicit = _parse_target_ref("email", "  user@example.com  ")
        assert chat_id == "user@example.com"
        assert is_explicit is True

    def test_invalid_email_not_explicit(self):
        assert _parse_target_ref("email", "not-an-email")[2] is False
        assert _parse_target_ref("email", "@example.com")[2] is False
        assert _parse_target_ref("email", "user@")[2] is False
        assert _parse_target_ref("email", "user@.com")[2] is False

    def test_email_not_explicit_for_other_platforms(self):
        assert _parse_target_ref("telegram", "user@example.com")[2] is False
        assert _parse_target_ref("discord", "user@example.com")[2] is False
        assert _parse_target_ref("slack", "user@example.com")[2] is False


class TestEmailHomeChannelErrorHint:
    """Email no-home-channel guidance should name the variable actually read."""

    def test_email_error_names_email_home_address(self):
        email_cfg = SimpleNamespace(enabled=True, token="", extra={})
        config = SimpleNamespace(
            platforms={Platform.EMAIL: email_cfg},
            get_home_channel=lambda _platform: None,
        )
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "email",
                        "message": "hi",
                    }
                )
            )
        assert "EMAIL_HOME_ADDRESS" in result["error"]
        assert "EMAIL_HOME_CHANNEL" not in result["error"]

    def test_non_email_platform_keeps_generic_home_channel_hint(self):
        telegram_cfg = SimpleNamespace(enabled=True, token="***", extra={})
        config = SimpleNamespace(
            platforms={Platform.TELEGRAM: telegram_cfg},
            get_home_channel=lambda _platform: None,
        )
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram",
                        "message": "hi",
                    }
                )
            )
        assert "TELEGRAM_HOME_CHANNEL" in result["error"]

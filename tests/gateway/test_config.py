"""Tests for gateway configuration management."""

import os
from unittest.mock import patch

from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
    StreamingConfig,
    _apply_env_overrides,
    load_gateway_config,
)


class TestHomeChannelRoundtrip:
    def test_to_dict_from_dict(self):
        hc = HomeChannel(platform=Platform.FEISHU, chat_id="oc_999", name="general")
        d = hc.to_dict()
        restored = HomeChannel.from_dict(d)

        assert restored.platform == Platform.FEISHU
        assert restored.chat_id == "oc_999"
        assert restored.name == "general"


class TestPlatformConfigRoundtrip:
    def test_to_dict_from_dict(self):
        pc = PlatformConfig(
            enabled=True,
            token="tok_123",
            home_channel=HomeChannel(
                platform=Platform.FEISHU,
                chat_id="oc_555",
                name="Home",
            ),
            extra={"foo": "bar"},
        )
        d = pc.to_dict()
        restored = PlatformConfig.from_dict(d)

        assert restored.enabled is True
        assert restored.token == "tok_123"
        assert restored.home_channel.chat_id == "oc_555"
        assert restored.extra == {"foo": "bar"}

    def test_disabled_no_token(self):
        pc = PlatformConfig()
        d = pc.to_dict()
        restored = PlatformConfig.from_dict(d)
        assert restored.enabled is False
        assert restored.token is None

    def test_from_dict_coerces_quoted_false_enabled(self):
        restored = PlatformConfig.from_dict({"enabled": "false"})
        assert restored.enabled is False

    def test_gateway_restart_notification_defaults_true(self):
        assert PlatformConfig().gateway_restart_notification is True
        assert PlatformConfig.from_dict({}).gateway_restart_notification is True

    def test_gateway_restart_notification_roundtrip_false(self):
        pc = PlatformConfig(enabled=True, gateway_restart_notification=False)
        restored = PlatformConfig.from_dict(pc.to_dict())
        assert restored.gateway_restart_notification is False

    def test_gateway_restart_notification_coerces_quoted_false(self):
        restored = PlatformConfig.from_dict({"gateway_restart_notification": "false"})
        assert restored.gateway_restart_notification is False


class TestGetConnectedPlatforms:
    def test_returns_current_enabled_platforms_with_required_config(self):
        config = GatewayConfig(
            platforms={
                Platform.FEISHU: PlatformConfig(
                    enabled=True,
                    extra={"app_id": "cli_current"},
                ),
                Platform.API_SERVER: PlatformConfig(enabled=False, extra={"key": "k"}),
                Platform.LOCAL: PlatformConfig(enabled=True),  # no connection material
            },
        )
        connected = config.get_connected_platforms()
        assert Platform.FEISHU in connected
        assert Platform.API_SERVER not in connected
        assert Platform.LOCAL not in connected

    def test_empty_platforms(self):
        config = GatewayConfig()
        assert config.get_connected_platforms() == []

    def test_legacy_dingtalk_recognised_via_extras_for_internal_schema_compatibility(self):
        """Legacy enum compatibility: not a current runtime candidate."""
        config = GatewayConfig(
            platforms={
                Platform.DINGTALK: PlatformConfig(
                    enabled=True,
                    extra={"client_id": "cid", "client_secret": "sec"},
                ),
            },
        )
        assert Platform.DINGTALK in config.get_connected_platforms()

    def test_legacy_dingtalk_recognised_via_env_vars_for_internal_schema_compatibility(self, monkeypatch):
        """Legacy enum compatibility: not a current runtime candidate."""
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env_cid")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env_sec")
        config = GatewayConfig(
            platforms={
                Platform.DINGTALK: PlatformConfig(enabled=True, extra={}),
            },
        )
        assert Platform.DINGTALK in config.get_connected_platforms()

    def test_legacy_dingtalk_missing_creds_not_connected_for_internal_schema_compatibility(self, monkeypatch):
        """Legacy enum compatibility: not a current runtime candidate."""
        monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
        config = GatewayConfig(
            platforms={
                Platform.DINGTALK: PlatformConfig(enabled=True, extra={}),
            },
        )
        assert Platform.DINGTALK not in config.get_connected_platforms()

    def test_legacy_dingtalk_disabled_not_connected_for_internal_schema_compatibility(self):
        """Legacy enum compatibility: not a current runtime candidate."""
        config = GatewayConfig(
            platforms={
                Platform.DINGTALK: PlatformConfig(
                    enabled=False,
                    extra={"client_id": "cid", "client_secret": "sec"},
                ),
            },
        )
        assert Platform.DINGTALK not in config.get_connected_platforms()


class TestSessionResetPolicy:
    def test_roundtrip(self):
        policy = SessionResetPolicy(mode="idle", at_hour=6, idle_minutes=120)
        d = policy.to_dict()
        restored = SessionResetPolicy.from_dict(d)
        assert restored.mode == "idle"
        assert restored.at_hour == 6
        assert restored.idle_minutes == 120

    def test_defaults(self):
        policy = SessionResetPolicy()
        assert policy.mode == "both"
        assert policy.at_hour == 4
        assert policy.idle_minutes == 1440

    def test_from_dict_treats_null_values_as_defaults(self):
        restored = SessionResetPolicy.from_dict(
            {"mode": None, "at_hour": None, "idle_minutes": None}
        )
        assert restored.mode == "both"
        assert restored.at_hour == 4
        assert restored.idle_minutes == 1440

    def test_from_dict_coerces_quoted_false_notify(self):
        restored = SessionResetPolicy.from_dict({"notify": "false"})
        assert restored.notify is False


class TestStreamingConfig:
    def test_defaults_to_edit_transport(self):
        restored = StreamingConfig.from_dict({"enabled": "true"})
        assert restored.transport == "edit"

    def test_from_dict_coerces_quoted_false_enabled(self):
        restored = StreamingConfig.from_dict({"enabled": "false"})
        assert restored.enabled is False

    def test_from_dict_malformed_numeric_values_fall_back_to_defaults(self):
        restored = StreamingConfig.from_dict(
            {
                "edit_interval": "oops",
                "buffer_threshold": "oops",
                "fresh_final_after_seconds": "oops",
            }
        )
        assert restored.edit_interval == 0.8
        assert restored.buffer_threshold == 24
        assert restored.fresh_final_after_seconds == 60.0


class TestGatewayConfigRoundtrip:
    def test_full_roundtrip(self):
        config = GatewayConfig(
            platforms={
                Platform.FEISHU: PlatformConfig(
                    enabled=True,
                    token="tok_123",
                    home_channel=HomeChannel(Platform.FEISHU, "oc_123", "Home"),
                ),
            },
            reset_triggers=["/new"],
            quick_commands={"limits": {"type": "exec", "command": "echo ok"}},
            group_sessions_per_user=False,
            thread_sessions_per_user=True,
            group_conversation_scope_per_user=True,
            thread_conversation_scope_per_user=True,
        )
        d = config.to_dict()
        restored = GatewayConfig.from_dict(d)

        assert Platform.FEISHU in restored.platforms
        assert restored.platforms[Platform.FEISHU].token == "tok_123"
        assert restored.reset_triggers == ["/new"]
        assert restored.quick_commands == {"limits": {"type": "exec", "command": "echo ok"}}
        assert restored.group_sessions_per_user is False
        assert restored.thread_sessions_per_user is True
        assert restored.group_conversation_scope_per_user is True
        assert restored.thread_conversation_scope_per_user is True

    def test_roundtrip_preserves_feishu_unauthorized_dm_behavior(self):
        config = GatewayConfig(
            unauthorized_dm_behavior="ignore",
            platforms={
                Platform.FEISHU: PlatformConfig(
                    enabled=True,
                    extra={"unauthorized_dm_behavior": "pair"},
                ),
            },
        )

        restored = GatewayConfig.from_dict(config.to_dict())

        assert restored.unauthorized_dm_behavior == "ignore"
        assert restored.platforms[Platform.FEISHU].extra["unauthorized_dm_behavior"] == "pair"

    def test_from_dict_coerces_quoted_false_always_log_local(self):
        restored = GatewayConfig.from_dict({"always_log_local": "false"})
        assert restored.always_log_local is False

    def test_get_notice_delivery_defaults_to_public(self):
        config = GatewayConfig(
            platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")}
        )

        assert config.get_notice_delivery(Platform.FEISHU) == "public"

    def test_get_notice_delivery_honors_platform_override(self):
        config = GatewayConfig(
            platforms={
                Platform.FEISHU: PlatformConfig(
                    enabled=True,
                    token="***",
                    extra={"notice_delivery": "private"},
                ),
            }
        )

        assert config.get_notice_delivery(Platform.FEISHU) == "private"


class TestLoadGatewayConfig:
    def test_bridges_quick_commands_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "quick_commands:\n"
            "  limits:\n"
            "    type: exec\n"
            "    command: echo ok\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.quick_commands == {"limits": {"type": "exec", "command": "echo ok"}}

    def test_bridges_group_sessions_per_user_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("group_sessions_per_user: false\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.group_sessions_per_user is False

    def test_bridges_thread_sessions_per_user_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("thread_sessions_per_user: true\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.thread_sessions_per_user is True

    def test_bridges_group_conversation_scope_per_user_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("group_conversation_scope_per_user: true\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.group_conversation_scope_per_user is True

    def test_bridges_thread_conversation_scope_per_user_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("thread_conversation_scope_per_user: true\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.thread_conversation_scope_per_user is True

    def test_thread_sessions_per_user_defaults_to_false(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("{}\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.thread_sessions_per_user is False

    def test_bridges_feishu_shared_keys_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "feishu:\n"
            "  allow_from:\n"
            "    - ou_operator\n"
            "  channel_prompts:\n"
            "    oc_research: Research mode\n"
            "    oc_ops: Operations mode\n"
            "  notice_delivery: private\n"
            "  unauthorized_dm_behavior: ignore\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        feishu = config.platforms[Platform.FEISHU]
        assert feishu.extra["allow_from"] == ["ou_operator"]
        assert feishu.extra["channel_prompts"] == {
            "oc_research": "Research mode",
            "oc_ops": "Operations mode",
        }
        assert config.get_notice_delivery(Platform.FEISHU) == "private"
        assert config.get_unauthorized_dm_behavior(Platform.FEISHU) == "ignore"

    def test_feishu_shared_key_yaml_does_not_overwrite_env_bridge(self, tmp_path, monkeypatch):
        """Explicit env vars still win over config.yaml for active Feishu bridges."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "feishu:\n"
            "  allow_bots: all\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("FEISHU_ALLOW_BOTS", "none")

        load_gateway_config()

        assert os.environ.get("FEISHU_ALLOW_BOTS") == "none"

    def test_bridges_nested_gateway_platforms_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n"
            "  platforms:\n"
            "    feishu:\n"
            "      enabled: true\n"
            "      token: nested-token\n"
            "      home_channel:\n"
            "        platform: feishu\n"
            "        chat_id: oc_123\n"
            "        name: Nested Home\n"
            "      extra:\n"
            "        reply_prefix: nested\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        feishu = config.platforms[Platform.FEISHU]
        assert feishu.enabled is True
        assert feishu.token == "nested-token"
        assert feishu.home_channel == HomeChannel(
            platform=Platform.FEISHU,
            chat_id="oc_123",
            name="Nested Home",
        )
        assert feishu.extra["reply_prefix"] == "nested"

    def test_top_level_platforms_override_nested_gateway_platforms(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n"
            "  platforms:\n"
            "    feishu:\n"
            "      enabled: false\n"
            "      token: nested-token\n"
            "      extra:\n"
            "        reply_prefix: nested\n"
            "platforms:\n"
            "  feishu:\n"
            "    enabled: true\n"
            "    token: top-token\n"
            "    extra:\n"
            "      reply_prefix: top\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        feishu = config.platforms[Platform.FEISHU]
        assert feishu.enabled is True
        assert feishu.token == "top-token"
        assert feishu.extra["reply_prefix"] == "top"

    def test_bridges_quoted_false_platform_enabled_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "platforms:\n"
            "  api_server:\n"
            "    enabled: \"false\"\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.platforms[Platform.API_SERVER].enabled is False
        assert Platform.API_SERVER not in config.get_connected_platforms()

    def test_bridges_quoted_false_session_notify_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "session_reset:\n"
            "  notify: \"false\"\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.default_reset_policy.notify is False

    def test_bridges_quoted_false_always_log_local_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "always_log_local: \"false\"\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.always_log_local is False

    def test_bridges_feishu_channel_prompts_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "feishu:\n"
            "  channel_prompts:\n"
            "    oc_research: Research mode\n"
            "    oc_ops: Operations mode\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.platforms[Platform.FEISHU].extra["channel_prompts"] == {
            "oc_research": "Research mode",
            "oc_ops": "Operations mode",
        }

    def test_ignores_legacy_discord_history_backfill_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Discord YAML must not seed env or survive as a runtime platform."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "discord:\n"
            "  history_backfill: true\n"
            "  history_backfill_limit: 17\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("DISCORD_HISTORY_BACKFILL", raising=False)
        monkeypatch.delenv("DISCORD_HISTORY_BACKFILL_LIMIT", raising=False)

        config = load_gateway_config()

        assert Platform.DISCORD not in config.platforms
        assert os.getenv("DISCORD_HISTORY_BACKFILL") is None
        assert os.getenv("DISCORD_HISTORY_BACKFILL_LIMIT") is None

    def test_ignores_legacy_telegram_channel_prompts_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Telegram YAML may parse internally, but is pruned from this runtime fork."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  channel_prompts:\n"
            '    "-1001234567": Research assistant\n'
            "    789: Creative writing\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert Platform.TELEGRAM not in config.platforms

    def test_ignores_legacy_slack_channel_prompts_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Slack YAML may parse internally, but is pruned from this runtime fork."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "slack:\n"
            "  channel_prompts:\n"
            '    "C01ABC": Code review mode\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert Platform.SLACK not in config.platforms

    def test_bridges_feishu_allow_bots_from_config_yaml_to_env(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "feishu:\n  allow_bots: mentions\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("FEISHU_ALLOW_BOTS", raising=False)

        load_gateway_config()

        assert os.environ.get("FEISHU_ALLOW_BOTS") == "mentions"

    def test_invalid_quick_commands_in_config_yaml_are_ignored(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("quick_commands: not-a-mapping\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.quick_commands == {}

    def test_bridges_feishu_unauthorized_dm_behavior_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "unauthorized_dm_behavior: ignore\n"
            "feishu:\n"
            "  unauthorized_dm_behavior: pair\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.unauthorized_dm_behavior == "ignore"
        assert config.platforms[Platform.FEISHU].extra["unauthorized_dm_behavior"] == "pair"

    def test_ignores_legacy_telegram_disable_link_previews_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Telegram presentation keys are not a current runtime surface."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  disable_link_previews: true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert Platform.TELEGRAM not in config.platforms

    def test_ignores_legacy_telegram_extra_base_url_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Telegram adapter keys are not a current runtime surface."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  extra:\n"
            "    base_url: https://custom-proxy.example.com/bot\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert Platform.TELEGRAM not in config.platforms

    def test_bridges_feishu_notice_delivery_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "feishu:\n"
            "  notice_delivery: private\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.get_notice_delivery(Platform.FEISHU) == "private"

    def test_ignores_legacy_telegram_proxy_url_yaml_as_runtime_surface(self, tmp_path, monkeypatch):
        """Legacy Telegram proxy YAML must not seed env in the Feishu runtime fork."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  proxy_url: socks5://127.0.0.1:1080\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

        config = load_gateway_config()

        assert Platform.TELEGRAM not in config.platforms
        assert os.environ.get("TELEGRAM_PROXY") is None

    def test_legacy_telegram_proxy_env_is_preserved_but_not_runtime_config(self, tmp_path, monkeypatch):
        """Existing legacy env remains untouched, but Telegram is not exposed."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  proxy_url: http://from-config:8080\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://from-env:1080")

        config = load_gateway_config()

        assert Platform.TELEGRAM not in config.platforms
        assert os.environ.get("TELEGRAM_PROXY") == "socks5://from-env:1080"


class TestHomeChannelEnvOverrides:
    """Current runtime home channel env vars should apply to existing config."""

    def test_existing_platform_configs_accept_home_channel_env_overrides(self):
        cases = [
            (
                Platform.FEISHU,
                PlatformConfig(enabled=True, extra={"app_id": "cli_current"}),
                {
                    "FEISHU_HOME_CHANNEL": "oc_123",
                    "FEISHU_HOME_CHANNEL_NAME": "Ops",
                    "FEISHU_HOME_CHANNEL_THREAD_ID": "omt_456",
                },
                ("oc_123", "Ops", "omt_456"),
            ),
        ]

        for platform, platform_config, env, expected in cases:
            config = GatewayConfig(platforms={platform: platform_config})
            with patch.dict(os.environ, env, clear=True):
                _apply_env_overrides(config)

            home = config.platforms[platform].home_channel
            assert home is not None, f"{platform.value}: home_channel should not be None"
            assert (home.chat_id, home.name, home.thread_id) == expected, platform.value

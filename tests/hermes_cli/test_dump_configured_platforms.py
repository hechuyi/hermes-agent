def test_configured_platforms_reports_feishu_headless_surfaces(monkeypatch):
    """The dump command reports only the Feishu/headless runtime surface."""
    from hermes_cli import dump

    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")

    assert dump._configured_platforms() == ["feishu", "api_server", "webhook"]


def test_configured_platforms_ignores_legacy_messaging_env(monkeypatch):
    """Legacy platform credentials do not make the Feishu fork look multi-platform."""
    from hermes_cli import dump

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENABLED", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)

    assert dump._configured_platforms() == []

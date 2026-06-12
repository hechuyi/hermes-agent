"""Tests for CLI max_tokens propagation into AIAgent runtime."""


def test_resolve_cli_max_tokens_reads_config(monkeypatch):
    import cli

    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)

    assert cli._resolve_cli_max_tokens({"max_tokens": 16384}) == 16384


def test_resolve_cli_max_tokens_env_override_wins(monkeypatch):
    import cli

    monkeypatch.setenv("HERMES_MAX_TOKENS", "2048")

    assert cli._resolve_cli_max_tokens({"max_tokens": 16384}) == 2048


def test_resolve_cli_max_tokens_invalid_env_disables_override(monkeypatch):
    import cli

    monkeypatch.setenv("HERMES_MAX_TOKENS", "not-an-int")

    assert cli._resolve_cli_max_tokens({"max_tokens": 16384}) is None


def test_turn_agent_config_carries_max_tokens():
    import cli

    app = object.__new__(cli.HermesCLI)
    app.api_key = "key"
    app.base_url = "https://example.com/v1"
    app.provider = "custom"
    app.api_mode = "chat_completions"
    app.acp_command = None
    app.acp_args = []
    app._credential_pool = None
    app.max_tokens = 4096
    app.model = "gpt-5.4"
    app.service_tier = None

    route = cli.HermesCLI._resolve_turn_agent_config(app, "hi")

    assert route["runtime"]["max_tokens"] == 4096
    assert 4096 in route["signature"]

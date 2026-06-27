"""Legacy IRC setup exclusion tests for the Feishu runtime fork.

The gateway setup CLI no longer discovers plugin messaging platforms for the
old multi-platform setup menu.  IRC can still exist in the runtime registry for
other code paths, but setup must remain Feishu-only.
"""

from gateway.platform_registry import PlatformEntry, platform_registry


def _register_irc_platform(**overrides):
    """Register an IRC-shaped legacy platform entry for exclusion tests."""
    defaults = dict(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        validate_config=None,
        required_env=["IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME"],
        install_hint="No extra packages needed",
        setup_fn=lambda: None,
        source="plugin",
        plugin_name="irc_platform",
        allowed_users_env="IRC_ALLOWED_USERS",
        allow_all_env="IRC_ALLOW_ALL_USERS",
        max_message_length=450,
        pii_safe=False,
        emoji="IRC",
        allow_update_command=True,
        platform_hint="You are chatting via IRC.",
    )
    defaults.update(overrides)
    entry = PlatformEntry(**defaults)
    platform_registry.register(entry)
    return {
        "key": entry.name,
        "label": entry.label,
        "emoji": entry.emoji,
        "token_var": entry.required_env[0] if entry.required_env else "",
        "install_hint": entry.install_hint,
        "_registry_entry": entry,
    }


def _unregister_irc_platform():
    platform_registry.unregister("irc")


def test_all_platforms_ignores_registered_irc_plugin():
    import hermes_cli.gateway as gateway_mod

    _register_irc_platform()
    try:
        platforms = gateway_mod._all_platforms()
    finally:
        _unregister_irc_platform()

    assert [p["key"] for p in platforms] == ["feishu"]
    assert all(p["key"] != "irc" for p in platforms)


def test_irc_env_does_not_count_as_configured_platform(monkeypatch):
    import hermes_cli.gateway as gateway_mod

    plat = _register_irc_platform()
    try:
        monkeypatch.setenv("IRC_SERVER", "irc.libera.chat")
        monkeypatch.setenv("IRC_CHANNEL", "#hermes")
        monkeypatch.setenv("IRC_NICKNAME", "hermes-bot")

        assert gateway_mod._platform_status(plat) == "not configured"
    finally:
        _unregister_irc_platform()


def test_configure_platform_rejects_irc_and_does_not_call_setup_fn(capsys):
    import hermes_cli.gateway as gateway_mod

    calls = []

    def fake_setup():
        calls.append("called")

    plat = _register_irc_platform(setup_fn=fake_setup)
    try:
        gateway_mod._configure_platform(plat)
    finally:
        _unregister_irc_platform()

    out = capsys.readouterr().out
    assert calls == []
    assert "Unsupported platform" in out
    assert "irc" in out


def test_setup_gateway_checklist_contains_only_feishu_when_irc_registered(monkeypatch):
    import hermes_cli.gateway as gateway_mod
    from hermes_cli import setup as setup_mod

    captured = []

    def capture_prompt_checklist(question, choices, pre_selected=None):
        captured.append({"question": question, "choices": choices})
        return []

    _register_irc_platform()
    try:
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        monkeypatch.setattr(setup_mod, "prompt_checklist", capture_prompt_checklist)
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(gateway_mod, "prompt_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

        setup_mod.setup_gateway({})
    finally:
        _unregister_irc_platform()

    assert captured
    choices_text = "\n".join(captured[0]["choices"])
    assert "Feishu / Lark" in choices_text
    assert "IRC" not in choices_text


def test_gateway_setup_menu_contains_only_feishu_when_irc_registered(monkeypatch):
    import hermes_cli.gateway as gateway_mod

    captured = []

    def capture_prompt_choice(question, choices, default=0):
        captured.append({"question": question, "choices": choices})
        return len(choices) - 1

    _register_irc_platform()
    try:
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        monkeypatch.setattr(gateway_mod, "prompt_choice", capture_prompt_choice)
        monkeypatch.setattr(gateway_mod, "prompt_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

        gateway_mod.gateway_setup()
    finally:
        _unregister_irc_platform()

    assert captured
    choices_text = "\n".join(captured[0]["choices"])
    assert "Feishu / Lark" in choices_text
    assert "Done" in choices_text
    assert "IRC" not in choices_text


def test_feishu_setup_dispatch_remains_available(monkeypatch):
    import hermes_cli.gateway as gateway_mod

    calls = []
    monkeypatch.setattr(gateway_mod, "_setup_feishu", lambda: calls.append("feishu"))

    gateway_mod._configure_platform({"key": "feishu"})

    assert calls == ["feishu"]

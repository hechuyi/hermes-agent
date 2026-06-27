"""Help-text regression tests for the Feishu/headless Hermes CLI surface."""

from __future__ import annotations

import sys

import pytest


LEGACY_CHAT_PLATFORM_TERMS = (
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "signal",
)


@pytest.fixture
def capture_help(monkeypatch, capsys):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_set_process_title", lambda: None)
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)

    def _capture(*argv: str) -> str:
        monkeypatch.setattr(sys, "argv", ["hermes", *argv, "--help"])
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        assert exc.value.code == 0
        return capsys.readouterr().out.lower()

    return _capture


@pytest.mark.parametrize(
    "argv",
    [
        ("cron", "create"),
        ("webhook", "subscribe"),
        ("pairing", "approve"),
        ("sessions", "list"),
        ("sessions", "browse"),
        ("insights",),
        ("prompt-size",),
    ],
)
def test_headless_help_omits_legacy_chat_platform_examples(capture_help, argv):
    help_text = capture_help(*argv)

    for term in LEGACY_CHAT_PLATFORM_TERMS:
        assert term not in help_text


def test_headless_help_keeps_current_runtime_examples(capture_help):
    assert "feishu" in capture_help("prompt-size")
    assert "origin" in capture_help("cron", "create")
    assert "local" in capture_help("cron", "create")
    assert "log" in capture_help("webhook", "subscribe")

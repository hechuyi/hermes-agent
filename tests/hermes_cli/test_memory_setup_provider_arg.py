"""Tests for direct `hermes memory setup <provider>` routing."""

from types import SimpleNamespace


def test_memory_setup_provider_routes_directly(monkeypatch):
    from hermes_cli import memory_setup

    calls: list[str] = []

    monkeypatch.setattr(memory_setup, "cmd_setup_provider", lambda provider: calls.append(provider))
    monkeypatch.setattr(
        memory_setup,
        "cmd_setup",
        lambda args: (_ for _ in ()).throw(AssertionError("picker should be skipped")),
    )

    memory_setup.memory_command(
        SimpleNamespace(memory_command="setup", provider="honcho")
    )

    assert calls == ["honcho"]

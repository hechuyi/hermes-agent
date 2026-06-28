"""Integration tests for terminal_tool's Docker orphan-reaper wiring."""

from unittest.mock import patch

import tools.terminal_tool as terminal_tool


def _reset_reaper_gate():
    terminal_tool._docker_orphan_reaper_ran = False


def test_maybe_reap_runs_once_per_process():
    _reset_reaper_gate()
    call_count = {"reap": 0}

    def _fake_reap(**kwargs):
        call_count["reap"] += 1
        return 0

    with patch("tools.environments.docker.reap_orphan_containers", _fake_reap):
        config = {"docker_orphan_reaper": True}
        terminal_tool._maybe_reap_docker_orphans(config)
        terminal_tool._maybe_reap_docker_orphans(config)
        terminal_tool._maybe_reap_docker_orphans(config)

    assert call_count["reap"] == 1


def test_maybe_reap_respects_disable_flag():
    _reset_reaper_gate()
    call_count = {"reap": 0}

    def _fake_reap(**kwargs):
        call_count["reap"] += 1
        return 0

    with patch("tools.environments.docker.reap_orphan_containers", _fake_reap):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": False})

    assert call_count["reap"] == 0
    assert terminal_tool._docker_orphan_reaper_ran is False


def test_maybe_reap_doubles_lifetime_for_max_age(monkeypatch):
    _reset_reaper_gate()
    captured_args = {}

    def _fake_reap(**kwargs):
        captured_args.update(kwargs)
        return 0

    monkeypatch.setenv("TERMINAL_LIFETIME_SECONDS", "300")
    with patch("tools.environments.docker.reap_orphan_containers", _fake_reap):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": True})

    assert captured_args.get("max_age_seconds") == 600


def test_maybe_reap_floors_at_60_seconds(monkeypatch):
    _reset_reaper_gate()
    captured_args = {}

    def _fake_reap(**kwargs):
        captured_args.update(kwargs)
        return 0

    monkeypatch.setenv("TERMINAL_LIFETIME_SECONDS", "0")
    with patch("tools.environments.docker.reap_orphan_containers", _fake_reap):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": True})

    assert captured_args.get("max_age_seconds") == 120


def test_maybe_reap_passes_current_profile_as_filter():
    _reset_reaper_gate()
    captured_args = {}

    def _fake_reap(**kwargs):
        captured_args.update(kwargs)
        return 0

    with (
        patch("tools.environments.docker.reap_orphan_containers", _fake_reap),
        patch("tools.environments.docker._get_active_profile_name", return_value="research-bot"),
    ):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": True})

    assert captured_args.get("profile_filter") == "research-bot"


def test_maybe_reap_swallows_exceptions():
    _reset_reaper_gate()

    def _exploding_reap(**kwargs):
        raise RuntimeError("docker daemon unavailable")

    with patch("tools.environments.docker.reap_orphan_containers", _exploding_reap):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": True})

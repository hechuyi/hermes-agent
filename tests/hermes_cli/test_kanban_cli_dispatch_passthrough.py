"""Regression tests for Kanban CLI dispatch configuration passthrough."""

from __future__ import annotations

import argparse
from pathlib import Path


def test_cli_dispatch_passes_config_caps_and_default_assignee(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban as kanban_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "max_spawn": 7,
                "max_in_progress": 3,
                "default_assignee": "default",
                "max_in_progress_per_profile": 2,
            }
        },
    )

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    assert kanban_cli._cmd_dispatch(args) == 0

    assert captured["max_spawn"] == 7
    assert captured["max_in_progress"] == 3
    assert captured["default_assignee"] == "default"
    assert captured["max_in_progress_per_profile"] == 2


def test_cli_max_flag_overrides_config_max_spawn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban as kanban_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"max_spawn": 7}},
    )

    captured = {}
    monkeypatch.setattr(
        kanban_db,
        "dispatch_once",
        lambda conn, **kwargs: (
            captured.update(kwargs),
            kanban_db.DispatchResult(),
        )[1],
    )

    args = argparse.Namespace(dry_run=True, max=2, failure_limit=2, json=False)
    assert kanban_cli._cmd_dispatch(args) == 0

    assert captured["max_spawn"] == 2


def test_cli_dispatch_invalid_config_caps_become_none(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban as kanban_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "max_spawn": "abc",
                "max_in_progress": 0,
                "max_in_progress_per_profile": -1,
            }
        },
    )

    captured = {}
    monkeypatch.setattr(
        kanban_db,
        "dispatch_once",
        lambda conn, **kwargs: (
            captured.update(kwargs),
            kanban_db.DispatchResult(),
        )[1],
    )

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    assert kanban_cli._cmd_dispatch(args) == 0

    assert captured["max_spawn"] is None
    assert captured["max_in_progress"] is None
    assert captured["max_in_progress_per_profile"] is None


def test_kanban_swarm_uses_bundled_humanizer_skill():
    repo_root = Path(__file__).resolve().parents[2]
    swarm_source = (repo_root / "hermes_cli" / "kanban_swarm.py").read_text()

    assert "avoid-ai-writing" not in swarm_source
    assert 'skills=["humanizer"]' in swarm_source
    assert (repo_root / "skills" / "creative" / "humanizer" / "SKILL.md").is_file()

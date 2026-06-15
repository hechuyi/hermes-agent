from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dispatch


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_dispatch_payload_passes_config_caps(monkeypatch, kanban_home):
    conn = kb.connect()
    captured = {}

    def fake_dispatch_once(conn_arg, **kwargs):
        assert conn_arg is conn
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)

    payload = kanban_dispatch.dispatch_payload(
        conn,
        dry_run=True,
        max_spawn=4,
        config_loader=lambda: {
            "kanban": {
                "max_in_progress": 3,
                "default_assignee": "default",
                "max_in_progress_per_profile": 2,
            }
        },
    )

    assert payload["spawned"] == []
    assert captured["dry_run"] is True
    assert captured["max_spawn"] == 4
    assert captured["max_in_progress"] == 3
    assert captured["default_assignee"] == "default"
    assert captured["max_in_progress_per_profile"] == 2


def test_dispatch_payload_ignores_invalid_caps(monkeypatch, kanban_home):
    conn = kb.connect()
    captured = {}

    def fake_dispatch_once(conn_arg, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)

    kanban_dispatch.dispatch_payload(
        conn,
        max_spawn=2,
        config_loader=lambda: {
            "kanban": {
                "max_in_progress": 0,
                "max_in_progress_per_profile": -1,
            }
        },
    )

    assert captured["max_in_progress"] is None
    assert captured["max_in_progress_per_profile"] is None

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import main as main_mod


def test_tui_flag_exits_without_launching_node_tui(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

    assert not hasattr(main_mod, "_launch_tui")

    args = SimpleNamespace(
        tui=True,
        tui_dev=False,
        continue_last=None,
        resume=None,
        model=None,
        provider=None,
        toolsets=None,
        skills=None,
        verbose=False,
        quiet=False,
        query=None,
        image=None,
        worktree=False,
        checkpoints=False,
        pass_session_id=False,
        max_turns=None,
        accept_hooks=False,
        yolo=False,
        ignore_user_config=False,
        ignore_rules=False,
        source=None,
        compact=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.cmd_chat(args)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "TUI is not available in this Feishu runtime fork" in captured.err

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_board_view
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_board_payload_includes_rollups_and_columns(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.add_comment(conn, parent, author="tester", body="note")

        payload = kanban_board_view.board_payload(conn)

        ready = next(c for c in payload["columns"] if c["name"] == "ready")
        parent_card = next(t for t in ready["tasks"] if t["id"] == parent)
        assert parent_card["link_counts"] == {"parents": 0, "children": 1}
        assert parent_card["comment_count"] == 1
        assert parent_card["progress"] == {"done": 0, "total": 1}

        todo = next(c for c in payload["columns"] if c["name"] == "todo")
        assert any(t["id"] == child for t in todo["tasks"])
        assert "latest_event_id" in payload
        assert "now" in payload
    finally:
        conn.close()


def test_task_detail_payload_includes_related_rows(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.add_comment(conn, child, author="tester", body="note")

        payload = kanban_board_view.task_detail_payload(conn, child)

        assert payload["task"]["id"] == child
        assert parent in payload["links"]["parents"]
        assert payload["comments"][0]["body"] == "note"
        assert payload["events"]
        assert payload["attachments"] == []
        assert payload["runs"] == []
    finally:
        conn.close()


def test_task_detail_payload_rejects_invalid_run_filter(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="task")

        with pytest.raises(ValueError, match="run_state_type"):
            kanban_board_view.task_detail_payload(
                conn,
                task_id,
                run_state_type="bad",
                run_state_name="x",
            )
    finally:
        conn.close()

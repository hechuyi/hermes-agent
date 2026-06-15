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


def test_task_log_payload_reports_tail_and_missing_task(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="logged")
    finally:
        conn.close()

    log_path = kb.worker_log_path(task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line 1\nline 2\n")

    payload = kanban_board_view.task_log_payload(task_id, tail=8)
    assert payload["task_id"] == task_id
    assert payload["exists"] is True
    assert payload["content"] == "line 2\n"
    assert payload["truncated"] is True

    with pytest.raises(LookupError):
        kanban_board_view.task_log_payload("missing")


def test_stats_and_assignees_payloads(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ready", assignee="worker")

        stats = kanban_board_view.stats_payload(conn)
        assignees = kanban_board_view.assignees_payload(conn)

        assert stats["by_status"]["ready"] == 1
        by_name = {item["name"]: item for item in assignees["assignees"]}
        assert by_name["worker"]["counts"] == {"ready": 1}
        assert kb.get_task(conn, task_id).assignee == "worker"
    finally:
        conn.close()


def test_board_registry_payload_includes_counts(kanban_home):
    kb.create_board("proj")
    conn = kb.connect(board="proj")
    try:
        kb.create_task(conn, title="work")
    finally:
        conn.close()

    payload = kanban_board_view.boards_payload()
    by_slug = {board["slug"]: board for board in payload["boards"]}
    assert by_slug["proj"]["counts"] == {"ready": 1}
    assert by_slug["proj"]["total"] == 1
    assert payload["current"]


def test_board_crud_helpers_create_rename_switch_and_delete(kanban_home):
    created = kanban_board_view.create_board_payload(
        "ops",
        name="Ops",
        switch=True,
    )
    assert created["board"]["slug"] == "ops"
    assert created["current"] == "ops"

    renamed = kanban_board_view.update_board_payload("ops", description="work")
    assert renamed["board"]["description"] == "work"

    switched = kanban_board_view.switch_board_payload("default")
    assert switched == {"current": "default"}

    deleted = kanban_board_view.delete_board_payload("ops", delete=True)
    assert deleted["current"] == "default"

    with pytest.raises(LookupError):
        kanban_board_view.update_board_payload("missing", name="Missing")

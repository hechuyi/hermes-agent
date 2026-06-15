from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_tasks


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_update_task_completes_with_summary_and_metadata(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ship")

        updated = kanban_tasks.update_task(
            conn,
            task_id,
            status="done",
            result="DECIDED",
            summary="DECIDED",
            metadata={"source": "core"},
        )

        run = kb.latest_run(conn, task_id)
        assert updated.status == "done"
        assert updated.result == "DECIDED"
        assert run.summary == "DECIDED"
        assert run.metadata == {"source": "core"}
    finally:
        conn.close()


def test_update_task_ready_reports_blocking_parents(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])

        with pytest.raises(kanban_tasks.TaskUpdateError) as exc:
            kanban_tasks.update_task(conn, child, status="ready")

        assert exc.value.status_code == 409
        assert "Cannot move to 'ready'" in exc.value.detail
        assert parent in exc.value.detail
        assert "'parent'" in exc.value.detail
    finally:
        conn.close()


def test_update_task_rejects_running_status(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="x")

        with pytest.raises(kanban_tasks.TaskUpdateError) as exc:
            kanban_tasks.update_task(conn, task_id, status="running")

        assert exc.value.status_code == 400
        assert "running" in exc.value.detail
        assert kb.get_task(conn, task_id).status != "running"
    finally:
        conn.close()


def test_update_task_rejects_empty_title(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="x")

        with pytest.raises(kanban_tasks.TaskUpdateError) as exc:
            kanban_tasks.update_task(conn, task_id, title="   ")

        assert exc.value.status_code == 400
        assert exc.value.detail == "title cannot be empty"
    finally:
        conn.close()


def test_delete_task_removes_existing_task(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="gone")

        assert kanban_tasks.delete_task(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None
    finally:
        conn.close()


def test_add_task_comment_validates_body_and_task(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="commented")

        with pytest.raises(kanban_tasks.TaskUpdateError) as exc:
            kanban_tasks.add_task_comment(conn, task_id, body="   ")
        assert exc.value.status_code == 400

        with pytest.raises(kanban_tasks.TaskUpdateError) as exc:
            kanban_tasks.add_task_comment(conn, "missing", body="hello")
        assert exc.value.status_code == 404

        kanban_tasks.add_task_comment(conn, task_id, body="hello", author=None)
        comments = kb.list_comments(conn, task_id)
        assert comments[0].body == "hello"
        assert comments[0].author == "dashboard"
    finally:
        conn.close()


def test_link_task_helpers_create_and_remove_links(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")

        kanban_tasks.add_task_link(conn, parent, child)
        assert parent in kanban_tasks.task_links(conn, child)["parents"]
        assert child in kanban_tasks.task_links(conn, parent)["children"]

        assert kanban_tasks.delete_task_link(conn, parent, child) is True
        assert parent not in kanban_tasks.task_links(conn, child)["parents"]
    finally:
        conn.close()


def test_create_task_payload_includes_dispatcher_warning_for_ready_assigned(kanban_home):
    conn = kb.connect()
    try:
        payload = kanban_tasks.create_task_payload(
            conn,
            title="warn",
            assignee="worker",
            dispatcher_probe=lambda: (False, "gateway is down"),
        )

        assert payload["task"]["title"] == "warn"
        assert payload["warning"] == "gateway is down"
    finally:
        conn.close()


def test_create_task_payload_skips_warning_for_triage(kanban_home):
    conn = kb.connect()
    try:
        payload = kanban_tasks.create_task_payload(
            conn,
            title="triage",
            assignee="worker",
            triage=True,
            dispatcher_probe=lambda: (False, "gateway is down"),
        )

        assert "warning" not in payload
        assert payload["task"]["status"] == "triage"
    finally:
        conn.close()

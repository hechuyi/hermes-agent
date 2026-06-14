from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_bulk


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_bulk_update_tasks_rejects_empty_ids(kanban_home):
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="ids is required"):
            kanban_bulk.bulk_update_tasks(conn, ids=[])
    finally:
        conn.close()


def test_bulk_update_tasks_partial_failure_does_not_abort_siblings(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        c = kb.create_task(conn, title="c")

        results = kanban_bulk.bulk_update_tasks(
            conn,
            ids=[a, "bogus-id", c],
            priority=7,
        )

        assert len(results) == 3
        assert {r["id"] for r in results if r["ok"]} == {a, c}
        assert any(r["id"] == "bogus-id" and not r["ok"] for r in results)
        assert kb.get_task(conn, a).priority == 7
        assert kb.get_task(conn, c).priority == 7
    finally:
        conn.close()


def test_bulk_update_tasks_done_forwards_completion_summary(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")

        results = kanban_bulk.bulk_update_tasks(
            conn,
            ids=[a, b],
            status="done",
            result="DECIDED: ship it",
            summary="DECIDED: ship it",
            metadata={"source": "core"},
        )

        assert all(r["ok"] for r in results)
        for task_id in (a, b):
            task = kb.get_task(conn, task_id)
            run = kb.latest_run(conn, task_id)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "core"}
    finally:
        conn.close()


def test_bulk_update_tasks_rejects_running_status(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="x")

        results = kanban_bulk.bulk_update_tasks(conn, ids=[task_id], status="running")

        assert results == [
            {
                "id": task_id,
                "ok": False,
                "error": (
                    "Cannot set status to 'running' directly; "
                    "use the dispatcher/claim path"
                ),
            }
        ]
        assert kb.get_task(conn, task_id).status != "running"
    finally:
        conn.close()

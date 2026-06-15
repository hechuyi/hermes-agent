"""Core kanban task mutation helpers."""

from __future__ import annotations

import json
import time
from typing import Optional

from hermes_cli import kanban_db


class TaskUpdateError(Exception):
    """Typed task-update failure for HTTP/CLI adapters to map cleanly."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _raise_blocking_parents(conn, task_id: str) -> None:
    blockers = kanban_db.parents_blocking_ready(conn, task_id)
    if not blockers:
        return
    names = ", ".join(
        f"{parent['title']!r} ({parent['id']}, status={parent['status']})"
        for parent in blockers
    )
    raise TaskUpdateError(
        409,
        f"Cannot move to 'ready': blocked by parent(s) not done — {names}",
    )


def update_task(
    conn,
    task_id: str,
    *,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    priority: Optional[int] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    result: Optional[str] = None,
    block_reason: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
):
    """Apply a dashboard-style single-task patch and return the updated task."""
    task = kanban_db.get_task(conn, task_id)
    if task is None:
        raise TaskUpdateError(404, f"task {task_id} not found")

    if assignee is not None:
        try:
            ok = kanban_db.assign_task(conn, task_id, assignee or None)
        except RuntimeError as exc:
            raise TaskUpdateError(409, str(exc)) from exc
        if not ok:
            raise TaskUpdateError(404, "task not found")

    if status is not None:
        ok = True
        if status == "done":
            ok = kanban_db.complete_task(
                conn,
                task_id,
                result=result,
                summary=summary,
                metadata=metadata,
            )
        elif status == "blocked":
            ok = kanban_db.block_task(conn, task_id, reason=block_reason)
        elif status == "scheduled":
            ok = kanban_db.schedule_task(conn, task_id, reason=block_reason)
        elif status == "ready":
            current = kanban_db.get_task(conn, task_id)
            if current and current.status in ("blocked", "scheduled"):
                ok = kanban_db.unblock_task(conn, task_id)
            else:
                ok = kanban_db.set_status_direct(conn, task_id, "ready")
        elif status == "archived":
            ok = kanban_db.archive_task(conn, task_id)
        elif status == "running":
            raise TaskUpdateError(
                400,
                "Cannot set status to 'running' directly; use the dispatcher/claim path",
            )
        elif status in ("todo", "triage"):
            ok = kanban_db.set_status_direct(conn, task_id, status)
        else:
            raise TaskUpdateError(400, f"unknown status: {status}")
        if not ok:
            if status == "ready":
                _raise_blocking_parents(conn, task_id)
            raise TaskUpdateError(
                409,
                f"status transition to {status!r} not valid from current state",
            )

    if priority is not None:
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET priority = ? WHERE id = ?",
                (int(priority), task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'reprioritized', ?, ?)",
                (
                    task_id,
                    json.dumps({"priority": int(priority)}),
                    int(time.time()),
                ),
            )

    if title is not None or body is not None:
        with kanban_db.write_txn(conn):
            sets, vals = [], []
            if title is not None:
                if not title.strip():
                    raise TaskUpdateError(400, "title cannot be empty")
                sets.append("title = ?")
                vals.append(title.strip())
            if body is not None:
                sets.append("body = ?")
                vals.append(body)
            vals.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'edited', NULL, ?)",
                (task_id, int(time.time())),
            )

    return kanban_db.get_task(conn, task_id)


def delete_task(conn, task_id: str, *, board: Optional[str] = None) -> bool:
    """Delete a task and its dependent rows."""
    return bool(kanban_db.delete_task(conn, task_id, board=board))


def add_task_comment(
    conn,
    task_id: str,
    *,
    body: str,
    author: Optional[str] = "dashboard",
) -> None:
    """Add a task comment after validating task existence and body."""
    if not body.strip():
        raise TaskUpdateError(400, "body is required")
    if kanban_db.get_task(conn, task_id) is None:
        raise TaskUpdateError(404, f"task {task_id} not found")
    kanban_db.add_comment(
        conn,
        task_id,
        author=author or "dashboard",
        body=body,
    )


def task_links(conn, task_id: str) -> dict[str, list[str]]:
    """Return parent and child task ids for a task."""
    parents = [
        row["parent_id"]
        for row in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        )
    ]
    children = [
        row["child_id"]
        for row in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
            (task_id,),
        )
    ]
    return {"parents": parents, "children": children}


def add_task_link(conn, parent_id: str, child_id: str) -> None:
    """Link two tasks, preserving ``kanban_db.link_tasks`` validation."""
    kanban_db.link_tasks(conn, parent_id, child_id)


def delete_task_link(conn, parent_id: str, child_id: str) -> bool:
    """Remove a task link if present."""
    return bool(kanban_db.unlink_tasks(conn, parent_id, child_id))

"""Core read models for kanban board and task detail views."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Optional

from hermes_cli import kanban_db
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_tasks
from hermes_cli import kanban_workers


BOARD_COLUMNS: list[str] = [
    "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
]

CARD_SUMMARY_PREVIEW_CHARS = 200


def task_payload(
    task: kanban_db.Task,
    *,
    latest_summary: Optional[str] = None,
) -> dict[str, Any]:
    payload = asdict(task)
    try:
        payload["age"] = kanban_db.task_age(task)
    except Exception:
        payload["age"] = {
            "created_age_seconds": None,
            "started_age_seconds": None,
            "time_to_complete_seconds": None,
        }
    payload["latest_summary"] = latest_summary
    return payload


def event_payload(event: kanban_db.Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
        "run_id": event.run_id,
    }


def comment_payload(comment: kanban_db.Comment) -> dict[str, Any]:
    return {
        "id": comment.id,
        "task_id": comment.task_id,
        "author": comment.author,
        "body": comment.body,
        "created_at": comment.created_at,
    }


def attachment_payload(attachment: kanban_db.Attachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "task_id": attachment.task_id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "uploaded_by": attachment.uploaded_by,
        "stored_path": attachment.stored_path,
        "created_at": attachment.created_at,
    }


def board_payload(
    conn,
    *,
    tenant: Optional[str] = None,
    include_archived: bool = False,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> dict:
    tasks = kanban_db.list_tasks(
        conn,
        tenant=tenant,
        include_archived=include_archived,
        workflow_template_id=workflow_template_id,
        current_step_key=current_step_key,
    )

    link_counts: dict[str, dict[str, int]] = {}
    for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
        link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})[
            "children"
        ] += 1
        link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})[
            "parents"
        ] += 1

    comment_counts: dict[str, int] = {
        row["task_id"]: row["n"]
        for row in conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
        )
    }

    progress: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT l.parent_id AS pid, t.status AS cstatus "
        "FROM task_links l JOIN tasks t ON t.id = l.child_id"
    ).fetchall():
        item = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        item["total"] += 1
        if row["cstatus"] == "done":
            item["done"] += 1

    diagnostics_per_task = kd.compute_task_diagnostics_by_task(conn, task_ids=None)
    latest_event_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
    ).fetchone()["m"]

    columns: dict[str, list[dict]] = {column: [] for column in BOARD_COLUMNS}
    if include_archived:
        columns["archived"] = []

    summary_map = kanban_db.latest_summaries(conn, [task.id for task in tasks])
    for task in tasks:
        full_summary = summary_map.get(task.id)
        preview = (
            full_summary[:CARD_SUMMARY_PREVIEW_CHARS] if full_summary else None
        )
        item = task_payload(task, latest_summary=preview)
        item["link_counts"] = link_counts.get(task.id, {"parents": 0, "children": 0})
        item["comment_count"] = comment_counts.get(task.id, 0)
        item["progress"] = progress.get(task.id)
        diagnostics = diagnostics_per_task.get(task.id)
        if diagnostics:
            item["diagnostics"] = diagnostics
            item["warnings"] = kd.warnings_summary_from_diagnostics(diagnostics)
        column = task.status if task.status in columns else "todo"
        columns[column].append(item)

    tenants = [
        row["tenant"]
        for row in conn.execute(
            "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
        )
    ]
    assignees = [
        row["assignee"]
        for row in conn.execute(
            "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL "
            "AND status != 'archived' ORDER BY assignee"
        )
    ]

    return {
        "columns": [
            {"name": name, "tasks": columns[name]} for name in columns.keys()
        ],
        "tenants": tenants,
        "assignees": assignees,
        "latest_event_id": int(latest_event_id),
        "now": int(time.time()),
    }


def task_detail_payload(
    conn,
    task_id: str,
    *,
    run_state_type: Optional[str] = None,
    run_state_name: Optional[str] = None,
) -> dict:
    if (run_state_type is None) ^ (run_state_name is None):
        raise ValueError("run_state_type and run_state_name must be passed together or omitted")
    if run_state_type is not None and run_state_type not in ("status", "outcome"):
        raise ValueError("run_state_type must be 'status' or 'outcome'")

    task = kanban_db.get_task(conn, task_id)
    if task is None:
        raise LookupError(f"task {task_id} not found")

    task_item = task_payload(task, latest_summary=kanban_db.latest_summary(conn, task_id))
    diagnostics = kd.compute_task_diagnostics_by_task(conn, task_ids=[task_id])
    diagnostic_list = diagnostics.get(task_id) or []
    if diagnostic_list:
        task_item["diagnostics"] = diagnostic_list
        task_item["warnings"] = kd.warnings_summary_from_diagnostics(diagnostic_list)

    return {
        "task": task_item,
        "comments": [
            comment_payload(comment)
            for comment in kanban_db.list_comments(conn, task_id)
        ],
        "events": [
            event_payload(event)
            for event in kanban_db.list_events(conn, task_id)
        ],
        "attachments": [
            attachment_payload(attachment)
            for attachment in kanban_db.list_attachments(conn, task_id)
        ],
        "links": kanban_tasks.task_links(conn, task_id),
        "runs": [
            kanban_workers.run_to_payload(run)
            for run in kanban_db.list_runs(
                conn,
                task_id,
                state_type=run_state_type,
                state_name=run_state_name,
            )
        ],
    }


def stats_payload(conn) -> dict:
    """Return board status/assignee stats."""
    return kanban_db.board_stats(conn)


def assignees_payload(conn) -> dict:
    """Return known assignees wrapped for the dashboard API."""
    return {"assignees": kanban_db.known_assignees(conn)}


def task_log_payload(
    task_id: str,
    *,
    tail: Optional[int] = None,
    board: Optional[str] = None,
) -> dict:
    """Return worker log metadata and content for a task."""
    with kanban_db.connect_closing(board=board) as conn:
        if kanban_db.get_task(conn, task_id) is None:
            raise LookupError(f"task {task_id} not found")

    content = kanban_db.read_worker_log(task_id, tail_bytes=tail, board=board)
    log_path = kanban_db.worker_log_path(task_id, board=board)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id,
        "path": str(log_path),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        "truncated": bool(tail and size > tail),
    }

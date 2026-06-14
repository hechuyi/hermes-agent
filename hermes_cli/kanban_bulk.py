"""Core kanban bulk task mutation helpers."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from hermes_cli import kanban_db


def bulk_update_tasks(
    conn,
    *,
    ids: list[str],
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    priority: Optional[int] = None,
    archive: bool = False,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    reclaim_first: bool = False,
) -> list[dict]:
    """Apply one bulk patch independently to each task id.

    Per-task failures are represented in the returned result list and do not
    abort later siblings.
    """
    task_ids = [task_id for task_id in (ids or []) if task_id]
    if not task_ids:
        raise ValueError("ids is required")

    results: list[dict] = []
    for task_id in task_ids:
        entry: dict[str, Any] = {"id": task_id, "ok": True}
        try:
            task = kanban_db.get_task(conn, task_id)
            if task is None:
                entry.update(ok=False, error="not found")
                results.append(entry)
                continue

            if archive:
                if not kanban_db.archive_task(conn, task_id):
                    entry.update(ok=False, error="archive refused")

            if status is not None and not archive:
                if status == "done":
                    ok = kanban_db.complete_task(
                        conn,
                        task_id,
                        result=result,
                        summary=summary,
                        metadata=metadata,
                    )
                elif status == "blocked":
                    ok = kanban_db.block_task(conn, task_id)
                elif status == "ready":
                    current = kanban_db.get_task(conn, task_id)
                    if current and current.status in ("blocked", "scheduled"):
                        ok = kanban_db.unblock_task(conn, task_id)
                    else:
                        ok = kanban_db.set_status_direct(conn, task_id, "ready")
                elif status == "running":
                    entry.update(
                        ok=False,
                        error=(
                            "Cannot set status to 'running' directly; "
                            "use the dispatcher/claim path"
                        ),
                    )
                    results.append(entry)
                    continue
                elif status == "scheduled":
                    ok = kanban_db.schedule_task(conn, task_id)
                elif status in {"todo", "triage"}:
                    ok = kanban_db.set_status_direct(conn, task_id, status)
                else:
                    entry.update(ok=False, error=f"unknown status {status!r}")
                    results.append(entry)
                    continue
                if not ok:
                    entry.update(ok=False, error=f"transition to {status!r} refused")

            if assignee is not None:
                try:
                    if reclaim_first:
                        ok = kanban_db.reassign_task(
                            conn,
                            task_id,
                            assignee or None,
                            reclaim_first=True,
                        )
                    else:
                        ok = kanban_db.assign_task(conn, task_id, assignee or None)
                    if not ok:
                        entry.update(ok=False, error="assign refused")
                except RuntimeError as exc:
                    entry.update(ok=False, error=str(exc))

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
        except Exception as exc:
            entry.update(ok=False, error=str(exc))
        results.append(entry)
    return results

"""Kanban worker/run service helpers."""

from __future__ import annotations

import time
from typing import Any

from hermes_cli import kanban_db


class KanbanWorkerError(Exception):
    """Base class for worker/run service failures."""


class RunNotFound(KanbanWorkerError):
    """The requested task run does not exist."""


class RunAlreadyEnded(KanbanWorkerError):
    """The requested task run is already closed."""


class RunNotReclaimable(KanbanWorkerError):
    """The run's task is no longer in a reclaimable state."""


def run_to_payload(r: kanban_db.Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "task_id": r.task_id,
        "profile": r.profile,
        "step_key": r.step_key,
        "status": r.status,
        "claim_lock": r.claim_lock,
        "claim_expires": r.claim_expires,
        "worker_pid": r.worker_pid,
        "max_runtime_seconds": r.max_runtime_seconds,
        "last_heartbeat_at": r.last_heartbeat_at,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "outcome": r.outcome,
        "summary": r.summary,
        "metadata": r.metadata,
        "error": r.error,
    }


def list_active_workers(conn) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            r.id          AS run_id,
            r.task_id,
            t.title       AS task_title,
            t.status      AS task_status,
            t.assignee    AS task_assignee,
            r.profile,
            r.worker_pid,
            r.started_at,
            r.claim_lock,
            r.claim_expires,
            r.last_heartbeat_at,
            r.max_runtime_seconds
        FROM task_runs r
        JOIN tasks t ON t.id = r.task_id
        WHERE r.ended_at IS NULL
          AND r.worker_pid IS NOT NULL
          AND t.status = 'running'
        ORDER BY r.started_at ASC
        """,
    ).fetchall()
    workers = [
        {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "task_title": row["task_title"],
            "task_status": row["task_status"],
            "task_assignee": row["task_assignee"],
            "profile": row["profile"],
            "worker_pid": row["worker_pid"],
            "started_at": row["started_at"],
            "claim_lock": row["claim_lock"],
            "claim_expires": row["claim_expires"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "max_runtime_seconds": row["max_runtime_seconds"],
        }
        for row in rows
    ]
    return {"workers": workers, "count": len(workers), "checked_at": int(time.time())}


def get_run_payload(conn, run_id: int) -> dict[str, Any]:
    r = kanban_db.get_run(conn, run_id)
    if r is None:
        raise RunNotFound(f"run {run_id} not found")
    return {"run": run_to_payload(r)}


def inspect_run(conn, run_id: int, *, psutil_module: Any = None) -> dict[str, Any]:
    r = kanban_db.get_run(conn, run_id)
    if r is None:
        raise RunNotFound(f"run {run_id} not found")

    if r.ended_at is not None:
        return {"run_id": run_id, "alive": False, "reason": "run already ended"}
    if r.worker_pid is None:
        return {"run_id": run_id, "alive": False, "reason": "no worker_pid recorded"}

    pid = r.worker_pid
    if psutil_module is None:
        try:
            import psutil as psutil_module  # type: ignore[import-not-found]
        except ImportError:
            psutil_module = None

    if psutil_module is None:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "psutil not available"}

    try:
        proc = psutil_module.Process(pid)
        info = proc.as_dict(attrs=[
            "cpu_percent", "memory_info", "num_threads",
            "status", "create_time", "cmdline",
        ])
        try:
            num_fds = proc.num_fds()
        except AttributeError:
            num_fds = None
        mem = info.get("memory_info")
        return {
            "run_id": run_id,
            "alive": True,
            "pid": pid,
            "cpu_percent": info.get("cpu_percent"),
            "memory_rss_bytes": mem.rss if mem else None,
            "memory_vms_bytes": mem.vms if mem else None,
            "num_threads": info.get("num_threads"),
            "num_fds": num_fds,
            "status": info.get("status"),
            "create_time": info.get("create_time"),
            "cmdline": info.get("cmdline"),
        }
    except psutil_module.NoSuchProcess:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "process not found"}
    except psutil_module.AccessDenied:
        return {"run_id": run_id, "alive": True, "pid": pid, "error": "access denied"}


def terminate_run(conn, run_id: int, *, reason: str | None = None) -> dict[str, Any]:
    r = kanban_db.get_run(conn, run_id)
    if r is None:
        raise RunNotFound(f"run {run_id} not found")
    if r.ended_at is not None:
        raise RunAlreadyEnded(f"run {run_id} already ended")
    ok = kanban_db.reclaim_task(conn, r.task_id, reason=reason)
    if not ok:
        raise RunNotReclaimable(
            f"cannot terminate run {run_id}: task {r.task_id} is no longer in a reclaimable state"
        )
    return {"ok": True, "run_id": run_id, "task_id": r.task_id}

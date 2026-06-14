from __future__ import annotations

import secrets
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_workers as kw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _insert_run(conn, task_id, *, worker_pid=None, ended_at=None):
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, ended_at) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time()), ended_at),
    )
    conn.commit()
    return cur.lastrowid


def _setup_running_task_with_run(conn, *, title, assignee, worker_pid):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, "
        "claim_expires=?, worker_pid=? WHERE id=?",
        (lock, future, worker_pid, task_id),
    )
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'running', ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time())),
    )
    conn.commit()
    return task_id, cur.lastrowid


def test_list_active_workers_returns_running_open_pid_rows(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="active-worker", assignee="alice")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        _insert_run(conn, task_id, worker_pid=12345)
        ended = kb.create_task(conn, title="ended-worker", assignee="bob")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ended,))
        _insert_run(conn, ended, worker_pid=99999, ended_at=int(time.time()) - 60)

        body = kw.list_active_workers(conn)

        assert body["count"] == 1
        assert "checked_at" in body
        worker = body["workers"][0]
        assert worker["task_id"] == task_id
        assert worker["task_title"] == "active-worker"
        assert worker["task_assignee"] == "alice"
        assert worker["task_status"] == "running"
        assert worker["worker_pid"] == 12345
    finally:
        conn.close()


def test_get_run_payload_returns_serialized_run(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="run-lookup", assignee="dave")
        run_id = _insert_run(conn, task_id, worker_pid=55555)

        body = kw.get_run_payload(conn, run_id)

        assert body["run"]["id"] == run_id
        assert body["run"]["task_id"] == task_id
        assert body["run"]["worker_pid"] == 55555
        assert body["run"]["ended_at"] is None
    finally:
        conn.close()


def test_get_run_payload_raises_for_unknown_run(kanban_home):
    conn = kb.connect()
    try:
        with pytest.raises(kw.RunNotFound):
            kw.get_run_payload(conn, 999999)
    finally:
        conn.close()


def test_inspect_run_dead_pid(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dead-pid", assignee="grace")
        run_id = _insert_run(conn, task_id, worker_pid=999999)

        mock_psutil = MagicMock()
        mock_psutil.NoSuchProcess = Exception
        mock_psutil.AccessDenied = PermissionError

        def _raise_no_such(*args, **kwargs):
            raise mock_psutil.NoSuchProcess("no such process")

        mock_psutil.Process = _raise_no_such
        body = kw.inspect_run(conn, run_id, psutil_module=mock_psutil)

        assert body["alive"] is False
        assert body["pid"] == 999999
        assert "not found" in body["reason"]
    finally:
        conn.close()


def test_inspect_run_live_pid(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="live-pid", assignee="heidi")
        run_id = _insert_run(conn, task_id, worker_pid=12345)

        mock_psutil = MagicMock()
        mock_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        mock_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_mem = MagicMock(rss=1024 * 1024 * 50, vms=1024 * 1024 * 200)
        fake_proc = MagicMock()
        fake_proc.as_dict.return_value = {
            "cpu_percent": 3.5,
            "memory_info": fake_mem,
            "num_threads": 4,
            "status": "sleeping",
            "create_time": time.time() - 300,
            "cmdline": ["python", "-m", "hermes"],
        }
        fake_proc.num_fds.return_value = 12
        mock_psutil.Process.return_value = fake_proc

        body = kw.inspect_run(conn, run_id, psutil_module=mock_psutil)

        assert body["alive"] is True
        assert body["pid"] == 12345
        assert body["cpu_percent"] == 3.5
        assert body["memory_rss_bytes"] == fake_mem.rss
        assert body["num_threads"] == 4
        assert body["status"] == "sleeping"
    finally:
        conn.close()


def test_terminate_run_reclaims_task_and_invokes_signal_path(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        task_id, run_id = _setup_running_task_with_run(
            conn, title="kill-me", assignee="jane", worker_pid=33333,
        )
        sent = []

        def _fake_terminate(pid, prev_lock, *, signal_fn=None):
            sent.append((pid, prev_lock))
            return {"signal": "SIGTERM", "delivered": True}

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _fake_terminate)

        body = kw.terminate_run(conn, run_id, reason="operator abort")

        assert body == {"ok": True, "run_id": run_id, "task_id": task_id}
        assert sent == [(33333, sent[0][1])]
        assert sent[0][1] is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.worker_pid is None
    finally:
        conn.close()


def test_terminate_run_raises_for_ended_run(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ended-terminate", assignee="ivy")
        run_id = _insert_run(
            conn, task_id, worker_pid=22222, ended_at=int(time.time()) - 30,
        )

        with pytest.raises(kw.RunAlreadyEnded):
            kw.terminate_run(conn, run_id, reason="too late")
    finally:
        conn.close()

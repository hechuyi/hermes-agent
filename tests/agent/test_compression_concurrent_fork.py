"""Regression tests for concurrent compression on one session id."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB


def _build_agent_with_db(db: SessionDB, session_id: str):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = MagicMock()

    def _compress_with_overlap(*_args, **_kwargs):
        time.sleep(0.25)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    compressor.compress.side_effect = _compress_with_overlap
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor.last_total_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    return agent


def _count_children(db: SessionDB, parent_sid: str) -> int:
    rows = db._conn.execute(
        "SELECT id FROM sessions WHERE parent_session_id = ?",
        (parent_sid,),
    ).fetchall()
    return len(rows)


def test_concurrent_compression_does_not_fork_session(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent_sid = "PARENT_TEST_SESSION"
        db.create_session(parent_sid, source="discord")

        agent_a = _build_agent_with_db(db, parent_sid)
        agent_b = _build_agent_with_db(db, parent_sid)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

        errors: list[BaseException] = []

        def run(agent) -> None:
            try:
                agent._compress_context(messages, "sys", approx_tokens=120_000)
            except BaseException as exc:
                errors.append(exc)

        t_a = threading.Thread(target=run, args=(agent_a,), name="main_turn")
        t_b = threading.Thread(target=run, args=(agent_b,), name="review_fork")
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert errors == []
        assert _count_children(db, parent_sid) == 1
        rotated = sum(1 for agent in (agent_a, agent_b) if agent.session_id != parent_sid)
        assert rotated == 1
        assert db.get_compression_lock_holder(parent_sid) is None
    finally:
        db.close()


def test_skipped_compression_returns_messages_unchanged(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent_sid = "LOSER_TEST"
        db.create_session(parent_sid, source="discord")
        assert db.try_acquire_compression_lock(parent_sid, "external_holder") is True

        agent = _build_agent_with_db(db, parent_sid)
        messages = [
            {"role": "user", "content": "m1"},
            {"role": "user", "content": "m2"},
        ]

        compressed, _system_prompt = agent._compress_context(
            messages,
            "sys",
            approx_tokens=120_000,
        )

        assert compressed is messages or compressed == messages
        assert agent.session_id == parent_sid
        agent.context_compressor.compress.assert_not_called()
    finally:
        db.close()


class _NoLockSubsystemDB:
    def __init__(self, real_db: SessionDB) -> None:
        self._real = real_db

    def try_acquire_compression_lock(self, *_args, **_kwargs):
        raise AttributeError(
            "'SessionDB' object has no attribute 'try_acquire_compression_lock'"
        )

    def get_compression_lock_holder(self, *_args, **_kwargs):
        raise AttributeError(
            "'SessionDB' object has no attribute 'get_compression_lock_holder'"
        )

    def release_compression_lock(self, *_args, **_kwargs):
        raise AttributeError(
            "'SessionDB' object has no attribute 'release_compression_lock'"
        )

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_missing_lock_subsystem_fails_open_not_infinite_loop(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent_sid = "SKEW_TEST_SESSION"
        db.create_session(parent_sid, source="discord")

        agent = _build_agent_with_db(db, parent_sid)
        agent._session_db = _NoLockSubsystemDB(db)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

        compressed, _system_prompt = agent._compress_context(
            messages,
            "sys",
            approx_tokens=120_000,
        )

        agent.context_compressor.compress.assert_called_once()
        assert len(compressed) < len(messages)
        assert agent.session_id != parent_sid
    finally:
        db.close()

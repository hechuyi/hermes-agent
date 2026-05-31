"""Tests for issue #860 — SQLite session transcript deduplication.

Verifies that:
1. _flush_messages_to_session_db uses _last_flushed_db_idx to avoid re-writing
2. Multiple _persist_session calls don't duplicate messages
3. append_to_transcript(skip_db=True) skips SQLite but writes JSONL
4. The gateway doesn't double-write messages the agent already persisted
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test: _flush_messages_to_session_db only writes new messages
# ---------------------------------------------------------------------------

class TestFlushDeduplication:
    """Verify _flush_messages_to_session_db tracks what it already wrote."""

    def _make_agent(self, session_db):
        """Create a minimal AIAgent with a real session DB."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id="test-session-860",
                skip_context_files=True,
                skip_memory=True,
            )
        # Simulate lazy session creation (normally done by run_conversation)
        agent._ensure_db_session()
        return agent

    def test_flush_writes_only_new_messages(self):
        """First flush writes all new messages, second flush writes none."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = [
                {"role": "user", "content": "old message"},
            ]
            messages = list(conversation_history) + [
                {"role": "user", "content": "new question"},
                {"role": "assistant", "content": "new answer"},
            ]

            # First flush — should write 2 new messages
            result = agent._flush_messages_to_session_db(messages, conversation_history)

            rows = db.get_messages(agent.session_id)
            assert len(rows) == 2, f"Expected 2 messages, got {len(rows)}"

            # Second flush with SAME messages — should write 0 new messages
            result = agent._flush_messages_to_session_db(messages, conversation_history)

            rows = db.get_messages(agent.session_id)
            assert len(rows) == 2, f"Expected still 2 messages after second flush, got {len(rows)}"

    def test_flush_writes_incrementally(self):
        """Messages added between flushes are written exactly once."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = []
            messages = [
                {"role": "user", "content": "hello"},
            ]

            # First flush — 1 message
            result = agent._flush_messages_to_session_db(messages, conversation_history)
            rows = db.get_messages(agent.session_id)
            assert len(rows) == 1

            # Add more messages
            messages.append({"role": "assistant", "content": "hi there"})
            messages.append({"role": "user", "content": "follow up"})

            # Second flush — should write only 2 new messages
            result = agent._flush_messages_to_session_db(messages, conversation_history)
            rows = db.get_messages(agent.session_id)
            assert len(rows) == 3, f"Expected 3 total messages, got {len(rows)}"
            assert result["attempted"] is True
            assert result["ok"] is True
            assert result["row_ids_by_message_index"][1] == rows[1]["id"]
            assert result["assistant_message_row_id"] == rows[1]["id"]

    def test_flush_failure_returns_typed_contract_and_does_not_advance_idx(self):
        """append_message failures must be visible to callers."""
        session_db = MagicMock()
        session_db.append_message.side_effect = sqlite3.OperationalError("disk full: /secret/path")

        agent = self._make_agent(session_db)
        result = agent._flush_messages_to_session_db(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            [],
        )

        assert result["attempted"] is True
        assert result["ok"] is False
        assert result["failure_class"] == "db_append_failed"
        assert result["stage"] == "append_message"
        assert result["message_index"] == 0
        assert result["role"] == "user"
        assert "disk full" in result["sanitized_reason"]
        assert "/secret/path" not in result["sanitized_reason"]
        assert result["row_ids_by_message_index"] == {}
        assert result["assistant_message_row_id"] is None
        assert agent._last_flushed_db_idx == 0

    def test_flush_real_db_rolls_back_partial_turn_when_final_assistant_append_fails(self, monkeypatch):
        """Real DB flushes are atomic: no half-turn rows or assistant proof on failure."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "final"},
            ]

            class FailingConnection:
                def __init__(self, conn):
                    self._conn = conn

                def execute(self, sql, params=()):
                    if (
                        isinstance(sql, str)
                        and "INSERT INTO messages" in sql
                        and len(params) >= 2
                        and params[1] == "assistant"
                    ):
                        raise sqlite3.OperationalError("assistant append failed: /tmp/secret/state.db")
                    return self._conn.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._conn, name)

            monkeypatch.setattr(db, "_conn", FailingConnection(db._conn))

            result = agent._persist_session(messages, [])

            assert result["attempted"] is True
            assert result["ok"] is False
            assert result["failure_class"] == "db_append_failed"
            assert result["stage"] == "append_message"
            assert result["message_index"] == 1
            assert result["role"] == "assistant"
            assert result["assistant_message_row_id"] is None
            assert result["assistant_content_sha256"] is None
            assert agent._last_flushed_db_idx == 0
            assert db.get_messages(agent.session_id) == []

    def test_persist_session_returns_flush_contract(self):
        """_persist_session exposes the DB persistence proof to callers."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)

            result = agent._persist_session(
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                [],
            )

            assert result["attempted"] is True
            assert result["ok"] is True
            assert result["assistant_message_row_id"] is not None
            assert result["assistant_content_sha256"] is not None
            assert agent._last_persistence_result == result

    def test_with_persistence_does_not_persist_empty_terminal_sentinel_as_visible_final(self):
        """The private empty-final marker must not become persisted assistant content."""
        from agent.conversation_loop import _with_persistence
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            messages = [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": "(empty)",
                    "_empty_terminal_sentinel": True,
                },
            ]

            result = _with_persistence(
                agent,
                messages,
                [],
                {
                    "final_response": "(empty)",
                    "messages": messages,
                    "failed": True,
                },
            )
            rows = db.get_messages(agent.session_id)

            assert result["messages"] == [{"role": "user", "content": "hello"}]
            assert [row["role"] for row in rows] == ["user"]
            assert [row["content"] for row in rows] == ["hello"]
            assert result["persistence"]["assistant_message_row_id"] is None
            assert result["persistence"]["assistant_content_sha256"] is None

    def test_content_policy_double_persist_proof_covers_user_and_final_assistant(self):
        """Content-policy terminal result must expose a complete turn proof."""
        from agent import conversation_loop as _conv_loop
        from gateway.run import _gateway_agent_persistence_covers_full_transcript
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent._fallback_chain = []
            agent._fallback_index = 0
            agent._cached_system_prompt = "You are helpful."
            agent._use_prompt_caching = False
            agent.tool_delay = 0
            agent.compression_enabled = False
            agent.save_trajectories = False
            err = RuntimeError(
                "This content was flagged for possible cybersecurity risk. "
                "If this seems wrong, try rephrasing your request."
            )
            err.status_code = 400
            agent.client = MagicMock()
            agent.client.chat.completions.create.side_effect = err

            with (
                patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources"),
                patch.object(_conv_loop, "time") as mock_time,
                patch.object(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0),
            ):
                mock_time.sleep = MagicMock()
                mock_time.monotonic.return_value = 12345.0
                mock_time.time.return_value = 1000.0
                result = agent.run_conversation("blocked request")

            assert result["failed"] is True
            assert "content_policy_blocked" in result["error"]
            assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
            assert _gateway_agent_persistence_covers_full_transcript(
                result["persistence"],
                result["messages"],
                history_offset=0,
            ) is True

    def test_persist_session_multiple_calls_no_duplication(self):
        """Multiple _persist_session calls don't duplicate DB entries."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = [{"role": "user", "content": "old"}]
            messages = list(conversation_history) + [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]

            # Simulate multiple persist calls (like the agent's many exit paths)
            for _ in range(5):
                agent._persist_session(messages, conversation_history)

            rows = db.get_messages(agent.session_id)
            assert len(rows) == 4, f"Expected 4 messages, got {len(rows)} (duplication bug!)"

    def test_flush_reset_after_compression(self):
        """After compression creates a new session, flush index resets."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            # Write some messages
            messages = [
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "reply1"},
            ]
            agent._flush_messages_to_session_db(messages, [])

            old_session = agent.session_id
            assert agent._last_flushed_db_idx == 2

            # Simulate what _compress_context does: new session, reset idx
            agent.session_id = "compressed-session-new"
            db.create_session(session_id=agent.session_id, source="test")
            agent._last_flushed_db_idx = 0

            # Now flush compressed messages to new session
            compressed_messages = [
                {"role": "user", "content": "summary of conversation"},
            ]
            agent._flush_messages_to_session_db(compressed_messages, [])

            new_rows = db.get_messages(agent.session_id)
            assert len(new_rows) == 1

            # Old session should still have its 2 messages
            old_rows = db.get_messages(old_session)
            assert len(old_rows) == 2


# ---------------------------------------------------------------------------
# Test: append_to_transcript skip_db parameter
# ---------------------------------------------------------------------------

class TestAppendToTranscriptSkipDb:
    """Verify skip_db=True skips the SQLite write."""

    def test_skip_db_prevents_sqlite_write(self, tmp_path):
        """With skip_db=True and a real DB, message does NOT appear in SQLite."""
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from hermes_state import SessionDB

        db_path = tmp_path / "test_skip.db"
        db = SessionDB(db_path=db_path)

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        session_id = "test-skip-db-real"
        db.create_session(session_id=session_id, source="test")

        msg = {"role": "assistant", "content": "hello world"}
        store.append_to_transcript(session_id, msg, skip_db=True)

        # SQLite should NOT have the message
        rows = db.get_messages(session_id)
        assert len(rows) == 0, f"Expected 0 DB rows with skip_db=True, got {len(rows)}"

    def test_default_writes_to_sqlite(self, tmp_path):
        """Without skip_db, message appears in SQLite."""
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from hermes_state import SessionDB

        db_path = tmp_path / "test_both.db"
        db = SessionDB(db_path=db_path)

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        session_id = "test-default-write"
        db.create_session(session_id=session_id, source="test")

        msg = {"role": "user", "content": "test message"}
        store.append_to_transcript(session_id, msg)

        # SQLite should have the message
        rows = db.get_messages(session_id)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test: _last_flushed_db_idx initialization
# ---------------------------------------------------------------------------

class TestFlushIdxInit:
    """Verify _last_flushed_db_idx is properly initialized."""

    def test_init_zero(self):
        """Agent starts with _last_flushed_db_idx = 0."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        assert agent._last_flushed_db_idx == 0

    def test_no_session_db_noop(self):
        """Without session_db, flush is a typed non-proof and doesn't crash."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        messages = [{"role": "user", "content": "test"}]
        result = agent._flush_messages_to_session_db(messages, [])
        # Should not crash, idx should remain 0
        assert agent._last_flushed_db_idx == 0
        assert result["attempted"] is False
        assert result["ok"] is False
        assert result["failure_class"] == "no_session_db"
        assert result["stage"] == "session_db_unavailable"

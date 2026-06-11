"""Malformed state.db schema recovery tests."""

import sqlite3
import uuid
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB, is_malformed_db_error, repair_state_db_schema


@pytest.fixture(autouse=True)
def _reset_repair_attempts():
    hermes_state._repair_attempted_paths.clear()
    hermes_state._set_last_init_error(None)
    yield
    hermes_state._repair_attempted_paths.clear()
    hermes_state._set_last_init_error(None)


def _build_healthy_db(db_path: Path) -> str:
    db = SessionDB(db_path=db_path)
    try:
        session_id = db.create_session(session_id=str(uuid.uuid4()), source="cli")
        for idx in range(5):
            db.append_message(session_id, role="user", content=f"hello world {idx}")
            db.append_message(
                session_id,
                role="assistant",
                content=f"reply about pizza {idx}",
            )
        return session_id
    finally:
        db.close()


def _corrupt_duplicate_fts_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
            "WHERE name='messages_fts'"
        )
        conn.commit()
    finally:
        conn.close()


def test_duplicate_fts_schema_makes_first_statement_fail(tmp_path):
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts_schema(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.DatabaseError) as excinfo:
            conn.execute("PRAGMA journal_mode").fetchone()
    finally:
        conn.close()
    assert is_malformed_db_error(excinfo.value)


def test_repair_preserves_sessions_messages_and_backup(tmp_path):
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts_schema(db_path)

    report = repair_state_db_schema(db_path)

    assert report["repaired"] is True
    assert report["failure_class"] == "malformed_schema"
    assert report["strategy"] in {"dedup_schema", "drop_fts_rebuild"}
    assert report["backup_created"] is True
    assert report["backup_name"]
    assert report["backup_path"]
    assert Path(report["backup_path"]).exists()

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    finally:
        conn.close()


def test_sessiondb_auto_heals_duplicate_schema_on_open(tmp_path):
    db_path = tmp_path / "state.db"
    session_id = _build_healthy_db(db_path)
    _corrupt_duplicate_fts_schema(db_path)

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.get_session(session_id) is not None
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'pizza'"
            ).fetchone()[0]
            == 5
        )
    finally:
        db.close()


def test_auto_heal_attempted_once_per_process(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts_schema(db_path)
    calls = {"count": 0}

    def fake_repair(path, **kwargs):
        calls["count"] += 1
        return {
            "repaired": False,
            "failure_class": "malformed_schema",
            "stage": "dedup_schema",
            "strategy": None,
            "backup_created": False,
            "backup_name": None,
            "backup_path": None,
            "error": "synthetic failure",
        }

    monkeypatch.setattr(hermes_state, "repair_state_db_schema", fake_repair)

    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert calls["count"] == 1


def test_non_malformed_error_is_not_auto_repaired(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"this is not a sqlite database")
    calls = {"count": 0}

    def fake_repair(path, **kwargs):
        calls["count"] += 1
        return {"repaired": True}

    monkeypatch.setattr(hermes_state, "repair_state_db_schema", fake_repair)

    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert calls["count"] == 0


def test_unrepairable_file_fails_with_backup_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\x00\xde\xad\xbe\xef" * 200)

    report = repair_state_db_schema(db_path)

    assert report["repaired"] is False
    assert report["failure_class"] == "malformed_schema"
    assert report["stage"] in {"dedup_schema", "drop_fts_rebuild"}
    assert report["error"]
    assert report["backup_created"] is True
    assert report["backup_name"]
    assert Path(report["backup_path"]).exists()


def test_malformed_error_classifier_is_narrow():
    assert is_malformed_db_error(
        sqlite3.DatabaseError("malformed database schema (messages_fts)")
    )
    assert is_malformed_db_error(sqlite3.DatabaseError("database disk image is malformed"))
    assert not is_malformed_db_error(sqlite3.OperationalError("database is locked"))
    assert not is_malformed_db_error(ValueError("malformed database schema"))

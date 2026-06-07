import json
import sqlite3
import subprocess
import sys
import tarfile
import time
import importlib.util
from pathlib import Path

from hermes_state import (
    SCHEMA_CONTRACT_META_KEY,
    SCHEMA_CONTRACT_META_VALUE,
    SCHEMA_VERSION,
    SCOPE_ONLY_SCHEMA_CONTRACT_META_VALUE,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "scripts" / "hermes_state_migration_probe.py"


def _create_legacy_v10_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (10);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL,
            ended_at REAL,
            title TEXT,
            parent_session_id TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            api_call_count INTEGER DEFAULT 0
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_name TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT
        );

        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, content=messages, content_rowid=id
        );
        CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
            content, content=messages, content_rowid=id, tokenize='trigram'
        );
        CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)",
        ("legacy", "cli", time.time(), 2),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)",
        ("empty", "cli", time.time(), 0),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, timestamp, role, content, tool_name, tool_calls) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "legacy", time.time(), "user", "hello", None, None),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, timestamp, role, content, tool_name, tool_calls) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, "legacy", time.time(), "assistant", "", "LEGACYTOOL", '{"q":"LEGACYARG"}'),
    )
    conn.commit()
    conn.close()


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def test_probe_migrates_temporary_copy_and_reports_invariants(tmp_path):
    source_db = tmp_path / "state.db"
    report_path = tmp_path / "report.json"
    _create_legacy_v10_db(source_db)

    result = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--input",
            str(source_db),
            "--json-output",
            str(report_path),
            "--expect-sessions",
            "2",
            "--expect-messages",
            "2",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert _schema_version(source_db) == 10

    report = json.loads(report_path.read_text())
    assert report["pass"] is True
    assert report["failure_class"] is None
    assert report["before"]["schema_version"] == 10
    assert report["after"]["schema_version"] == SCHEMA_VERSION
    assert report["before"]["counts"] == {"sessions": 2, "messages": 2}
    assert report["after"]["counts"] == {"sessions": 2, "messages": 2}
    assert report["before"]["row_fingerprints"]["sessions"]["count"] == 2
    assert report["before"]["row_fingerprints"]["messages"]["count"] == 2
    for table in ("sessions", "messages"):
        before_columns = set(report["before"]["row_fingerprints"][table]["columns"])
        after_columns = set(report["after"]["row_fingerprints"][table]["columns"])
        assert before_columns <= after_columns
        for column, digest in report["before"]["row_fingerprints"][table][
            "column_sha256"
        ].items():
            assert digest == report["after"]["row_fingerprints"][table]["column_sha256"][column]
    assert report["after"]["tables"]["conversation_scopes"]["exists"] is True
    assert report["after"]["tables"]["compression_locks"]["exists"] is True
    assert report["after"]["fts_counts"]["messages_fts"] == 2
    assert report["after"]["fts_counts"]["messages_fts_trigram"] == 2
    assert report["after"]["schema_fingerprint"]
    assert report["after"]["state_meta_contract_marker"]["ok"] is True
    assert report["after"]["state_meta_contract_marker"]["key"] == SCHEMA_CONTRACT_META_KEY
    assert report["after"]["state_meta_contract_marker"]["expected"] == SCHEMA_CONTRACT_META_VALUE
    assert report["after"]["state_meta_contract_marker"]["actual"] == SCHEMA_CONTRACT_META_VALUE
    assert report["after"]["required_object_invariants"]["conversation_scopes"]["ok"] is True
    assert report["after"]["required_object_invariants"]["compression_locks"]["ok"] is True
    scope_distribution = report["after"]["scope_assignment_status"]
    assert scope_distribution.get("scoped", 0) == 0
    assert sum(scope_distribution.values()) == 2
    assert report["checks"]["integrity_check"] == ["ok"]
    assert report["checks"]["quick_check"] == ["ok"]
    assert report["checks"]["foreign_key_check"] == []


def test_probe_accepts_backup_tarball_without_mutating_input(tmp_path):
    source_db = tmp_path / "state.db"
    _create_legacy_v10_db(source_db)
    backup = tmp_path / "hermes-home.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        tar.add(source_db, arcname=".hermes/state.db")

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(backup)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["pass"] is True
    assert report["input"]["kind"] == "tar.gz"
    assert report["input"]["selected_member"] == ".hermes/state.db"
    assert report["before"]["schema_version"] == 10
    assert report["after"]["schema_version"] == SCHEMA_VERSION


def test_probe_fails_closed_when_backup_tarball_has_ambiguous_state_db_members(tmp_path):
    source_db = tmp_path / "state.db"
    _create_legacy_v10_db(source_db)
    backup = tmp_path / "ambiguous.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        tar.add(source_db, arcname="first/.hermes/state.db")
        tar.add(source_db, arcname="second/.hermes/state.db")

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(backup)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["pass"] is False
    assert report["failure_class"] == "input_state_db_ambiguous"
    assert report["input"]["candidate_count"] == 2
    assert report["input"]["exact_candidate_count"] == 2
    assert report["input"]["candidate_members"] == [
        "first/.hermes/state.db",
        "second/.hermes/state.db",
    ]


def test_probe_fails_closed_when_backup_tarball_has_multiple_non_exact_state_db_members(tmp_path):
    source_db = tmp_path / "state.db"
    _create_legacy_v10_db(source_db)
    backup = tmp_path / "ambiguous-non-exact.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        tar.add(source_db, arcname="alpha/state.db")
        tar.add(source_db, arcname="beta/state.db")

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(backup)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["pass"] is False
    assert report["failure_class"] == "input_state_db_ambiguous"
    assert report["input"]["candidate_count"] == 2
    assert report["input"]["exact_candidate_count"] == 0
    assert report["input"]["candidate_members"] == ["alpha/state.db", "beta/state.db"]


def test_probe_fails_closed_when_expected_counts_do_not_match(tmp_path):
    source_db = tmp_path / "state.db"
    _create_legacy_v10_db(source_db)

    result = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--input",
            str(source_db),
            "--expect-sessions",
            "999",
            "--expect-messages",
            "2",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["pass"] is False
    assert report["failure_class"] == "expected_count_mismatch"


def test_probe_fails_closed_when_version_14_lacks_required_scope_objects(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (14);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
        CREATE TABLE state_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("s1", "cli", 1000.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("s1", "user", "hello", 1001.0),
    )
    conn.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)",
        (SCHEMA_CONTRACT_META_KEY, SCOPE_ONLY_SCHEMA_CONTRACT_META_VALUE),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(db_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["pass"] is False
    assert report["failure_class"] == "state_schema_missing_required_object"
    assert report["before"]["schema_fingerprint"]
    assert report["before"]["required_object_invariants"]["conversation_scopes"]["ok"] is False
    assert "after" not in report


def test_probe_fails_closed_when_version_14_lacks_required_scope_columns(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (14);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
        CREATE TABLE conversation_scopes (
            id TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            participant_mode TEXT NOT NULL
        );
        CREATE TABLE state_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("s1", "cli", 1000.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("s1", "user", "hello", 1001.0),
    )
    conn.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)",
        (SCHEMA_CONTRACT_META_KEY, SCOPE_ONLY_SCHEMA_CONTRACT_META_VALUE),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(db_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["failure_class"] == "state_schema_missing_required_object"
    assert report["before"]["required_object_invariants"]["sessions"]["ok"] is False
    assert "conversation_scope_id" in report["before"]["required_object_invariants"]["sessions"]["missing_columns"]


def test_probe_fails_closed_when_db_version_exceeds_supported(tmp_path):
    db_path = tmp_path / "state.db"
    _create_legacy_v10_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(PROBE), "--input", str(db_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["failure_class"] == "state_schema_unknown_future_version"


def test_validate_after_requires_supported_schema_version():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    before = {
        "schema_version": SCHEMA_VERSION,
        "counts": {"sessions": 1, "messages": 1},
        "scope_assignment_status": {},
        "row_fingerprints": {
            "sessions": {"sha256": "same", "count": 1, "column_sha256": {}},
            "messages": {"sha256": "same", "count": 1, "column_sha256": {}},
        },
    }
    after = {
        "schema_version": SCHEMA_VERSION - 1,
        "counts": {"sessions": 1, "messages": 1},
        "row_fingerprints": {
            "sessions": {"sha256": "same", "count": 1, "column_sha256": {}},
            "messages": {"sha256": "same", "count": 1, "column_sha256": {}},
        },
        "required_object_invariants": {
            "sessions": {"ok": True},
            "messages": {"ok": True},
            "conversation_scopes": {"ok": True},
            "state_meta": {"ok": True},
            "compression_locks": {"ok": True},
            "messages_fts": {"ok": True},
            "messages_fts_trigram": {"ok": True},
        },
        "state_meta_contract_marker": {"ok": True, "actual": SCHEMA_CONTRACT_META_VALUE},
        "fts_counts": {"messages_fts": 1, "messages_fts_trigram": 1},
        "scope_assignment_status": {},
    }
    checks = {
        "integrity_check": ["ok"],
        "quick_check": ["ok"],
        "foreign_key_check": [],
    }

    try:
        module._validate_after(
            before=before,
            after=after,
            checks=checks,
            expect_sessions=None,
            expect_messages=None,
        )
    except module.ProbeFailure as exc:
        assert exc.failure_class == "state_schema_version_mismatch"
    else:
        raise AssertionError("expected state_schema_version_mismatch")


def test_validate_after_detects_row_fingerprint_mismatch():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    before = {
        "schema_version": SCHEMA_VERSION,
        "counts": {"sessions": 1, "messages": 1},
        "scope_assignment_status": {},
        "row_fingerprints": {
            "sessions": {"sha256": "session-before", "count": 1, "column_sha256": {}},
            "messages": {
                "sha256": "message-before",
                "count": 1,
                "columns": ["content"],
                "column_sha256": {"content": "message-before"},
            },
        },
    }
    after = {
        "schema_version": SCHEMA_VERSION,
        "counts": {"sessions": 1, "messages": 1},
        "row_fingerprints": {
            "sessions": {"sha256": "session-before", "count": 1, "column_sha256": {}},
            "messages": {
                "sha256": "message-after",
                "count": 1,
                "columns": ["content"],
                "column_sha256": {"content": "message-after"},
            },
        },
        "required_object_invariants": {
            "sessions": {"ok": True},
            "messages": {"ok": True},
            "conversation_scopes": {"ok": True},
            "state_meta": {"ok": True},
            "compression_locks": {"ok": True},
            "messages_fts": {"ok": True},
            "messages_fts_trigram": {"ok": True},
        },
        "state_meta_contract_marker": {"ok": True, "actual": SCHEMA_CONTRACT_META_VALUE},
        "fts_counts": {"messages_fts": 1, "messages_fts_trigram": 1},
        "scope_assignment_status": {},
    }
    checks = {
        "integrity_check": ["ok"],
        "quick_check": ["ok"],
        "foreign_key_check": [],
    }

    try:
        module._validate_after(
            before=before,
            after=after,
            checks=checks,
            expect_sessions=None,
            expect_messages=None,
        )
    except module.ProbeFailure as exc:
        assert exc.failure_class == "row_fingerprint_mismatch"
    else:
        raise AssertionError("expected row_fingerprint_mismatch")


def test_validate_after_fails_closed_and_preserves_evidence_when_fts_counts_mismatch():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    before = {
        "schema_version": SCHEMA_VERSION,
        "counts": {"sessions": 1, "messages": 2},
        "scope_assignment_status": {},
        "row_fingerprints": {
            "sessions": {"sha256": "session", "count": 1, "column_sha256": {}},
            "messages": {"sha256": "messages", "count": 2, "column_sha256": {}},
        },
    }
    after = {
        "schema_version": SCHEMA_VERSION,
        "counts": {"sessions": 1, "messages": 2},
        "row_fingerprints": {
            "sessions": {"sha256": "session", "count": 1, "column_sha256": {}},
            "messages": {"sha256": "messages", "count": 2, "column_sha256": {}},
        },
        "required_object_invariants": {
            "sessions": {"ok": True},
            "messages": {"ok": True},
            "conversation_scopes": {"ok": True},
            "state_meta": {"ok": True},
            "compression_locks": {"ok": True},
            "messages_fts": {"ok": True},
            "messages_fts_trigram": {"ok": True},
        },
        "state_meta_contract_marker": {
            "ok": True,
            "actual": SCHEMA_CONTRACT_META_VALUE,
        },
        "fts_counts": {"messages_fts": 1, "messages_fts_trigram": 2},
        "scope_assignment_status": {},
    }
    checks = {
        "integrity_check": ["ok"],
        "quick_check": ["ok"],
        "foreign_key_check": [],
    }

    try:
        module._validate_after(
            before=before,
            after=after,
            checks=checks,
            expect_sessions=None,
            expect_messages=None,
        )
    except module.ProbeFailure as exc:
        assert exc.failure_class == "fts_count_mismatch"
        assert after["fts_counts"]["messages_fts"] == 1
    else:
        raise AssertionError("expected fts_count_mismatch")

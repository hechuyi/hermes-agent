#!/usr/bin/env python3
"""Fail-closed deployment probe for Hermes SessionDB state migrations.

The probe never opens the input database in write mode. It copies either a
state.db file or a hermes-home backup tarball member into a temporary
directory, opens only that copy through hermes_state.SessionDB, then reports
pre/post schema and data invariants as JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import (  # noqa: E402
    SCHEMA_CONTRACT_META_KEY,
    SCHEMA_CONTRACT_META_VALUE,
    SCHEMA_VERSION,
    SCOPE_ONLY_SCHEMA_CONTRACT_META_VALUE,
    SCOPE_ONLY_SCHEMA_VERSION,
    SessionDB,
)


REQUIRED_TABLES = {
    "schema_version",
    "sessions",
    "messages",
    "conversation_scopes",
    "state_meta",
    "compression_locks",
    "messages_fts",
    "messages_fts_trigram",
}

REQUIRED_COLUMNS = {
    "sessions": {
        "id",
        "source",
        "started_at",
        "conversation_scope_id",
        "scope_assignment_status",
        "route_session_key_snapshot",
        "route_partition_key",
    },
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "tool_name",
        "tool_calls",
        "timestamp",
        "conversation_scope_id",
    },
    "conversation_scopes": {
        "id",
        "canonical_key",
        "platform",
        "chat_type",
        "chat_id",
        "participant_mode",
    },
    "state_meta": {
        "key",
        "value",
    },
    "compression_locks": {
        "session_id",
        "holder",
        "acquired_at",
        "expires_at",
    },
}

ROW_FINGERPRINT_COLUMNS = {
    "sessions": (
        "id",
        "source",
        "user_id",
        "parent_session_id",
        "started_at",
        "ended_at",
        "end_reason",
        "title",
    ),
    "messages": (
        "id",
        "session_id",
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "timestamp",
        "finish_reason",
        "platform_message_id",
        "observed",
    ),
}

MAX_REPORTED_TARBALL_MEMBERS = 20


class ProbeFailure(RuntimeError):
    def __init__(self, failure_class: str, reason: str):
        super().__init__(reason)
        self.failure_class = failure_class
        self.reason = reason


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'virtual table')",
        (table,),
    ).fetchone()
    return row is not None


def _schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "schema_version"):
        return None
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    return None if row is None else int(row[0])


def _user_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.DatabaseError:
        return None
    return None if row is None else int(row[0])


def _count_rows(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    try:
        return int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    except sqlite3.DatabaseError:
        return None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _table_exists(conn, table):
        return []
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    canonical = "\n".join(
        f"{row[0]}|{row[1]}|{row[2]}|{row[3] or ''}" for row in rows
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _jsonable_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return value


def _row_fingerprint(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not _table_exists(conn, table):
        return {
            "ok": False,
            "count": None,
            "sha256": None,
            "columns": [],
            "missing_columns": list(ROW_FINGERPRINT_COLUMNS[table]),
        }

    live_columns = set(_columns(conn, table))
    columns = [col for col in ROW_FINGERPRINT_COLUMNS[table] if col in live_columns]
    missing_columns = [
        col for col in ROW_FINGERPRINT_COLUMNS[table] if col not in live_columns
    ]
    if not columns:
        return {
            "ok": False,
            "count": None,
            "sha256": None,
            "columns": [],
            "missing_columns": missing_columns,
        }

    quoted_columns = ", ".join(f'"{col}"' for col in columns)
    order_column = "id" if "id" in live_columns else columns[0]
    rows = conn.execute(
        f'SELECT {quoted_columns} FROM "{table}" ORDER BY "{order_column}"'
    ).fetchall()
    digest = hashlib.sha256()
    column_digests = {col: hashlib.sha256() for col in columns}
    for row in rows:
        item = {col: _jsonable_cell(row[col]) for col in columns}
        digest.update(
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
        row_id = _jsonable_cell(row[order_column])
        for col in columns:
            column_digests[col].update(
                json.dumps(
                    {"id": row_id, "value": _jsonable_cell(row[col])},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            column_digests[col].update(b"\n")
    return {
        "ok": True,
        "count": len(rows),
        "sha256": digest.hexdigest(),
        "column_sha256": {
            col: column_digests[col].hexdigest()
            for col in columns
        },
        "columns": columns,
        "missing_columns": missing_columns,
    }


def _row_fingerprints(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        table: _row_fingerprint(conn, table)
        for table in sorted(ROW_FINGERPRINT_COLUMNS)
    }


def _required_object_invariants(conn: sqlite3.Connection) -> dict[str, Any]:
    invariants: dict[str, Any] = {}
    for table, required in REQUIRED_COLUMNS.items():
        exists = _table_exists(conn, table)
        columns = set(_columns(conn, table))
        missing = sorted(required.difference(columns))
        invariants[table] = {
            "ok": exists and not missing,
            "exists": exists,
            "missing_columns": missing,
        }
    for table in ("messages_fts", "messages_fts_trigram"):
        exists = _table_exists(conn, table)
        invariants[table] = {"ok": exists, "exists": exists, "missing_columns": []}
    return invariants


def _contract_marker_check(
    conn: sqlite3.Connection,
    *,
    expected_value: str = SCHEMA_CONTRACT_META_VALUE,
) -> dict[str, Any]:
    if not _table_exists(conn, "state_meta"):
        return {
            "ok": False,
            "read": False,
            "key": SCHEMA_CONTRACT_META_KEY,
            "expected": expected_value,
            "actual": None,
        }
    try:
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SCHEMA_CONTRACT_META_KEY,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return {
            "ok": False,
            "read": False,
            "key": SCHEMA_CONTRACT_META_KEY,
            "expected": expected_value,
            "actual": None,
        }
    actual = None if row is None else row[0]
    read_ok = actual == expected_value
    return {
        "ok": read_ok,
        "read": row is not None,
        "key": SCHEMA_CONTRACT_META_KEY,
        "expected": expected_value,
        "actual": actual,
    }


def _scope_distribution(conn: sqlite3.Connection) -> dict[str, int]:
    if "scope_assignment_status" not in _columns(conn, "sessions"):
        return {}
    rows = conn.execute(
        "SELECT COALESCE(scope_assignment_status, '<NULL>') AS status, count(*) "
        "FROM sessions GROUP BY COALESCE(scope_assignment_status, '<NULL>') "
        "ORDER BY status"
    ).fetchall()
    return {str(status): int(count) for status, count in rows}


def _snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schema_version": _schema_version(conn),
        "user_version": _user_version(conn),
        "counts": {
            "sessions": _count_rows(conn, "sessions"),
            "messages": _count_rows(conn, "messages"),
        },
        "columns": {
            "sessions": _columns(conn, "sessions"),
            "messages": _columns(conn, "messages"),
            "conversation_scopes": _columns(conn, "conversation_scopes"),
            "state_meta": _columns(conn, "state_meta"),
            "compression_locks": _columns(conn, "compression_locks"),
        },
        "tables": {
            table: {"exists": _table_exists(conn, table)}
            for table in sorted(REQUIRED_TABLES)
        },
        "scope_assignment_status": _scope_distribution(conn),
        "fts_counts": {
            "messages_fts": _count_rows(conn, "messages_fts"),
            "messages_fts_trigram": _count_rows(conn, "messages_fts_trigram"),
        },
        "schema_fingerprint": _schema_fingerprint(conn),
        "required_object_invariants": _required_object_invariants(conn),
        "row_fingerprints": _row_fingerprints(conn),
    }


def _run_checks(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = [
        str(row[0])
        for row in conn.execute("PRAGMA integrity_check").fetchall()
    ]
    quick = [
        str(row[0])
        for row in conn.execute("PRAGMA quick_check").fetchall()
    ]
    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "integrity_check": integrity,
        "quick_check": quick,
        "foreign_key_check": [list(row) for row in fk_rows],
    }


def _sanitize_member_name(name: str) -> str:
    return Path(name).as_posix().lstrip("/")


def _ambiguous_tarball_meta(candidates: list[tarfile.TarInfo], exact_count: int) -> dict[str, Any]:
    names = [_sanitize_member_name(member.name) for member in candidates]
    return {
        "candidate_count": len(candidates),
        "exact_candidate_count": exact_count,
        "candidate_members": names[:MAX_REPORTED_TARBALL_MEMBERS],
        "candidate_members_truncated": len(names) > MAX_REPORTED_TARBALL_MEMBERS,
    }


def _safe_extract_state_db(archive_path: Path, destination: Path) -> tuple[str, dict[str, Any]]:
    with tarfile.open(archive_path, "r:*") as tar:
        candidates = []
        for member in tar.getmembers():
            if member.isdir():
                continue
            name = Path(member.name)
            if name.name == "state.db":
                candidates.append(member)
        if not candidates:
            raise ProbeFailure("input_state_db_not_found", "backup has no state.db member")
        exact = [
            m
            for m in candidates
            if Path(m.name).as_posix().lstrip("./").endswith(".hermes/state.db")
        ]
        if len(exact) == 1:
            member = exact[0]
        elif len(exact) > 1 or len(candidates) > 1:
            meta = _ambiguous_tarball_meta(candidates, len(exact))
            raise ProbeFailure(
                "input_state_db_ambiguous",
                json.dumps(meta, sort_keys=True, separators=(",", ":")),
            )
        else:
            member = candidates[0]
        source = tar.extractfile(member)
        if source is None:
            raise ProbeFailure("input_state_db_not_found", "state.db member cannot be read")
        with source, destination.open("wb") as out:
            shutil.copyfileobj(source, out)
        return _sanitize_member_name(member.name), _ambiguous_tarball_meta(candidates, len(exact))


def _copy_input_to_temp(input_path: Path, work_dir: Path) -> tuple[Path, dict[str, Any]]:
    copy_path = work_dir / "state-copy.db"
    if input_path.is_file() and tarfile.is_tarfile(input_path):
        member_name, member_meta = _safe_extract_state_db(input_path, copy_path)
        return copy_path, {
            "kind": "tar.gz",
            "name": input_path.name,
            "selected_member": member_name,
            **member_meta,
        }
    if input_path.is_file():
        shutil.copy2(input_path, copy_path)
        sidecars = []
        for suffix in ("-wal", "-shm"):
            source_sidecar = input_path.with_name(input_path.name + suffix)
            if source_sidecar.exists():
                shutil.copy2(source_sidecar, copy_path.with_name(copy_path.name + suffix))
                sidecars.append(suffix)
        return copy_path, {"kind": "sqlite", "name": input_path.name, "sidecars": sidecars}
    raise ProbeFailure("input_not_found", "input is not a readable file")


def _validate_before(before: dict[str, Any]) -> None:
    for table in ("schema_version", "sessions", "messages"):
        if not before["tables"].get(table, {}).get("exists"):
            raise ProbeFailure("state_schema_missing_required_object", f"missing table: {table}")
    for table in ("sessions", "messages"):
        if before["counts"][table] is None:
            raise ProbeFailure("state_schema_missing_required_object", f"cannot count table: {table}")


def _validate_supported_version(before: dict[str, Any], supported_version: int) -> None:
    version = before["schema_version"]
    if version is not None and version > supported_version:
        raise ProbeFailure(
            "state_schema_unknown_future_version",
            "database schema_version exceeds current source support",
        )


def _find_invariant_failure(
    snapshot: dict[str, Any],
    *,
    ignore: set[str] | None = None,
) -> str | None:
    ignored = ignore or set()
    for name, invariant in snapshot["required_object_invariants"].items():
        if name in ignored:
            continue
        if not invariant["ok"]:
            return name
    return None


def _validate_version_contracts(before: dict[str, Any], supported_version: int) -> None:
    schema_version = before["schema_version"]
    if schema_version == supported_version:
        marker = before.get("state_meta_contract_marker", {})
        if not marker.get("ok"):
            raise ProbeFailure(
                "state_schema_contract_mismatch",
                "current-version database schema contract marker missing or mismatched",
            )
        failed_object = _find_invariant_failure(before)
        if failed_object is not None:
            raise ProbeFailure(
                "state_schema_missing_required_object",
                f"current-version database missing required object: {failed_object}",
            )
        return

    if schema_version != SCOPE_ONLY_SCHEMA_VERSION:
        return

    marker = before.get("scope_only_contract_marker", {})
    if not marker.get("ok"):
        raise ProbeFailure(
            "state_schema_contract_mismatch",
            "scope-only v14 database schema contract marker missing or mismatched",
        )
    failed_object = _find_invariant_failure(before, ignore={"compression_locks"})
    if failed_object is not None:
        raise ProbeFailure(
            "state_schema_missing_required_object",
            f"scope-only v14 database missing required object: {failed_object}",
        )


def _validate_after(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    checks: dict[str, Any],
    expect_sessions: int | None,
    expect_messages: int | None,
) -> None:
    if expect_sessions is not None and before["counts"]["sessions"] != expect_sessions:
        raise ProbeFailure("expected_count_mismatch", "sessions count does not match expectation")
    if expect_messages is not None and before["counts"]["messages"] != expect_messages:
        raise ProbeFailure("expected_count_mismatch", "messages count does not match expectation")

    if before["counts"] != after["counts"]:
        raise ProbeFailure("count_changed", "session/message counts changed during migration")

    if after["schema_version"] != SCHEMA_VERSION:
        raise ProbeFailure(
            "state_schema_version_mismatch",
            "post-migration schema_version does not match supported source version",
        )

    before_fingerprints = before.get("row_fingerprints", {})
    after_fingerprints = after.get("row_fingerprints", {})
    for table in ROW_FINGERPRINT_COLUMNS:
        before_fp = before_fingerprints.get(table, {})
        after_fp = after_fingerprints.get(table, {})
        if before_fp.get("count") != after_fp.get("count"):
            raise ProbeFailure(
                "state_data_changed",
                f"{table} fingerprint row count changed during migration",
            )
        before_column_hashes = before_fp.get("column_sha256", {})
        after_column_hashes = after_fp.get("column_sha256", {})
        for column, before_hash in before_column_hashes.items():
            if column not in after_column_hashes:
                raise ProbeFailure(
                    "state_data_changed",
                    f"{table}.{column} disappeared during migration",
                )
            if before_hash != after_column_hashes[column]:
                raise ProbeFailure(
                    "row_fingerprint_mismatch",
                    f"{table}.{column} values changed during migration",
                )
        if before_fp.get("columns") == after_fp.get("columns") and before_fp.get(
            "sha256"
        ) != after_fp.get("sha256"):
            raise ProbeFailure(
                "row_fingerprint_mismatch",
                f"{table} row fingerprint changed during migration",
            )

    failed_object = _find_invariant_failure(after)
    if failed_object is not None:
        raise ProbeFailure(
            "state_schema_missing_required_object",
            f"missing required object after migration: {failed_object}",
        )
    marker = after.get("state_meta_contract_marker", {})
    if not marker.get("ok"):
        raise ProbeFailure(
            "state_schema_contract_mismatch",
            "state_meta schema contract marker missing or mismatched",
        )

    message_count = after["counts"]["messages"]
    for table, fts_count in after["fts_counts"].items():
        if fts_count != message_count:
            raise ProbeFailure("fts_count_mismatch", f"{table} count does not match messages")

    if checks["integrity_check"] != ["ok"]:
        raise ProbeFailure("integrity_check_failed", "PRAGMA integrity_check failed")
    if checks["quick_check"] != ["ok"]:
        raise ProbeFailure("quick_check_failed", "PRAGMA quick_check failed")
    if checks["foreign_key_check"]:
        raise ProbeFailure("foreign_key_check_failed", "PRAGMA foreign_key_check returned rows")

    before_scoped = before["scope_assignment_status"].get("scoped", 0)
    after_scoped = after["scope_assignment_status"].get("scoped", 0)
    if after_scoped > before_scoped:
        raise ProbeFailure("scope_status_regression", "migration promoted old rows to scoped")


def run_probe(
    input_path: Path,
    *,
    expect_sessions: int | None = None,
    expect_messages: int | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "pass": False,
        "failure_class": None,
        "failure_reason": None,
        "input": {"kind": "unknown", "name": input_path.name},
    }
    with tempfile.TemporaryDirectory(prefix="hermes-state-probe-") as tmp:
        try:
            work_dir = Path(tmp)
            copy_path, input_meta = _copy_input_to_temp(input_path, work_dir)
            report["input"] = input_meta
            report["copy"] = {"name": copy_path.name}

            conn = sqlite3.connect(str(copy_path))
            conn.row_factory = sqlite3.Row
            try:
                before = _snapshot(conn)
                before["state_meta_contract_marker"] = _contract_marker_check(conn)
                before["scope_only_contract_marker"] = _contract_marker_check(
                    conn,
                    expected_value=SCOPE_ONLY_SCHEMA_CONTRACT_META_VALUE,
                )
                report["before"] = before
                _validate_before(before)
            finally:
                conn.close()

            report["supported_schema_version"] = SCHEMA_VERSION
            _validate_supported_version(before, SCHEMA_VERSION)
            _validate_version_contracts(before, SCHEMA_VERSION)

            try:
                migrated = SessionDB(db_path=copy_path)
                migrated.close()
            except Exception as exc:
                raise ProbeFailure("migration_failed", type(exc).__name__) from exc

            conn = sqlite3.connect(str(copy_path))
            conn.row_factory = sqlite3.Row
            try:
                after = _snapshot(conn)
                after["state_meta_contract_marker"] = _contract_marker_check(conn)
                checks = _run_checks(conn)
                report["after"] = after
                report["checks"] = checks
                _validate_after(
                    before=before,
                    after=after,
                    checks=checks,
                    expect_sessions=expect_sessions,
                    expect_messages=expect_messages,
                )
            finally:
                conn.close()
        except ProbeFailure as exc:
            report["failure_class"] = exc.failure_class
            report["failure_reason"] = exc.reason
            if exc.failure_class == "input_state_db_ambiguous":
                try:
                    report["input"].update(json.loads(exc.reason))
                except json.JSONDecodeError:
                    pass
            return report
        except Exception as exc:
            report["failure_class"] = "probe_error"
            report["failure_reason"] = type(exc).__name__
            return report

    report["pass"] = True
    return report


def _write_report(report: dict[str, Any], output_path: Path | None) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output_path is None:
        sys.stdout.write(payload)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="state.db or backup tar.gz")
    parser.add_argument("--json-output", type=Path, help="write JSON report to this path")
    parser.add_argument("--expect-sessions", type=int)
    parser.add_argument("--expect-messages", type=int)
    args = parser.parse_args(argv)

    try:
        report = run_probe(
            args.input,
            expect_sessions=args.expect_sessions,
            expect_messages=args.expect_messages,
        )
        _write_report(report, args.json_output)
        return 0 if report.get("pass") else 1
    except Exception as exc:
        report = {
            "pass": False,
            "failure_class": "probe_error",
            "failure_reason": type(exc).__name__,
            "input": {"kind": "unknown", "name": args.input.name},
        }
        _write_report(report, args.json_output)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

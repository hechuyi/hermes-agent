from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_attachments as ka


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class ChunkReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_safe_attachment_name_strips_paths_and_dot_prefix():
    assert ka.safe_attachment_name("../../.env") == "env"
    assert ka.safe_attachment_name(r"C:\tmp\report.pdf") == "report.pdf"


@pytest.mark.asyncio
async def test_store_upload_sanitizes_and_records_attachment(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="upload target")
        att = await ka.store_attachment_upload(
            conn,
            task_id,
            filename="../../notes.txt",
            content_type="text/plain",
            uploaded_by="tester",
            reader=ChunkReader([b"hello ", b"world"]),
        )

        assert att.filename == "notes.txt"
        assert att.size == len(b"hello world")
        assert Path(att.stored_path).read_bytes() == b"hello world"
        assert Path(att.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(task_id).resolve()
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_store_upload_avoids_broken_symlink_collision(kanban_home, tmp_path):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="upload target")
        dest_dir = kb.task_attachments_dir(task_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside-created.txt"
        symlink = dest_dir / "evil.txt"
        try:
            symlink.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable on this platform")

        att = await ka.store_attachment_upload(
            conn,
            task_id,
            filename="evil.txt",
            content_type="text/plain",
            uploaded_by="tester",
            reader=ChunkReader([b"x"]),
        )

        assert not outside.exists()
        assert att.filename == "evil (1).txt"
        assert Path(att.stored_path).read_bytes() == b"x"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_store_upload_removes_partial_file_when_size_limit_exceeded(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="upload target")

        with pytest.raises(ka.AttachmentTooLarge):
            await ka.store_attachment_upload(
                conn,
                task_id,
                filename="huge.bin",
                content_type="application/octet-stream",
                uploaded_by="tester",
                reader=ChunkReader([b"abc", b"def"]),
                max_bytes=5,
            )

        assert not any(kb.task_attachments_dir(task_id).iterdir())
        assert kb.list_attachments(conn, task_id) == []
    finally:
        conn.close()


def test_resolve_attachment_download_rejects_tampered_path(kanban_home, tmp_path):
    conn = kb.connect()
    try:
        first = kb.create_task(conn, title="first")
        second = kb.create_task(conn, title="second")
        second_dir = kb.task_attachments_dir(second)
        second_dir.mkdir(parents=True, exist_ok=True)
        blob = second_dir / "other.txt"
        blob.write_text("wrong task")

        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
            (first, "other.txt", str(blob), "text/plain", blob.stat().st_size, "tester"),
        )
        conn.commit()
        att_id = int(cur.lastrowid)

        with pytest.raises(ka.AttachmentUnavailable):
            ka.resolve_attachment_download(conn, att_id)
    finally:
        conn.close()

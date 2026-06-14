"""Kanban attachment storage and download safety helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class AttachmentError(Exception):
    """Base class for attachment service failures."""


class InvalidAttachmentName(AttachmentError, ValueError):
    """The supplied filename cannot be safely stored."""


class AttachmentTooLarge(AttachmentError):
    """The upload exceeded the configured attachment size cap."""


class AttachmentNotFound(AttachmentError):
    """The task or attachment row does not exist."""


class AttachmentUnavailable(AttachmentError):
    """The attachment row exists but the blob cannot be safely served."""


@dataclass(frozen=True)
class AttachmentDownload:
    attachment: kanban_db.Attachment
    path: Path
    filename: str
    media_type: str


def safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise InvalidAttachmentName("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> tuple[str, Path]:
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists() or (dest_dir / candidate).is_symlink():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return candidate, dest_dir / candidate


async def store_attachment_upload(
    conn,
    task_id: str,
    *,
    filename: str,
    reader: Any,
    content_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    board: Optional[str] = None,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> kanban_db.Attachment:
    """Stream an upload into the task attachment directory and persist metadata."""
    if kanban_db.get_task(conn, task_id) is None:
        raise AttachmentNotFound(f"task {task_id} not found")

    safe_name = safe_attachment_name(filename)
    dest_dir = kanban_db.task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_name, dest_path = _collision_free_path(dest_dir, safe_name)

    total = 0
    created_path = False
    try:
        with open(dest_path, "xb") as out:
            created_path = True
            while True:
                chunk = await reader.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    out.close()
                    dest_path.unlink(missing_ok=True)
                    raise AttachmentTooLarge(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                out.write(chunk)
    except AttachmentTooLarge:
        raise
    except OSError as exc:
        if created_path:
            dest_path.unlink(missing_ok=True)
        raise AttachmentUnavailable(f"failed to store attachment: {exc}") from exc

    try:
        att_id = kanban_db.add_attachment(
            conn,
            task_id,
            filename=stored_name,
            stored_path=str(dest_path.resolve()),
            content_type=content_type,
            size=total,
            uploaded_by=(uploaded_by or "dashboard"),
            board=board,
        )
    except ValueError as exc:
        dest_path.unlink(missing_ok=True)
        raise AttachmentError(str(exc)) from exc

    att = kanban_db.get_attachment(conn, att_id)
    if att is None:
        dest_path.unlink(missing_ok=True)
        raise AttachmentUnavailable("attachment metadata was not persisted")
    return att


def resolve_attachment_download(
    conn,
    attachment_id: int,
    *,
    board: Optional[str] = None,
) -> AttachmentDownload:
    """Return a verified file path for a downloadable attachment."""
    att = kanban_db.get_attachment(conn, attachment_id)
    if att is None:
        raise AttachmentNotFound("attachment not found")

    root = kanban_db.task_attachments_dir(att.task_id, board=board).resolve()
    try:
        stored = Path(att.stored_path).resolve()
        stored.relative_to(root)
    except (ValueError, OSError) as exc:
        raise AttachmentUnavailable("attachment file unavailable") from exc
    if not stored.is_file():
        raise AttachmentUnavailable("attachment file missing on disk")

    return AttachmentDownload(
        attachment=att,
        path=stored,
        filename=att.filename,
        media_type=att.content_type or "application/octet-stream",
    )

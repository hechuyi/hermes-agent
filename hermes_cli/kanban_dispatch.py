"""Core helpers for kanban dispatch triggers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Callable, Optional

from hermes_cli import kanban_db


ConfigLoader = Callable[[], dict]


def _coerce_positive_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def dispatch_payload(
    conn,
    *,
    dry_run: bool = False,
    max_spawn: int = 8,
    board: Optional[str] = None,
    config_loader: Optional[ConfigLoader] = None,
) -> dict:
    """Run one dispatch pass and return an API-safe payload."""
    try:
        if config_loader is None:
            from hermes_cli.config import load_config

            config_loader = load_config
        config = config_loader() or {}
        kanban_cfg = (config.get("kanban") or {}) if isinstance(config, dict) else {}
    except Exception:
        kanban_cfg = {}

    result = kanban_db.dispatch_once(
        conn,
        dry_run=dry_run,
        max_spawn=max_spawn,
        max_in_progress=_coerce_positive_int(kanban_cfg.get("max_in_progress")),
        board=board,
        default_assignee=(kanban_cfg.get("default_assignee") or "").strip() or None,
        max_in_progress_per_profile=_coerce_positive_int(
            kanban_cfg.get("max_in_progress_per_profile")
        ),
    )
    try:
        return asdict(result)
    except TypeError:
        return {"result": str(result)}

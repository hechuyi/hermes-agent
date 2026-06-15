"""Core helpers for kanban home-channel subscriptions."""

from __future__ import annotations

from typing import Callable, Optional

from hermes_cli import kanban_db


class HomeChannelError(Exception):
    """Typed home-channel failure for HTTP/CLI adapters to map cleanly."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


ConfigLoader = Callable[[], object]
ActiveProfileLoader = Callable[[], Optional[str]]


def configured_home_channels(
    *,
    config_loader: Optional[ConfigLoader] = None,
) -> list[dict]:
    """Return configured gateway home channels in API-safe shape."""
    if config_loader is None:
        try:
            from gateway.config import load_gateway_config
        except Exception:
            return []
        config_loader = load_gateway_config

    try:
        gw_cfg = config_loader()
    except Exception:
        return []

    platforms = getattr(gw_cfg, "platforms", {}) or {}
    result: list[dict] = []
    for platform, pcfg in platforms.items():
        home_channel = getattr(pcfg, "home_channel", None)
        if not pcfg or not home_channel:
            continue
        result.append({
            "platform": getattr(platform, "value", str(platform)),
            "chat_id": home_channel.chat_id,
            "thread_id": home_channel.thread_id or "",
            "name": home_channel.name or "Home",
        })
    result.sort(key=lambda item: item["platform"])
    return result


def _home_key(home: dict) -> tuple[str, str, str]:
    return (
        str(home.get("platform") or ""),
        str(home.get("chat_id") or ""),
        str(home.get("thread_id") or ""),
    )


def _home_for_platform(
    platform: str,
    *,
    config_loader: Optional[ConfigLoader] = None,
) -> dict:
    home = next(
        (
            item
            for item in configured_home_channels(config_loader=config_loader)
            if item["platform"] == platform
        ),
        None,
    )
    if home is None:
        raise HomeChannelError(
            404,
            f"No home channel configured for platform {platform!r}. "
            f"Set one from the messenger via /sethome, or configure "
            f"gateway.platforms.{platform}.home_channel in config.yaml.",
        )
    return home


def _active_profile_name(
    *,
    active_profile_loader: Optional[ActiveProfileLoader] = None,
) -> str:
    if active_profile_loader is None:
        try:
            from hermes_cli.profiles import get_active_profile_name
        except Exception:
            return "default"
        active_profile_loader = get_active_profile_name
    try:
        return active_profile_loader() or "default"
    except Exception:
        return "default"


def home_channels_payload(
    conn,
    *,
    task_id: Optional[str] = None,
    config_loader: Optional[ConfigLoader] = None,
) -> dict:
    """List configured homes and whether the optional task is subscribed."""
    homes = configured_home_channels(config_loader=config_loader)
    subscribed_homes: set[tuple[str, str, str]] = set()
    if task_id:
        for sub in kanban_db.list_notify_subs(conn, task_id):
            subscribed_homes.add((
                str(sub.get("platform") or ""),
                str(sub.get("chat_id") or ""),
                str(sub.get("thread_id") or ""),
            ))
    return {
        "home_channels": [
            {**home, "subscribed": _home_key(home) in subscribed_homes}
            for home in homes
        ],
    }


def subscribe_home_channel(
    conn,
    task_id: str,
    platform: str,
    *,
    config_loader: Optional[ConfigLoader] = None,
    active_profile_loader: Optional[ActiveProfileLoader] = None,
) -> dict:
    """Subscribe a task to a configured platform home channel."""
    home = _home_for_platform(platform, config_loader=config_loader)
    if kanban_db.get_task(conn, task_id) is None:
        raise HomeChannelError(404, f"task {task_id} not found")
    kanban_db.add_notify_sub(
        conn,
        task_id=task_id,
        platform=platform,
        chat_id=home["chat_id"],
        thread_id=home["thread_id"] or None,
        notifier_profile=_active_profile_name(
            active_profile_loader=active_profile_loader,
        ),
    )
    return {"ok": True, "task_id": task_id, "home_channel": home}


def unsubscribe_home_channel(
    conn,
    task_id: str,
    platform: str,
    *,
    config_loader: Optional[ConfigLoader] = None,
) -> dict:
    """Remove a task subscription matching a configured platform home."""
    home = _home_for_platform(platform, config_loader=config_loader)
    kanban_db.remove_notify_sub(
        conn,
        task_id=task_id,
        platform=platform,
        chat_id=home["chat_id"],
        thread_id=home["thread_id"] or None,
    )
    return {"ok": True, "task_id": task_id, "home_channel": home}

"""
Shared platform registry for Hermes Agent.

Single source of truth for platform metadata consumed by both
skills_config (label display) and tools_config (default toolset
resolution). This Feishu runtime fork keeps platform selection static:
CLI, Feishu, API server, and cron.
"""

from collections import OrderedDict
from typing import NamedTuple

from runtime_profile import CLI_TOOL_PLATFORMS


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict(
    (key, PlatformInfo(label=info.label, default_toolset=info.default_toolset))
    for key, info in CLI_TOOL_PLATFORMS.items()
)


def platform_label(key: str, default: str = "") -> str:
    """Return the display label for a platform key, or *default*."""
    info = PLATFORMS.get(key)
    if info is not None:
        return info.label
    return default


def get_all_platforms() -> "OrderedDict[str, PlatformInfo]":
    """Return the static platform menu for this Feishu runtime fork."""
    return OrderedDict(PLATFORMS)

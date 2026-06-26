"""Runtime profile for this Feishu/Lark headless Hermes fork.

The profile is the positive boundary for runtime surfaces. Generic tool
capabilities live in the tool registry/toolsets; messaging channels and
adapter construction must be derived from this profile instead of inferred
from arbitrary platform names.
"""

from collections import OrderedDict
from typing import NamedTuple


class RuntimePlatformInfo(NamedTuple):
    """Metadata for one platform/tool profile entry."""

    label: str
    default_toolset: str


CLI_TOOL_PLATFORMS: "OrderedDict[str, RuntimePlatformInfo]" = OrderedDict(
    [
        ("cli", RuntimePlatformInfo(label="🖥️  CLI", default_toolset="hermes-cli")),
        ("feishu", RuntimePlatformInfo(label="🪽 Feishu", default_toolset="hermes-feishu")),
        (
            "api_server",
            RuntimePlatformInfo(label="🌐 API Server", default_toolset="hermes-api-server"),
        ),
        ("cron", RuntimePlatformInfo(label="⏰ Cron", default_toolset="hermes-cron")),
    ]
)


GATEWAY_RUNTIME_PLATFORM_VALUES = frozenset({"local", "feishu", "api_server"})


def is_gateway_runtime_platform(value: object) -> bool:
    """Return whether a gateway Platform value belongs to this runtime fork."""

    platform_value = getattr(value, "value", value)
    return str(platform_value) in GATEWAY_RUNTIME_PLATFORM_VALUES

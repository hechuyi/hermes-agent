"""Session-scoped progressive tool disclosure for Hermes tools."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"
BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})
CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True)
class ToolSearchConfig:
    enabled: str
    threshold_pct: float
    search_default_limit: int
    max_search_limit: int

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        if raw is True:
            return cls("auto", 10.0, 5, 20)
        if raw is False:
            return cls("off", 10.0, 5, 20)
        if not isinstance(raw, dict):
            return cls("auto", 10.0, 5, 20)

        enabled_raw = str(raw.get("enabled", "auto")).strip().lower()
        if enabled_raw in {"true", "1", "yes"}:
            enabled = "on"
        elif enabled_raw in {"false", "0", "no"}:
            enabled = "off"
        elif enabled_raw in {"auto", "on", "off"}:
            enabled = enabled_raw
        else:
            enabled = "auto"

        threshold_pct = max(0.0, min(100.0, _safe_float(raw.get("threshold_pct"), 10.0)))
        max_limit = max(1, min(50, _safe_int(raw.get("max_search_limit"), 20)))
        default_limit = max(1, min(max_limit, _safe_int(raw.get("search_default_limit"), 5)))
        return cls(enabled, threshold_pct, default_limit, max_limit)


@dataclass
class CatalogEntry:
    name: str
    description: str
    schema: dict[str, Any]
    source: str
    source_name: str
    _tokens: list[str] = field(default_factory=list)


@dataclass
class AssemblyResult:
    tool_defs: list[dict[str, Any]]
    activated: bool
    catalog: list[CatalogEntry]
    deferrable_tokens: int


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> ToolSearchConfig:
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        return ToolSearchConfig.from_raw(tools_cfg.get("tool_search"))
    except Exception:
        return ToolSearchConfig.from_raw(None)


def _core_tool_names() -> frozenset[str]:
    try:
        from toolsets import _HERMES_CORE_TOOLS

        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


def _toolset_for_name(name: str) -> str | None:
    try:
        from tools.registry import registry

        entry = registry.get_entry(name)
        return entry.toolset if entry else None
    except Exception:
        return None


def _is_feishu_family_toolset(toolset: str | None) -> bool:
    if not toolset:
        return False
    normalized = toolset.replace("_", "-").lower()
    return normalized.startswith("feishu") or normalized.startswith("hermes-feishu")


def is_deferrable_tool_name(name: str) -> bool:
    if not name or name in BRIDGE_TOOL_NAMES:
        return False
    if name in _core_tool_names():
        return False
    toolset = _toolset_for_name(name)
    if not toolset:
        return False
    if _is_feishu_family_toolset(toolset):
        return False
    return True


def classify_tools(
    tool_defs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    visible: list[dict[str, Any]] = []
    deferrable: list[dict[str, Any]] = []
    for tool_def in tool_defs:
        name = (tool_def.get("function") or {}).get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            continue
        if is_deferrable_tool_name(name):
            deferrable.append(tool_def)
        else:
            visible.append(tool_def)
    return visible, deferrable


def estimate_tokens_from_schemas(tool_defs: Iterable[dict[str, Any]]) -> int:
    chars = 0
    for tool_def in tool_defs:
        try:
            chars += len(json.dumps(tool_def, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            chars += len(str(tool_def))
    return int(math.ceil(chars / CHARS_PER_TOKEN))


def should_activate(
    config: ToolSearchConfig,
    deferrable_tokens: int,
    context_length: int | None,
) -> bool:
    if config.enabled == "off" or deferrable_tokens <= 0:
        return False
    if config.enabled == "on":
        return True
    if not context_length or context_length <= 0:
        return deferrable_tokens >= 20_000
    threshold_tokens = int(context_length * (config.threshold_pct / 100.0))
    return deferrable_tokens >= threshold_tokens


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text or "")]


def _entry_search_text(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function") or {}
    name = str(fn.get("name", ""))
    description = str(fn.get("description", "") or "")
    properties = ((fn.get("parameters") or {}).get("properties") or {})
    params = " ".join(str(key) for key in properties)
    name_words = name.replace("_", " ").replace("-", " ").replace(".", " ")
    return f"{name_words} {description} {params}"


def _source_for_name(name: str) -> tuple[str, str]:
    toolset = _toolset_for_name(name) or ""
    if toolset.startswith("mcp-"):
        return "mcp", toolset
    return "plugin", toolset


def build_catalog(tool_defs: list[dict[str, Any]]) -> list[CatalogEntry]:
    _visible, deferrable = classify_tools(tool_defs)
    catalog: list[CatalogEntry] = []
    for tool_def in deferrable:
        fn = tool_def.get("function") or {}
        name = str(fn.get("name", ""))
        source, source_name = _source_for_name(name)
        entry = CatalogEntry(
            name=name,
            description=str(fn.get("description", "") or ""),
            schema=tool_def,
            source=source,
            source_name=source_name,
        )
        entry._tokens = _tokenize(_entry_search_text(tool_def))
        catalog.append(entry)
    return catalog


def search_catalog(catalog: list[CatalogEntry], query: str, limit: int = 5) -> list[CatalogEntry]:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    scored: list[tuple[float, CatalogEntry]] = []
    for entry in catalog:
        token_set = set(entry._tokens)
        score = sum(1.0 for token in query_tokens if token in token_set)
        lowered_query = " ".join(query_tokens)
        haystack = f"{entry.name} {entry.description}".lower()
        if lowered_query and lowered_query in haystack:
            score += 2.0
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [entry for _score, entry in scored[: max(1, int(limit))]]


def _bridge_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def bridge_tool_defs(config: ToolSearchConfig) -> list[dict[str, Any]]:
    return [
        _bridge_schema(
            TOOL_SEARCH_NAME,
            "Search session-scoped deferred plugin and MCP tools.",
            {
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": config.max_search_limit,
                    "default": config.search_default_limit,
                },
            },
            ["query"],
        ),
        _bridge_schema(
            TOOL_DESCRIBE_NAME,
            "Describe one session-scoped deferred tool by name.",
            {"name": {"type": "string"}},
            ["name"],
        ),
        _bridge_schema(
            TOOL_CALL_NAME,
            "Call one session-scoped deferred tool by name with JSON arguments.",
            {
                "name": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            ["name", "arguments"],
        ),
    ]


def assemble_tool_defs(
    tool_defs: list[dict[str, Any]],
    *,
    context_length: int | None = None,
    config: ToolSearchConfig | None = None,
) -> AssemblyResult:
    config = config or load_config()
    visible, deferrable = classify_tools(tool_defs)
    catalog = build_catalog(tool_defs)
    deferrable_tokens = estimate_tokens_from_schemas(deferrable)
    if not should_activate(config, deferrable_tokens, context_length):
        return AssemblyResult(visible + deferrable, False, catalog, deferrable_tokens)
    return AssemblyResult(visible + bridge_tool_defs(config), True, catalog, deferrable_tokens)


def scoped_deferrable_names(tool_defs: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(entry.name for entry in build_catalog(tool_defs))


def _catalog_by_name(tool_defs: list[dict[str, Any]]) -> dict[str, CatalogEntry]:
    return {entry.name: entry for entry in build_catalog(tool_defs)}


def dispatch_tool_search(args: dict[str, Any], *, current_tool_defs: list[dict[str, Any]]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "tool_search requires a query"}, ensure_ascii=False)
    config = load_config()
    catalog = build_catalog(current_tool_defs)
    limit = max(1, min(config.max_search_limit, _safe_int(args.get("limit"), config.search_default_limit)))
    matches = [
        {
            "name": entry.name,
            "description": entry.description,
            "source": entry.source,
            "source_name": entry.source_name,
        }
        for entry in search_catalog(catalog, query, limit)
    ]
    return json.dumps(
        {"matches": matches, "total_available": len(catalog)},
        ensure_ascii=False,
    )


def dispatch_tool_describe(args: dict[str, Any], *, current_tool_defs: list[dict[str, Any]]) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "tool_describe requires a name"}, ensure_ascii=False)
    entry = _catalog_by_name(current_tool_defs).get(name)
    if entry is None:
        return json.dumps(
            {"error": f"Tool {name!r} is not available in this session or is not a deferrable tool"},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "name": entry.name,
            "description": entry.description,
            "source": entry.source,
            "source_name": entry.source_name,
            "schema": entry.schema,
        },
        ensure_ascii=False,
    )


def resolve_underlying_call(
    args: dict[str, Any],
    *,
    allowed_names: frozenset[str] | None = None,
) -> tuple[str | None, dict[str, Any], str | None]:
    name = str(args.get("name") or "").strip()
    if not name:
        return None, {}, "tool_call requires a tool name"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, "tool_call cannot invoke another bridge tool"

    raw_arguments = args.get("arguments", {})
    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return None, {}, "tool_call arguments are not valid JSON"
    if raw_arguments is None:
        raw_arguments = {}
    if not isinstance(raw_arguments, dict):
        return None, {}, "tool_call arguments must be a JSON object"

    if allowed_names is not None and name not in allowed_names:
        return None, {}, f"Tool {name!r} is not available in this session"
    if allowed_names is None and not is_deferrable_tool_name(name):
        return None, {}, f"Tool {name!r} is not a deferrable tool; call it directly"
    return name, raw_arguments, None

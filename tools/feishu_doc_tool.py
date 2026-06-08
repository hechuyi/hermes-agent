"""Feishu Document Tool -- read document content via Feishu/Lark API.

Provides ``feishu_doc_read`` for reading document content as plain text.
Uses the same lazy-import + BaseRequest pattern as feishu_comment.py.
"""

import json
import logging
import threading

from gateway.feishu_legacy_guard import (
    LEGACY_FEISHU_BROKER_DENIAL_REASON,
    audit_feishu_legacy_tool_denial,
    require_feishu_broker_context,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Thread-local storage for the lark client injected by feishu_comment handler.
_local = threading.local()


def set_client(client):
    """Store a lark client for the current thread (called by feishu_comment)."""
    _local.client = client


def get_client():
    """Return the lark client for the current thread, or None."""
    return getattr(_local, "client", None)


# ---------------------------------------------------------------------------
# feishu_doc_read
# ---------------------------------------------------------------------------

_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"

FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": (
        "Read the full content of a Feishu/Lark document as plain text. "
        "Useful when you need more context beyond the quoted text in a comment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {
                "type": "string",
                "description": "The document token (from the document URL or comment context).",
            },
        },
        "required": ["doc_token"],
    },
}


def _check_feishu():
    allowed, _ = require_feishu_broker_context("tool", "feishu_doc_read")
    if not allowed:
        return False
    # Use ``importlib.util.find_spec`` — it checks whether ``lark_oapi``
    # is importable without actually executing its ``__init__``.
    # Executing the real import here costs ~5 seconds (the SDK eagerly
    # loads websockets, dispatcher, every api/v2 model) and this probe
    # fires at every ``hermes`` startup during tool-availability
    # evaluation.  Correctness is preserved because the actual tool
    # handler still does the real import when invoked.
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


_check_feishu._hermes_context_sensitive = True


def _legacy_denial_result(tool: str, surface: str) -> str:
    audit_result = audit_feishu_legacy_tool_denial(surface=surface, tool=tool)
    if not audit_result.ok:
        return tool_error(
            "Feishu legacy denial audit failed",
            success=False,
            failure_class="feishu_denial_audit_unavailable",
            audit_failure_class=audit_result.failure_class or "gateway_event_apply_failed",
            audit_event="feishu_legacy_tool_denied",
        )
    return tool_error(
        LEGACY_FEISHU_BROKER_DENIAL_REASON,
        success=False,
        failure_class=LEGACY_FEISHU_BROKER_DENIAL_REASON,
        audit_event="feishu_legacy_tool_denied",
    )


def _handle_feishu_doc_read(args: dict, **kwargs) -> str:
    allowed, _ = require_feishu_broker_context("tool", "feishu_doc_read", args=args)
    if not allowed:
        return _legacy_denial_result("feishu_doc_read", "doc")

    doc_token = args.get("doc_token", "").strip()
    if not doc_token:
        return tool_error("doc_token is required")

    client = get_client()
    if client is None:
        return tool_error("Feishu client not available (not in a Feishu comment context)")

    try:
        from lark_oapi import AccessTokenType
        from lark_oapi.core.enum import HttpMethod
        from lark_oapi.core.model.base_request import BaseRequest
    except ImportError:
        return tool_error("lark_oapi not installed")

    request = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET)
        .uri(_RAW_CONTENT_URI)
        .token_types({AccessTokenType.TENANT})
        .paths({"document_id": doc_token})
        .build()
    )

    # Tool handlers run synchronously in a worker thread (no running event
    # loop), so call the blocking lark client directly.
    response = client.request(request)

    code = getattr(response, "code", None)
    if code != 0:
        msg = getattr(response, "msg", "unknown error")
        return tool_error(f"Failed to read document: code={code} msg={msg}")

    raw = getattr(response, "raw", None)
    if raw and hasattr(raw, "content"):
        try:
            body = json.loads(raw.content)
            content = body.get("data", {}).get("content", "")
            return tool_result(success=True, content=content)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Fallback: try response.data
    data = getattr(response, "data", None)
    if data:
        if isinstance(data, dict):
            content = data.get("content", "")
        else:
            content = getattr(data, "content", str(data))
        return tool_result(success=True, content=content)

    return tool_error("No content returned from document API")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="feishu_doc_read",
    toolset="feishu_doc",
    schema=FEISHU_DOC_READ_SCHEMA,
    handler=_handle_feishu_doc_read,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="Read Feishu document content",
    emoji="\U0001f4c4",
)

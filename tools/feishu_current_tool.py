"""Package B Feishu current-conversation model-visible capability schemas."""

from __future__ import annotations

from typing import Any

from gateway.feishu_legacy_guard import current_feishu_broker_context
from tools.registry import registry, tool_error


def _string_property(description: str, *, max_length: int = 4000) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        "maxLength": max_length,
    }


def _object_schema(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _fail_closed_current_capability(args: dict[str, Any], **kwargs: Any) -> str:
    del args, kwargs
    if current_feishu_broker_context() is None:
        return tool_error(
            "feishu_current_conversation_broker_required",
            failure_class="feishu_current_conversation_broker_required",
        )
    return tool_error(
        "feishu_current_conversation_broker_unavailable",
        failure_class="feishu_current_conversation_broker_unavailable",
    )


_REPLY_SEND_SCHEMA = _object_schema(
    name="feishu.current.reply.send",
    description="Send a reply in the current brokered Feishu conversation.",
    properties={
        "text": _string_property("Reply text for the current conversation."),
    },
    required=["text"],
)

_REPLY_EDIT_SCHEMA = _object_schema(
    name="feishu.current.reply.edit_bot_owned",
    description="Edit a bot-owned reply in the current brokered Feishu conversation.",
    properties={
        "text": _string_property("Replacement reply text for the current conversation."),
    },
    required=["text"],
)

_CLARIFICATION_CARD_SCHEMA = _object_schema(
    name="feishu.current.card.clarification.create",
    description="Create a clarification card in the current brokered Feishu conversation.",
    properties={
        "question": _string_property("Clarification question for the current conversation."),
    },
    required=["question"],
)

_CONFIRMATION_CARD_SCHEMA = _object_schema(
    name="feishu.current.card.confirmation.create",
    description="Create a confirmation card in the current brokered Feishu conversation.",
    properties={
        "prompt": _string_property("Confirmation prompt for the current conversation."),
    },
    required=["prompt"],
)

_ECHO_PROVENANCE_SCHEMA = _object_schema(
    name="feishu.current.attachment.echo_provenance",
    description="Echo sanitized provenance for an inbound attachment in the current brokered Feishu conversation.",
    properties={
        "provenance_note": _string_property(
            "Sanitized provenance note for the current conversation.",
            max_length=1000,
        ),
    },
    required=["provenance_note"],
)

_SEND_GENERATED_SCHEMA = _object_schema(
    name="feishu.current.attachment.send_generated",
    description="Send a generated attachment in the current brokered Feishu conversation.",
    properties={
        "label": _string_property(
            "Sanitized generated attachment label for the current conversation.",
            max_length=200,
        ),
    },
    required=["label"],
)

registry.register(
    name="feishu.current.reply.send",
    toolset="hermes-feishu",
    schema=_REPLY_SEND_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_REPLY_SEND_SCHEMA["description"],
    emoji="",
)

registry.register(
    name="feishu.current.reply.edit_bot_owned",
    toolset="hermes-feishu",
    schema=_REPLY_EDIT_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_REPLY_EDIT_SCHEMA["description"],
    emoji="",
)

registry.register(
    name="feishu.current.card.clarification.create",
    toolset="hermes-feishu",
    schema=_CLARIFICATION_CARD_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_CLARIFICATION_CARD_SCHEMA["description"],
    emoji="",
)

registry.register(
    name="feishu.current.card.confirmation.create",
    toolset="hermes-feishu",
    schema=_CONFIRMATION_CARD_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_CONFIRMATION_CARD_SCHEMA["description"],
    emoji="",
)

registry.register(
    name="feishu.current.attachment.echo_provenance",
    toolset="hermes-feishu",
    schema=_ECHO_PROVENANCE_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_ECHO_PROVENANCE_SCHEMA["description"],
    emoji="",
)

registry.register(
    name="feishu.current.attachment.send_generated",
    toolset="hermes-feishu",
    schema=_SEND_GENERATED_SCHEMA,
    handler=_fail_closed_current_capability,
    description=_SEND_GENERATED_SCHEMA["description"],
    emoji="",
)

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import PlatformConfig
from gateway.feishu_action_plan import (
    FeishuActionContract,
    RenderPlan,
    RenderPlanPart,
)
from gateway.feishu_contracts import FeishuContractError, feishu_hashed_ref
from gateway.gateway_event_ledger import LEDGER_FILENAME
from gateway.platforms.feishu import FeishuAdapter


_TARGET_HASH = "sha256:" + "a" * 64
_OBJECT_HASH = "sha256:" + "b" * 64
_PAYLOAD_HASH = "sha256:" + "c" * 64
_PROVENANCE_HASH = "sha256:" + "d" * 64
_ROUTE_HASH = "sha256:" + "e" * 64
_ACTION_DIGEST = "sha256:" + "f" * 64
_CHUNK_GROUP = "sha256:" + "1" * 64


class _FakeResponse:
    def __init__(
        self,
        *,
        ok: bool = True,
        message_id: str | None = "msg_plan_1",
        code: int = 0,
        msg: str = "ok",
    ) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = SimpleNamespace(message_id=message_id) if message_id else None

    def success(self) -> bool:
        return self._ok


def _sha(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _part(part_type: str, seed: str, **overrides) -> RenderPlanPart:
    values = {
        "part_type": part_type,
        "content_hash": _sha(f"content:{seed}"),
        "payload_hash": _sha(f"payload:{seed}"),
        "fallback_action": "preserve",
    }
    values.update(overrides)
    return RenderPlanPart(**values)


def _action_contract(action_kind: str = "button") -> FeishuActionContract:
    return FeishuActionContract(
        action_kind=action_kind,
        action_digest=_ACTION_DIGEST,
        same_operator_scope=True,
        expires_at="2026-06-08T10:30:00Z",
        route_snapshot_hash=_ROUTE_HASH,
        payload_hash=_PAYLOAD_HASH,
    )


def _plan(parts: tuple[RenderPlanPart, ...]) -> RenderPlan:
    return RenderPlan(
        target_ref_hash=_TARGET_HASH,
        object_ref_hash=_OBJECT_HASH,
        render_mode="offline_snapshot",
        parts=parts,
    )


def _admission(
    *,
    evidence_state: str = "current",
    reply_anchor: str = "msg_anchor_plan",
) -> dict[str, str]:
    reply_ref = feishu_hashed_ref("feishu_reply_anchor", reply_anchor)
    assert reply_ref is not None
    return {
        "canonical_event_ref": _sha("event:plan"),
        "contract_hash": _sha("contract:plan"),
        "route_partition_key": "route:plan",
        "route_session_key_snapshot": "route-snapshot-plan",
        "actor_ref": _sha("actor:plan"),
        "authority_subject_ref": _sha("authority:plan"),
        "transport_kind": "dm",
        "reply_anchor_ref": reply_ref.value_hash,
        "evidence_state": evidence_state,
    }


def _metadata(*, delivery_id: str = "delivery-plan") -> dict[str, object]:
    return {
        "delivery_id": delivery_id,
        "inbound_id": "inbound-plan",
        "session_id": "session-plan",
        "correlation_id": "correlation-plan",
        "feishu_current_admission": _admission(),
    }


def _adapter(tmp_path, *, reply_side_effect=None) -> FeishuAdapter:
    adapter = FeishuAdapter(
        PlatformConfig(
            extra={
                "app_id": "app_plan",
                "tenant_partition_key": "tenant:plan",
                "app_partition_key": "app:plan",
                "gateway_event_state_dir": str(tmp_path),
            }
        )
    )
    reply = Mock(side_effect=reply_side_effect)
    if reply_side_effect is None:
        reply.side_effect = [
            _FakeResponse(message_id=f"msg_plan_{index}") for index in range(1, 40)
        ]
    message = SimpleNamespace(create=Mock(), reply=reply, update=Mock())
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=message,
                image=SimpleNamespace(create=Mock()),
                file=SimpleNamespace(create=Mock()),
            )
        )
    )
    return adapter


def _request_bodies(adapter: FeishuAdapter) -> list[SimpleNamespace]:
    calls = adapter._client.im.v1.message.reply.call_args_list
    return [call.args[0].request_body for call in calls]


def _payload_text(body: SimpleNamespace) -> str:
    payload = json.loads(body.content)
    if body.msg_type == "text":
        return str(payload["text"])
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _state(tmp_path) -> dict:
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        return json.load(handle)


def _lifecycle_events(tmp_path, event_type: str | None = None) -> list[dict]:
    events = _state(tmp_path).get("feishu_delivery_lifecycle", [])
    if event_type is None:
        return events
    return [event for event in events if event["type"] == event_type]


def _ledger_text(tmp_path) -> str:
    return (tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8")


def _assert_no_success_lifecycle(tmp_path) -> None:
    if not (tmp_path / LEDGER_FILENAME).exists():
        return
    assert _lifecycle_events(tmp_path, "feishu_delivery_sent") == []
    assert _lifecycle_events(tmp_path, "feishu_delivery_ack_unknown") == []


@pytest.mark.asyncio
async def test_render_plan_supported_parts_use_current_lifecycle_route(tmp_path):
    parts = (
        _part("plain_text", "text", metadata={"format": "text"}),
        _part("post", "post", metadata={"format": "feishu_post"}),
        _part("markdown", "markdown", metadata={"format": "commonmark"}),
        _part("code_block", "code", metadata={"language": "python"}),
        _part("table", "table", metadata={"columns_hash": _sha("columns")}),
        _part("link", "link", metadata={"href_hash": _sha("href")}),
    )
    adapter = _adapter(tmp_path)

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan(parts),
        rendered_parts={
            parts[0].part_hash: "alpha text",
            parts[1].part_hash: "beta post",
            parts[2].part_hash: "gamma **markdown**",
            parts[3].part_hash: {"code": "print('delta')", "language": "python"},
            parts[4].part_hash: {"headers": ["k", "v"], "rows": [["e", "z"]]},
            parts[5].part_hash: {"label": "eta", "href": "https://example.invalid/eta"},
        },
        reply_to="msg_anchor_plan",
        metadata=_metadata(),
    )

    assert result.success is True
    assert adapter._client.im.v1.message.create.call_count == 0
    assert adapter._client.im.v1.message.reply.call_count == len(parts)
    bodies = _request_bodies(adapter)
    assert [body.msg_type for body in bodies] == [
        "text",
        "post",
        "post",
        "post",
        "text",
        "post",
    ]
    rendered = "\n".join(_payload_text(body) for body in bodies)
    for marker in ("alpha text", "beta post", "gamma", "delta", "eta"):
        assert marker in rendered
    assert [event["type"] for event in _lifecycle_events(tmp_path)] == [
        item
        for _ in parts
        for item in (
            "feishu_delivery_attempted",
            "feishu_delivery_sent",
            "feishu_delivery_ack_unknown",
        )
    ]


@pytest.mark.asyncio
async def test_render_plan_current_success_ledger_persists_only_sanitized_evidence(tmp_path):
    raw_chat_id = "oc_fake_render_plan_chat_0001"
    raw_reply_anchor = "om_fake_render_plan_reply_anchor_0001"
    raw_message_id = "om_fake_render_plan_sent_0001"
    raw_path_marker = "/fake/raw/render-plan/path/secret.txt"
    original_content = (
        "original render content that must stay out of ledger "
        "om_fake_render_plan_body_0001"
    )
    part = _part("plain_text", "sanitized-ledger", metadata={"format": "text"})
    adapter = _adapter(
        tmp_path,
        reply_side_effect=[_FakeResponse(message_id=raw_message_id)],
    )
    metadata = _metadata(delivery_id="delivery-render-sanitized")
    metadata["feishu_current_admission"] = _admission(reply_anchor=raw_reply_anchor)

    result = await adapter.send_render_plan(
        chat_id=raw_chat_id,
        plan=_plan((part,)),
        rendered_parts={part.part_hash: f"{original_content}\n{raw_path_marker}"},
        reply_to=raw_reply_anchor,
        metadata=metadata,
    )

    assert result.success is True
    result_text = repr(result)
    assert raw_message_id not in result_text
    assert result.message_id is not None
    assert result.message_id.startswith("feishu_render_plan_message_")
    request_body = _request_bodies(adapter)[0].content
    state = _state(tmp_path)
    ledger_text = _ledger_text(tmp_path)
    state_text = json.dumps(state, sort_keys=True, ensure_ascii=False)
    for forbidden in (
        raw_chat_id,
        raw_reply_anchor,
        raw_message_id,
        raw_path_marker,
        original_content,
        request_body,
        str(tmp_path),
    ):
        assert forbidden not in ledger_text
        assert forbidden not in state_text

    legacy_record = next(iter(state["deliveries"].values()))
    assert legacy_record["status"] == "sent"
    assert legacy_record["target"].startswith("feishu:render_plan_target:")
    assert legacy_record["inbound_id"].startswith("feishu:render_plan_inbound:")
    assert legacy_record["feishu_message_id"].startswith("feishu_render_plan_message_")
    assert raw_chat_id not in json.dumps(legacy_record, sort_keys=True)
    assert raw_message_id not in json.dumps(legacy_record, sort_keys=True)

    lifecycle_events = _lifecycle_events(tmp_path)
    assert [event["type"] for event in lifecycle_events] == [
        "feishu_delivery_attempted",
        "feishu_delivery_sent",
        "feishu_delivery_ack_unknown",
    ]
    for event in lifecycle_events:
        assert event["evidence_state"] == "current"
        assert event["target_ref_hash"].startswith("sha256:")
        assert event["delivery_hash"].startswith("sha256:")
        assert "message_id" not in event
        assert "message_ref_hash" not in event or event["message_ref_hash"].startswith("sha256:")
    assert lifecycle_events[1]["message_ref_hash"].startswith("sha256:")
    assert lifecycle_events[1]["bot_ownership_hash"].startswith("sha256:")
    assert lifecycle_events[2]["message_ref_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_chunked_render_plan_preserves_order_and_stops_on_failed_chunk(tmp_path):
    parts = tuple(
        _part(
            "plain_text",
            f"chunk:{index}",
            chunk_group=_CHUNK_GROUP,
            chunk_index=index,
            chunk_count=3,
            metadata={"chunk_profile": "ordered"},
        )
        for index in range(3)
    )
    adapter = _adapter(
        tmp_path,
        reply_side_effect=[
            _FakeResponse(message_id="msg_chunk_1"),
            _FakeResponse(ok=False, message_id=None, code=190001, msg="denied"),
            _FakeResponse(message_id="msg_chunk_3"),
        ],
    )

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan(parts),
        rendered_parts={part.part_hash: f"chunk {index}" for index, part in enumerate(parts)},
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id="delivery-chunk-plan"),
    )

    assert result.success is False
    assert result.error == "feishu_terminal_non_acceptance"
    assert adapter._client.im.v1.message.reply.call_count == 2
    assert [_payload_text(body) for body in _request_bodies(adapter)] == [
        "chunk 0",
        "chunk 1",
    ]
    assert result.raw_response == {
        "failure_class": "feishu_terminal_non_acceptance",
        "failed_part_index": 1,
        "failed_chunk_index": 1,
        "chunk_count": 3,
        "chunk_group_hash": _CHUNK_GROUP,
        "sent_part_count": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_action", "expected_calls", "expected_text"),
    [
        ("preserve", 1, "preserve shadow"),
        ("replace", 1, "replacement shadow"),
        ("drop", 0, None),
    ],
)
async def test_unsupported_part_fallback_matrix_is_deterministic(
    tmp_path,
    fallback_action,
    expected_calls,
    expected_text,
):
    part = _part(
        "button",
        fallback_action,
        fallback_action=fallback_action,
        action_contract=_action_contract("button"),
    )
    adapter = _adapter(tmp_path)

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan((part,)),
        rendered_parts={
            part.part_hash: {
                "preserve_text": "preserve shadow",
                "replacement_text": "replacement shadow",
            }
        },
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id=f"delivery-fallback-{fallback_action}"),
    )

    assert result.success is True
    assert adapter._client.im.v1.message.reply.call_count == expected_calls
    if expected_text is None:
        assert result.message_id is None
        _assert_no_success_lifecycle(tmp_path)
    else:
        assert _payload_text(_request_bodies(adapter)[0]) == expected_text
    assert result.raw_response["fallback_events"] == [
        {
            "part_hash": part.part_hash,
            "part_index": 0,
            "part_type": "button",
            "fallback_action": fallback_action,
            "outcome": "sent" if expected_calls else "dropped",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_action", "text_key"),
    [
        ("preserve", "preserve_text"),
        ("replace", "replacement_text"),
    ],
)
async def test_unsupported_part_fallback_fails_closed_when_split_chunk_exceeds_limit(
    tmp_path,
    fallback_action,
    text_key,
):
    part = _part(
        "button",
        f"oversized-fallback-{fallback_action}",
        fallback_action=fallback_action,
        action_contract=_action_contract("button"),
    )
    adapter = _adapter(tmp_path)
    adapter.MAX_MESSAGE_LENGTH = 24
    long_fence_language = "x" * 80
    fallback_text = f"```{long_fence_language}\n" + ("opaque fallback body\n" * 4)

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan((part,)),
        rendered_parts={part.part_hash: {text_key: fallback_text}},
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id=f"delivery-fallback-too-large-{fallback_action}"),
    )

    assert result.success is False
    assert result.error == "feishu_render_part_too_large"
    assert adapter._client.im.v1.message.reply.call_count == 0
    assert adapter._client.im.v1.message.create.call_count == 0
    _assert_no_success_lifecycle(tmp_path)


@pytest.mark.asyncio
async def test_oversized_render_content_is_chunked_not_silently_truncated(tmp_path):
    part = _part("plain_text", "oversized", metadata={"format": "text"})
    adapter = _adapter(tmp_path)
    adapter.MAX_MESSAGE_LENGTH = 24
    content = "abcdefghijklmnopqrstuvwxyz0123456789"

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan((part,)),
        rendered_parts={part.part_hash: content},
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id="delivery-oversized"),
    )

    assert result.success is True
    assert adapter._client.im.v1.message.reply.call_count > 1
    delivered = "\n".join(_payload_text(body) for body in _request_bodies(adapter))
    assert "abcdef" in delivered
    assert "6789" in delivered
    assert delivered != content[:24]
    assert result.error != "feishu_render_part_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("part_type", ["image", "file", "attachment"])
async def test_attachment_parts_are_denied_before_upload_until_provenance_gate_exists(
    tmp_path,
    part_type,
):
    part = _part(
        part_type,
        part_type,
        source_class="generated",
        provenance_hash=_PROVENANCE_HASH,
    )
    adapter = _adapter(tmp_path)

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan((part,)),
        rendered_parts={part.part_hash: {"object_ref_hash": _sha(part_type)}},
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id=f"delivery-deny-{part_type}"),
    )

    assert result.success is False
    assert result.error == "feishu_render_attachment_provenance_required"
    assert adapter._client.im.v1.image.create.call_count == 0
    assert adapter._client.im.v1.file.create.call_count == 0
    assert adapter._client.im.v1.message.reply.call_count == 0
    assert adapter._client.im.v1.message.create.call_count == 0
    _assert_no_success_lifecycle(tmp_path)


def test_local_path_render_part_is_denied_at_contract_boundary():
    with pytest.raises(FeishuContractError) as exc_info:
        _part(
            "local_path",
            "local_path",
            source_class="generated",
            provenance_hash=_PROVENANCE_HASH,
        )

    assert exc_info.value.failure_class == "feishu_arbitrary_local_upload_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rendered_part",
    [
        {"method": "POST"},
        {"path": "opaque-route"},
        {"body": {"schemaversion": 1}},
        {"openapi_descriptor": {"schemaversion": 1}},
        {"sdk_request": {"schemaversion": 1}},
        {"request_body": {"schemaversion": 1}},
    ],
)
async def test_raw_descriptor_material_is_rejected_before_send_with_zero_sdk_calls(
    tmp_path,
    rendered_part,
):
    part = _part("plain_text", "raw-denied", metadata={"format": "text"})
    adapter = _adapter(tmp_path)

    result = await adapter.send_render_plan(
        chat_id="chat_plan",
        plan=_plan((part,)),
        rendered_parts={part.part_hash: rendered_part},
        reply_to="msg_anchor_plan",
        metadata=_metadata(delivery_id="delivery-raw-denied"),
    )

    assert result.success is False
    assert result.error == "feishu_action_raw_tool_material"
    assert adapter._client.im.v1.message.reply.call_count == 0
    assert adapter._client.im.v1.message.create.call_count == 0
    _assert_no_success_lifecycle(tmp_path)


def test_raw_descriptor_keys_are_rejected_from_render_plan_metadata():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="plain_text",
            content_hash=_sha("raw-metadata"),
            payload_hash=_sha("raw-metadata-payload"),
            fallback_action="preserve",
            metadata={"sdk_request": {"schemaversion": 1}},
        )

    assert exc_info.value.failure_class == "feishu_action_raw_tool_material"

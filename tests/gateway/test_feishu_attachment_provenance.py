import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.feishu_action_plan import RenderPlanPart
from gateway.feishu_contracts import feishu_hashed_ref
from gateway.feishu_contracts import FeishuContractError
from gateway.gateway_event_ledger import (
    apply_gateway_event,
    feishu_attachment_provenance_record,
)
from gateway.platforms.feishu import FeishuAdapter


_EVENT_HASH = "sha256:" + "1" * 64
_FILE_KEY_HASH = "sha256:" + "2" * 64
_ROUTE_HASH = "sha256:" + "3" * 64
_CONTRACT_HASH = "sha256:" + "4" * 64
_CONTENT_BYTES = b"\x89PNG\r\n\x1a\nhermes-b6-provenance"
_CONTENT_HASH = "sha256:" + hashlib.sha256(_CONTENT_BYTES).hexdigest()
_PLAN_HASH = "sha256:" + "6" * 64
_ROOT_PROOF_HASH = "sha256:" + "7" * 64
_TOOL_ACTION_HASH = "sha256:" + "8" * 64
_GRANT_HANDLE = "broker_grant_handle:sha256:" + "9" * 64
_PROVENANCE_HASH_RE = r"^sha256:[a-f0-9]{64}$"
_REPLY_TO = "om_parent"
_REPLY_REF = feishu_hashed_ref("feishu_reply_anchor", _REPLY_TO).value_hash


class _FakeResponse:
    def __init__(self, *, message_id="om_sent"):
        self.code = 0
        self.msg = "ok"
        self.data = SimpleNamespace(message_id=message_id)

    def success(self):
        return True


class _FakeMessageApi:
    def __init__(self):
        self.create_calls = []
        self.reply_calls = []
        self.create_response = _FakeResponse(message_id="om_created")

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response

    def reply(self, request):
        self.reply_calls.append(request)
        return _FakeResponse(message_id="om_reply")


class _FakeImageApi:
    def __init__(self):
        self.create_calls = []
        self.create_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(image_key="img_uploaded"),
        )

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response


class _FakeFileApi:
    def __init__(self):
        self.create_calls = []
        self.create_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(file_key="file_uploaded"),
        )

    def create(self, request):
        self.create_calls.append(request)
        return self.create_response


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
    message_api = _FakeMessageApi()
    image_api = _FakeImageApi()
    file_api = _FakeFileApi()
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=message_api,
                image=image_api,
                file=file_api,
            )
        )
    )
    return adapter, image_api, file_api, message_api


def _metadata(provenance_hash, **overrides):
    values = {
        "delivery_id": "delivery-provenance",
        "inbound_id": "inbound-provenance",
        "session_id": "session-provenance",
        "correlation_id": "corr-provenance",
        "feishu_current_admission": {
            "evidence_state": "current",
            "route_partition_key": "route-partition-b6",
            "route_session_key_snapshot": "route-session-b6",
            "route_partition_hash": _ROUTE_HASH,
            "route_snapshot_hash": _ROUTE_HASH,
            "contract_hash": _CONTRACT_HASH,
            "reply_anchor_ref": _REPLY_REF,
            "actor_ref": "actor-ref-b6",
            "actor_hash": "sha256:" + "b" * 64,
            "authority_subject_ref": "authority-ref-b6",
            "transport_kind": "dm",
        },
        "feishu_attachment_provenance_hash": provenance_hash,
        "feishu_attachment_declared_mime_class": "image",
        "feishu_attachment_size_class": "small",
        "feishu_attachment_content_hash": _CONTENT_HASH,
        "feishu_attachment_source_grant_handles": (_GRANT_HANDLE,),
    }
    values.update(overrides)
    return values


def _generated_event(**overrides):
    values = {
        "type": "feishu_attachment_provenance_recorded",
        "provenance_kind": "generated",
        "safe_output_root_proof_hash": _ROOT_PROOF_HASH,
        "producing_tool_action_hash": _TOOL_ACTION_HASH,
        "content_hash": _CONTENT_HASH,
        "declared_mime_class": "image",
        "size_class": "small",
        "route_partition_hash": _ROUTE_HASH,
        "route_snapshot_hash": _ROUTE_HASH,
        "contract_hash": _CONTRACT_HASH,
        "delivery_plan_hash": _PLAN_HASH,
        "sensitivity_classification": "internal",
        "sensitivity_state": "current",
        "redaction_state": "redacted",
        "retention_policy": "ephemeral",
        "retention_state": "current",
        "generator_state": "complete",
        "source_grant_handles": [_GRANT_HANDLE],
        "timestamp": 1_718_000_000,
    }
    values.update(overrides)
    return values


def _inbound_event(**overrides):
    values = {
        "type": "feishu_attachment_provenance_recorded",
        "provenance_kind": "inbound_user_attachment",
        "source_event_hash": _EVENT_HASH,
        "file_key_hash": _FILE_KEY_HASH,
        "declared_mime_class": "image",
        "size_class": "small",
        "retention_class": "ephemeral",
        "retention_state": "current",
        "route_partition_hash": _ROUTE_HASH,
        "route_snapshot_hash": _ROUTE_HASH,
        "contract_hash": _CONTRACT_HASH,
        "sensitivity_classification": "internal",
        "sensitivity_state": "current",
        "redaction_state": "redacted",
        "timestamp": 1_718_000_000,
    }
    values.update(overrides)
    return values


def _state(tmp_path):
    state_path = tmp_path / "gateway_event_ledger.json"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())


def test_inbound_user_attachment_provenance_records_sanitized_closure(tmp_path):
    result = apply_gateway_event(_inbound_event(), tmp_path)

    assert result.ok is True
    record = result.action["record"]
    assert record["provenance_hash"].startswith("sha256:")
    assert record["source_event_hash"] == _EVENT_HASH
    assert record["file_key_hash"] == _FILE_KEY_HASH
    assert record["declared_mime_class"] == "image"
    assert record["size_class"] == "small"
    assert record["retention_class"] == "ephemeral"
    assert record["retention_state"] == "current"
    assert record["route_partition_hash"] == _ROUTE_HASH
    assert record["contract_hash"] == _CONTRACT_HASH
    assert record["sensitivity_classification"] == "internal"
    assert record["sensitivity_state"] == "current"
    assert record["redaction_state"] == "redacted"
    assert "fk_live_raw" not in json.dumps(record)
    assert "local_path" not in json.dumps(record)


def test_generated_attachment_provenance_records_full_generation_closure(tmp_path):
    result = apply_gateway_event(_generated_event(), tmp_path)

    assert result.ok is True
    record = result.action["record"]
    assert record["provenance_kind"] == "generated"
    assert record["safe_output_root_proof_hash"] == _ROOT_PROOF_HASH
    assert record["producing_tool_action_hash"] == _TOOL_ACTION_HASH
    assert record["content_hash"] == _CONTENT_HASH
    assert record["declared_mime_class"] == "image"
    assert record["size_class"] == "small"
    assert record["route_partition_hash"] == _ROUTE_HASH
    assert record["contract_hash"] == _CONTRACT_HASH
    assert record["delivery_plan_hash"] == _PLAN_HASH
    assert record["sensitivity_classification"] == "internal"
    assert record["redaction_state"] == "redacted"
    assert record["retention_policy"] == "ephemeral"
    assert record["source_grant_handles"] == [_GRANT_HANDLE]


def test_attachment_render_parts_without_provenance_fail_with_b6_failure_class():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="image",
            payload_hash="sha256:" + "c" * 64,
            content_hash=_CONTENT_HASH,
            fallback_action="drop",
            source_class="generated",
        )

    assert exc_info.value.failure_class == "feishu_attachment_provenance_missing"


@pytest.mark.asyncio
async def test_generated_attachment_upload_requires_complete_matching_provenance(tmp_path):
    provenance = apply_gateway_event(_generated_event(), tmp_path).action["record"]
    adapter, image_api, file_api, message_api = _adapter(tmp_path)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(_CONTENT_BYTES)

    result = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=_metadata(
            provenance["provenance_hash"],
            feishu_attachment_producing_tool_action_hash=_TOOL_ACTION_HASH,
            feishu_attachment_safe_output_root_proof_hash=_ROOT_PROOF_HASH,
        ),
    )

    assert result.success is True
    assert len(image_api.create_calls) == 1
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    assert len(message_api.reply_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_override", "metadata_override"),
    [
        ({"route_partition_hash": "sha256:" + "d" * 64}, {}),
        ({"retention_state": "stale"}, {}),
        ({}, {"feishu_attachment_declared_mime_class": "file"}),
        ({}, {"feishu_attachment_size_class": "large"}),
        ({"sensitivity_state": "missing"}, {}),
        ({"sensitivity_state": "stale"}, {}),
        ({"safe_output_root_proof_hash": None}, {}),
        ({"safe_output_root_state": "stale"}, {}),
        ({"producing_tool_action_hash": None}, {}),
        ({}, {"feishu_attachment_producing_tool_action_hash": "sha256:" + "e" * 64}),
        ({}, {"feishu_attachment_content_hash": "sha256:" + "f" * 64}),
        ({"redaction_state": None}, {}),
        ({"redaction_state": "unknown"}, {}),
        ({"retention_policy": None}, {}),
        ({"source_grant_handles": []}, {}),
        ({"source_grant_state": "stale"}, {}),
        ({"generator_state": "unknown"}, {}),
    ],
)
async def test_attachment_provenance_negative_cases_fail_before_upload_with_sanitized_evidence(
    tmp_path,
    event_override,
    metadata_override,
):
    event = _generated_event(**event_override)
    provenance_result = apply_gateway_event(event, tmp_path)
    provenance_hash = (
        provenance_result.action["record"]["provenance_hash"]
        if provenance_result.ok
        else "sha256:" + "0" * 64
    )
    before_state = _state(tmp_path)
    adapter, image_api, file_api, message_api = _adapter(tmp_path)
    image_path = tmp_path / "blocked.png"
    image_path.write_bytes(_CONTENT_BYTES)

    metadata = _metadata(
        provenance_hash,
        feishu_attachment_producing_tool_action_hash=_TOOL_ACTION_HASH,
        feishu_attachment_safe_output_root_proof_hash=_ROOT_PROOF_HASH,
    )
    metadata.update(metadata_override)

    result = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=metadata,
    )

    assert result.success is False
    assert result.error == "feishu_attachment_provenance_mismatch"
    assert len(image_api.create_calls) == 0
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    after_state = _state(tmp_path)
    assert after_state.get("feishu_attachment_provenance", {}) == before_state.get(
        "feishu_attachment_provenance", {}
    )
    denial_events = after_state.get("feishu_attachment_denials", [])
    assert denial_events[-1]["failure_class"] == "feishu_attachment_provenance_mismatch"
    assert "local_path" not in json.dumps(denial_events[-1])


@pytest.mark.asyncio
async def test_attachment_provenance_survives_replay_without_duplicate_upload(tmp_path):
    provenance = apply_gateway_event(_generated_event(), tmp_path).action["record"]
    adapter, image_api, _file_api, message_api = _adapter(tmp_path)
    image_path = tmp_path / "replay.png"
    image_path.write_bytes(_CONTENT_BYTES)
    metadata = _metadata(
        provenance["provenance_hash"],
        feishu_attachment_producing_tool_action_hash=_TOOL_ACTION_HASH,
        feishu_attachment_safe_output_root_proof_hash=_ROOT_PROOF_HASH,
        delivery_id="delivery-provenance-replay",
    )

    first = await adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=metadata,
    )
    replay_adapter, replay_image_api, _replay_file_api, replay_message_api = _adapter(
        tmp_path
    )
    second = await replay_adapter.send_image_file(
        chat_id="oc_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=metadata,
    )

    assert first.success is True
    assert second.success is True
    assert len(image_api.create_calls) == 1
    assert len(message_api.create_calls) == 0
    assert len(message_api.reply_calls) == 1
    assert len(replay_image_api.create_calls) == 0
    assert len(replay_message_api.create_calls) == 0
    assert len(replay_message_api.reply_calls) == 0
    persisted = feishu_attachment_provenance_record(
        tmp_path, provenance["provenance_hash"]
    )
    assert persisted["provenance_hash"] == provenance["provenance_hash"]

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import PlatformConfig
from gateway.feishu_action_plan import RenderPlanPart
from gateway.feishu_contracts import FeishuContractError, feishu_hashed_ref
from gateway.gateway_event_ledger import apply_gateway_event
from gateway.platforms.feishu import FeishuAdapter


_CONTENT_BYTES = b"\x89PNG\r\n\x1a\nhermes-b7-upload-denial"
_CONTENT_HASH = "sha256:" + hashlib.sha256(_CONTENT_BYTES).hexdigest()
_ROUTE_HASH = "sha256:" + "3" * 64
_CONTRACT_HASH = "sha256:" + "4" * 64
_PLAN_HASH = "sha256:" + "6" * 64
_ROOT_PROOF_HASH = "sha256:" + "7" * 64
_TOOL_ACTION_HASH = "sha256:" + "8" * 64
_GRANT_HANDLE = "broker_grant_handle:sha256:" + "9" * 64
_SOURCE_EVENT_HASH = "sha256:" + "a" * 64
_FILE_KEY_HASH = "sha256:" + "b" * 64
_REPLY_TO = "om_b7_parent"
_REPLY_REF = feishu_hashed_ref("feishu_reply_anchor", _REPLY_TO).value_hash


class _FakeMessageApi:
    def __init__(self):
        self.create_calls = []
        self.reply_calls = []

    def create(self, request):
        self.create_calls.append(request)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="om_b7_message"),
        )

    def reply(self, request):
        self.reply_calls.append(request)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="om_b7_reply"),
        )


class _FakeUploadApi:
    def __init__(self, field_name, field_value):
        self.create_calls = []
        self._field_name = field_name
        self._field_value = field_value

    def create(self, request):
        self.create_calls.append(request)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(**{self._field_name: self._field_value}),
        )


def _adapter(tmp_path):
    adapter = FeishuAdapter(
        PlatformConfig(extra={"hermes_tools_state_dir": str(tmp_path)})
    )
    message_api = _FakeMessageApi()
    image_api = _FakeUploadApi("image_key", "img_b7_file_key")
    file_api = _FakeUploadApi("file_key", "file_b7_file_key")
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=message_api,
                image=image_api,
                file=file_api,
            )
        )
    )
    adapter._build_image_upload_body = Mock(
        wraps=adapter._build_image_upload_body
    )
    adapter._build_image_upload_request = Mock(
        wraps=adapter._build_image_upload_request
    )
    adapter._build_file_upload_body = Mock(
        wraps=adapter._build_file_upload_body
    )
    adapter._build_file_upload_request = Mock(
        wraps=adapter._build_file_upload_request
    )
    return adapter, image_api, file_api, message_api


def _state(tmp_path):
    state_path = tmp_path / "gateway_event_ledger.json"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())


def _base_metadata(provenance_hash=None, **overrides):
    values = {
        "delivery_id": "delivery-b7",
        "inbound_id": "inbound-b7",
        "session_id": "session-b7",
        "correlation_id": "corr-b7",
        "feishu_current_admission": {
            "evidence_state": "current",
            "route_partition_key": "route-partition-b7",
            "route_session_key_snapshot": "route-session-b7",
            "route_partition_hash": _ROUTE_HASH,
            "route_snapshot_hash": _ROUTE_HASH,
            "contract_hash": _CONTRACT_HASH,
            "reply_anchor_ref": _REPLY_REF,
            "actor_ref": "actor-ref-b7",
            "actor_hash": "sha256:" + "c" * 64,
            "authority_subject_ref": "authority-ref-b7",
            "transport_kind": "dm",
        },
        "feishu_attachment_declared_mime_class": "image",
        "feishu_attachment_size_class": "small",
        "feishu_attachment_content_hash": _CONTENT_HASH,
        "feishu_attachment_source_grant_handles": (_GRANT_HANDLE,),
    }
    if provenance_hash is not None:
        values["feishu_attachment_provenance_hash"] = provenance_hash
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
        "source_event_hash": _SOURCE_EVENT_HASH,
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


def _generated_metadata(provenance_hash, **overrides):
    return _base_metadata(
        provenance_hash,
        feishu_attachment_producing_tool_action_hash=_TOOL_ACTION_HASH,
        feishu_attachment_safe_output_root_proof_hash=_ROOT_PROOF_HASH,
        **overrides,
    )


def _inbound_metadata(provenance_hash, **overrides):
    return _base_metadata(
        provenance_hash,
        feishu_attachment_source_event_hash=_SOURCE_EVENT_HASH,
        feishu_attachment_file_key_hash=_FILE_KEY_HASH,
        **overrides,
    )


def _assert_no_upload_side_effects(adapter, image_api, file_api, message_api):
    assert len(image_api.create_calls) == 0
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    assert len(message_api.reply_calls) == 0
    assert adapter._build_image_upload_body.call_count == 0
    assert adapter._build_image_upload_request.call_count == 0
    assert adapter._build_file_upload_body.call_count == 0
    assert adapter._build_file_upload_request.call_count == 0


def _assert_only_sanitized_denial(tmp_path, *, forbidden_values):
    state = _state(tmp_path)
    assert state.get("deliveries", {}) == {}
    assert state.get("delivery_identity_index", {}) == {}
    assert state.get("message_id_index", {}) == {}
    denials = state.get("feishu_attachment_denials", [])
    assert len(denials) == 1
    assert denials[0]["failure_class"] == "feishu_arbitrary_local_upload_denied"
    state_json = json.dumps(state, sort_keys=True)
    assert "delivery_sent" not in state_json
    assert "feishu:chat:" not in state_json
    assert "local_path" not in state_json
    for forbidden in forbidden_values:
        assert forbidden not in state_json


def test_render_plan_local_path_part_without_provenance_is_arbitrary_upload_denial():
    with pytest.raises(FeishuContractError) as exc_info:
        RenderPlanPart(
            part_type="local_path",
            content_hash=_CONTENT_HASH,
            payload_hash="sha256:" + "d" * 64,
            fallback_action="drop",
            source_class="generated",
            metadata={"file_path": "/tmp/fake-report.pdf"},
        )

    assert exc_info.value.failure_class == "feishu_arbitrary_local_upload_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        "/tmp/fake-report.pdf",
        "reports/fake-report.pdf",
        "~/fake-report.pdf",
        "/tmp/hermes-b7-symlink-target.pdf",
        "../private/fake-report.pdf",
    ],
)
async def test_document_upload_without_provenance_denies_before_local_probe_or_sdk_upload(
    tmp_path,
    monkeypatch,
    file_path,
):
    adapter, image_api, file_api, message_api = _adapter(tmp_path)
    original_exists = __import__("os").path.exists
    original_stat = __import__("os").stat

    def guarded_exists(path):
        if path == file_path:
            pytest.fail(f"local path was probed before denial: {path!r}")
        return original_exists(path)

    def guarded_stat(path, *args, **kwargs):
        if path == file_path:
            pytest.fail(f"local stat was attempted before denial: {path!r}")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        "gateway.platforms.feishu.os.path.exists",
        guarded_exists,
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu.os.stat",
        guarded_stat,
    )

    result = await adapter.send_document(
        chat_id="oc_b7_chat",
        file_path=file_path,
        metadata=_base_metadata(),
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    _assert_no_upload_side_effects(adapter, image_api, file_api, message_api)
    _assert_only_sanitized_denial(
        tmp_path,
        forbidden_values=(file_path, "oc_b7_chat", "fake-report.pdf"),
    )


@pytest.mark.asyncio
async def test_image_upload_without_provenance_denies_before_upload_request_creation(
    tmp_path,
    monkeypatch,
):
    adapter, image_api, file_api, message_api = _adapter(tmp_path)
    image_path = "/tmp/fake-image.png"
    original_exists = __import__("os").path.exists

    def guarded_exists(path):
        if path == image_path:
            pytest.fail(f"local path was probed before denial: {path!r}")
        return original_exists(path)

    monkeypatch.setattr(
        "gateway.platforms.feishu.os.path.exists",
        guarded_exists,
    )

    result = await adapter.send_image_file(
        chat_id="oc_b7_chat",
        image_path=image_path,
        reply_to=_REPLY_TO,
        metadata=_base_metadata(),
    )

    assert result.success is False
    assert result.error == "feishu_arbitrary_local_upload_denied"
    _assert_no_upload_side_effects(adapter, image_api, file_api, message_api)
    _assert_only_sanitized_denial(
        tmp_path,
        forbidden_values=(image_path, "oc_b7_chat", "fake-image.png"),
    )


@pytest.mark.asyncio
async def test_generated_attachment_with_matching_provenance_and_content_hash_uploads(
    tmp_path,
):
    provenance = apply_gateway_event(_generated_event(), tmp_path).action["record"]
    image_path = tmp_path / "generated-b7.png"
    image_path.write_bytes(_CONTENT_BYTES)
    adapter, image_api, file_api, message_api = _adapter(tmp_path)

    result = await adapter.send_image_file(
        chat_id="oc_b7_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=_generated_metadata(provenance["provenance_hash"]),
    )

    assert result.success is True
    assert len(image_api.create_calls) == 1
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    assert len(message_api.reply_calls) == 1


@pytest.mark.asyncio
async def test_generated_attachment_with_content_hash_mismatch_denies_without_upload(
    tmp_path,
):
    provenance = apply_gateway_event(_generated_event(), tmp_path).action["record"]
    image_path = tmp_path / "generated-b7-mismatch.png"
    image_path.write_bytes(b"different generated bytes")
    adapter, image_api, file_api, message_api = _adapter(tmp_path)

    result = await adapter.send_image_file(
        chat_id="oc_b7_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=_generated_metadata(provenance["provenance_hash"]),
    )

    assert result.success is False
    assert result.error == "feishu_attachment_provenance_mismatch"
    _assert_no_upload_side_effects(adapter, image_api, file_api, message_api)


@pytest.mark.asyncio
async def test_inbound_attachment_echo_uploads_only_with_current_event_provenance(
    tmp_path,
):
    provenance = apply_gateway_event(_inbound_event(), tmp_path).action["record"]
    image_path = tmp_path / "inbound-echo-b7.png"
    image_path.write_bytes(_CONTENT_BYTES)
    adapter, image_api, file_api, message_api = _adapter(tmp_path)

    result = await adapter.send_image_file(
        chat_id="oc_b7_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=_inbound_metadata(provenance["provenance_hash"]),
    )

    assert result.success is True
    assert len(image_api.create_calls) == 1
    assert len(file_api.create_calls) == 0
    assert len(message_api.create_calls) == 0
    assert len(message_api.reply_calls) == 1


@pytest.mark.asyncio
async def test_inbound_attachment_echo_denies_when_current_event_proof_is_absent(
    tmp_path,
):
    provenance = apply_gateway_event(_inbound_event(), tmp_path).action["record"]
    image_path = tmp_path / "inbound-echo-b7-missing-proof.png"
    image_path.write_bytes(_CONTENT_BYTES)
    adapter, image_api, file_api, message_api = _adapter(tmp_path)

    result = await adapter.send_image_file(
        chat_id="oc_b7_chat",
        image_path=str(image_path),
        reply_to=_REPLY_TO,
        metadata=_base_metadata(provenance["provenance_hash"]),
    )

    assert result.success is False
    assert result.error == "feishu_attachment_provenance_mismatch"
    _assert_no_upload_side_effects(adapter, image_api, file_api, message_api)

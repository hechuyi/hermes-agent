import asyncio
import json
import subprocess
import threading

import pytest

from gateway.hermes_tools_gateway_event import (
    apply_gateway_event,
    apply_gateway_event_async,
    preflight_gateway_event,
)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["hermes-tools"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _rust_delivery_record(status="pending", **overrides):
    record = {
        "delivery_id": "delivery-1",
        "inbound_id": "om_inbound_1",
        "target": "feishu:chat:oc_1",
        "session_id": "session-a",
        "correlation_id": "corr-1",
        "status": status,
        "created_at": 100,
        "updated_at": 100,
        "feishu_message_id": None,
        "failure_class": None,
        "ack_event_id": None,
    }
    record.update(overrides)
    return record


def _rust_inbound_admission_action(*, duplicate=False):
    return {
        "type": "inbound_admission",
        "decision": "continue",
        "duplicate": duplicate,
        "record": {
            "inbound_id_hash": "fnv1a64:d5793e6083fe7f82",
            "message_id_hash": "fnv1a64:234d09b47b7872c9",
            "message_type": "text",
            "first_seen_at": 100,
        },
    }


def test_apply_gateway_event_success_invokes_hermes_tools_with_json_stdin(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {
                        "type": "delivery_record",
                        "record": _rust_delivery_record(),
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    event = {"type": "delivery_pending", "delivery_id": "delivery-1"}
    result = apply_gateway_event(event, tmp_path)

    assert result.ok is True
    assert result.event_type == "delivery_pending"
    assert result.action == {
        "type": "delivery_record",
        "record": _rust_delivery_record(),
    }
    assert result.failure_class is None
    assert result.reason is None
    assert result.diagnostics == ""

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == [
        "hermes-tools",
        "gateway-event",
        "apply",
        "--state-dir",
        str(tmp_path),
    ]
    assert json.loads(kwargs["input"]) == event
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] > 0
    assert kwargs["check"] is False


def test_apply_gateway_event_accepts_inbound_admission_action(monkeypatch, tmp_path):
    action = _rust_inbound_admission_action(duplicate=False)

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "feishu_inbound",
                    "action": action,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event(
        {
            "type": "feishu_inbound",
            "inbound_id": "ev_1",
            "message_id": "om_1",
            "message_type": "text",
            "timestamp": 100,
        },
        tmp_path,
    )

    assert result.ok is True
    assert result.event_type == "feishu_inbound"
    assert result.action == action


@pytest.mark.parametrize(
    ("event_type", "record"),
    [
        ("delivery_pending", _rust_delivery_record()),
        (
            "delivery_sent",
            _rust_delivery_record(
                "sent",
                updated_at=105,
                feishu_message_id="om_123",
            ),
        ),
        (
            "delivery_failed",
            _rust_delivery_record(
                "failed",
                updated_at=106,
                failure_class="send_failed",
            ),
        ),
        (
            "feishu_ack",
            _rust_delivery_record(
                "acked",
                updated_at=110,
                feishu_message_id="om_123",
                ack_event_id="read-event-1",
            ),
        ),
        (
            "unknown_delivery_state",
            _rust_delivery_record(
                "unknown",
                updated_at=111,
                feishu_message_id="om_123",
                failure_class="delivery-sent-apply-failed",
            ),
        ),
    ],
)
def test_apply_gateway_event_accepts_rust_delivery_record_shapes(
    monkeypatch, tmp_path, event_type, record
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": event_type,
                    "action": {"type": "delivery_record", "record": record},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": event_type}, tmp_path)

    assert result.ok is True
    assert result.event_type == event_type
    assert result.action == {"type": "delivery_record", "record": record}


def test_apply_gateway_event_accepts_rust_stale_pending_alert_shape(
    monkeypatch, tmp_path
):
    stale_record = _rust_delivery_record(
        "unknown",
        updated_at=111,
        failure_class="sdk-exception-after-admission",
    )
    action = {
        "type": "stale_pending_alert",
        "alert_required": True,
        "resend_permitted": False,
        "count": 1,
        "records": [stale_record],
    }

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "stale_pending_scan",
                    "action": action,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "stale_pending_scan"}, tmp_path)

    assert result.ok is True
    assert result.event_type == "stale_pending_scan"
    assert result.action == action


def test_apply_gateway_event_rejects_unknown_delivery_without_failure_class(
    monkeypatch, tmp_path
):
    record = _rust_delivery_record(
        "unknown",
        updated_at=111,
        failure_class=None,
    )

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "unknown_delivery_state",
                    "action": {"type": "delivery_record", "record": record},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "unknown_delivery_state"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


def test_apply_gateway_event_rejects_stale_unknown_record_without_failure_class(
    monkeypatch, tmp_path
):
    action = {
        "type": "stale_pending_alert",
        "alert_required": True,
        "resend_permitted": False,
        "count": 1,
        "records": [
            _rust_delivery_record(
                "unknown",
                updated_at=111,
                failure_class=None,
            )
        ],
    }

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "stale_pending_scan",
                    "action": action,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "stale_pending_scan"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


def test_apply_gateway_event_rejects_stale_pending_alert_without_blocker_contract(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "stale_pending_scan",
                    "action": {
                        "type": "stale_pending_alert",
                        "alert_required": True,
                        "resend_permitted": True,
                        "count": 0,
                        "records": [],
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "stale_pending_scan"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


def test_apply_gateway_event_rejects_inbound_admission_extra_fields(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "feishu_inbound",
                    "action": {
                        "type": "inbound_admission",
                        "decision": "continue",
                        "raw_message_id": "om_sensitive",
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "feishu_inbound"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "om_sensitive" not in result.diagnostics


def test_apply_gateway_event_rejects_inbound_admission_non_continue(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "feishu_inbound",
                    "action": {"type": "inbound_admission", "decision": "raw user text"},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "feishu_inbound"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "raw user text" not in result.diagnostics


def test_apply_gateway_event_rejects_raw_inbound_admission_ids(monkeypatch, tmp_path):
    action = _rust_inbound_admission_action()
    action["record"] = {
        **action["record"],
        "inbound_id_hash": "ev_1",
        "message_id_hash": "om_1",
    }

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "feishu_inbound",
                    "action": action,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "feishu_inbound"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "ev_1" not in result.diagnostics
    assert "om_1" not in result.diagnostics


@pytest.mark.asyncio
async def test_apply_gateway_event_async_runs_sync_apply_in_worker(monkeypatch, tmp_path):
    calls = []

    def fake_apply(event, state_dir, **kwargs):
        calls.append((event, state_dir, kwargs))
        return "result"

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        fake_apply,
    )

    result = await apply_gateway_event_async(
        {"type": "feishu_inbound"},
        tmp_path,
        timeout_seconds=3,
        binary="hermes-tools-test",
    )

    assert result == "result"
    assert calls == [
        (
            {"type": "feishu_inbound"},
            tmp_path,
            {"timeout_seconds": 3, "binary": "hermes-tools-test"},
        )
    ]


@pytest.mark.asyncio
async def test_apply_gateway_event_async_fails_closed_when_worker_slots_are_saturated(
    monkeypatch, tmp_path
):
    started = 0
    started_lock = threading.Lock()
    release = threading.Event()

    def fake_apply(event, state_dir, **kwargs):
        nonlocal started
        with started_lock:
            started += 1
        release.wait(timeout=5)
        return "result"

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.apply_gateway_event",
        fake_apply,
    )

    tasks = [
        asyncio.create_task(apply_gateway_event_async({"type": f"event_{index}"}, tmp_path))
        for index in range(4)
    ]
    for _ in range(100):
        with started_lock:
            if started == 4:
                break
        await asyncio.sleep(0.01)

    saturated = await apply_gateway_event_async({"type": "event_5"}, tmp_path)
    release.set()
    completed = await asyncio.gather(*tasks)

    assert completed == ["result", "result", "result", "result"]
    assert saturated.ok is False
    assert saturated.failure_class == "hermes_tools_async_saturated"
    assert saturated.event_type == "event_5"


def test_preflight_gateway_event_success_invokes_preflight(monkeypatch, tmp_path):
    checks = [
        {
            "name": "state_dir_writable",
            "ok": True,
            "detail": "state directory accepts create/write/remove",
        },
        {
            "name": "feishu_inbound",
            "ok": True,
            "detail": "inbound admission persisted",
        },
        {
            "name": "delivery_lifecycle",
            "ok": True,
            "detail": "delivery_pending and delivery_sent persisted",
        },
        {
            "name": "feishu_ack",
            "ok": True,
            "detail": "read-event ack updated the sent delivery",
        },
        {
            "name": "stale_pending_scan",
            "ok": True,
            "detail": "stale pending count=1",
        },
        {
            "name": "session_guard",
            "ok": True,
            "detail": "compression mismatch is rejected with implicit_session_switch",
        },
        {
            "name": "status_card_request_descriptor",
            "ok": True,
            "detail": "task_status produced a Feishu send request descriptor",
        },
    ]
    feishu_request = {
        "body": {
            "content": (
                '{"config":{"update_multi":true,"wide_screen_mode":true},'
                '"elements":[{"tag":"div","text":{"content":"**State:** running\\n'
                'preflight started","tag":"lark_md"}},{"tag":"hr"},{"elements":'
                '[{"content":"task running: preflight started","tag":"plain_text"}],'
                '"tag":"note"}],"header":{"template":"blue","title":{"content":'
                '"Hermes task running","tag":"plain_text"}}}'
            ),
            "msg_type": "interactive",
            "receive_id": "oc_preflight",
            "uuid": "preflight-task-create",
        },
        "method": "POST",
        "operation": "send_interactive_message",
        "params": {"receive_id_type": "chat_id"},
        "path": "/open-apis/im/v1/messages",
    }

    def fake_run(*args, **kwargs):
        assert args[0] == [
            "hermes-tools",
            "gateway-event",
            "preflight",
            "--state-dir",
            str(tmp_path),
        ]
        assert "input" not in kwargs
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "state_dir_writable": True,
                    "checks": checks,
                    "feishu_request": feishu_request,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is True
    assert result.event_type == "preflight"
    assert result.action["type"] == "preflight_report"
    assert result.action["state_dir_writable"] is True
    assert result.action["checks"] == [
        {"name": check["name"], "ok": check["ok"]} for check in checks
    ]
    assert result.action["feishu_request"] == feishu_request
    assert result.failure_class is None
    assert result.diagnostics == ""


def test_apply_gateway_event_missing_binary_fails_closed(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no hermes-tools in PATH")

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_missing"
    assert result.event_type is None
    assert result.action is None
    assert "no hermes-tools in PATH" not in result.diagnostics


def test_apply_gateway_event_timeout_fails_closed(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path, timeout_seconds=2)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_timeout"
    assert result.reason == "subprocess timeout"
    assert "2" in result.diagnostics


def test_apply_gateway_event_nonzero_without_envelope_fails_closed(
    monkeypatch, tmp_path
):
    raw_stdout = "user text: reset my password; open_id=ou_sensitive"
    raw_stderr = "panic for https://tenant.example/callback token=secret-token"

    def fake_run(*args, **kwargs):
        return _completed(stdout=raw_stdout, stderr=raw_stderr, returncode=7)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_nonzero_exit"
    assert result.reason == "hermes-tools exited nonzero"
    assert "returncode=7" in result.diagnostics
    assert "stdout_bytes=" in result.diagnostics
    assert "stderr_bytes=" in result.diagnostics
    assert "stdout_json=invalid" in result.diagnostics
    assert "stderr_json=invalid" in result.diagnostics
    assert raw_stdout not in result.diagnostics
    assert raw_stderr not in result.diagnostics
    assert "ou_sensitive" not in result.diagnostics
    assert "tenant.example" not in result.diagnostics
    assert "secret-token" not in result.diagnostics


def test_apply_gateway_event_invalid_json_fails_closed(monkeypatch, tmp_path):
    raw_stdout = "not json: arbitrary user message om_sensitive_msg"

    def fake_run(*args, **kwargs):
        return _completed(stdout=raw_stdout, returncode=0)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_json"
    assert result.reason == "invalid hermes-tools JSON envelope"
    assert "stdout_json=invalid" in result.diagnostics
    assert raw_stdout not in result.diagnostics
    assert "om_sensitive_msg" not in result.diagnostics


def test_apply_gateway_event_ok_false_envelope_uses_stable_reason(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stderr=json.dumps(
                {
                    "ok": False,
                    "event_type": "delivery_pending",
                    "error": {
                        "reason": "state_io_error",
                        "message": (
                            "state_io_error: permission denied for "
                            "ou_sensitive https://tenant.example"
                        ),
                    },
                }
            ),
            returncode=1,
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.event_type == "delivery_pending"
    assert result.failure_class == "state_io_error"
    assert result.reason == "state_io_error"
    assert result.action is None
    assert "permission denied" not in result.diagnostics
    assert "ou_sensitive" not in result.diagnostics
    assert "tenant.example" not in result.diagnostics


def test_preflight_ok_false_envelope_uses_failure_class(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": False,
                    "state_dir_writable": False,
                    "checks": [{"name": "state_dir_writable", "ok": False}],
                    "failure_class": "state_dir_unusable",
                    "failure_detail": "state_dir_writable failed for ou_sensitive",
                    "feishu_request": None,
                }
            ),
            returncode=1,
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is False
    assert result.event_type == "preflight"
    assert result.failure_class == "state_dir_unusable"
    assert result.reason == "state_dir_unusable"
    assert "state_dir_writable failed" not in result.diagnostics
    assert "ou_sensitive" not in result.diagnostics


def test_apply_gateway_event_unsupported_action_fails_closed(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {"type": "raw_http_request", "url": "https://example.test"},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "unsupported_action"
    assert result.reason == "unsupported action"
    assert result.action is None
    assert "type_present=true" in result.diagnostics
    assert "raw_http_request" not in result.diagnostics
    assert "https://example.test" not in result.diagnostics


def test_apply_gateway_event_unsupported_token_like_action_type_is_not_diagnostic(
    monkeypatch, tmp_path
):
    token_like_type = "sk-test-abcdefghijklmnopqrstuvwxyz"

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {"type": token_like_type},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "unsupported_action"
    assert token_like_type not in result.diagnostics
    assert "type_present=true" in result.diagnostics


def test_apply_gateway_event_unsupported_identifier_action_type_is_not_diagnostic(
    monkeypatch, tmp_path
):
    identifier_type = "secret_token_value"

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {"type": identifier_type},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "unsupported_action"
    assert identifier_type not in result.diagnostics
    assert "type_present=true" in result.diagnostics


def test_diagnostics_do_not_include_sensitive_output_or_state_dir(monkeypatch, tmp_path):
    raw_secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    raw_output = (
        f"state_dir={tmp_path} OPENAI_API_KEY={raw_secret} "
        '{"access_token": "super-secret-token-value"}'
    )

    def fake_run(*args, **kwargs):
        return _completed(stdout="{not json", stderr=raw_output, returncode=0)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    combined = " ".join(
        part for part in [result.reason, result.diagnostics] if part
    )
    assert raw_secret not in combined
    assert "super-secret-token-value" not in combined
    assert str(tmp_path) not in combined
    assert "stdout_bytes=" in combined
    assert "stderr_bytes=" in combined
    assert "access_token" not in combined


def test_apply_gateway_event_rejects_extra_top_level_fields(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "debug": {"text": "arbitrary user text ou_sensitive"},
                    "action": {
                        "type": "delivery_record",
                        "record": {"delivery_id": "delivery-1"},
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "arbitrary user text" not in result.diagnostics
    assert "ou_sensitive" not in result.diagnostics


def test_apply_gateway_event_rejects_unknown_action_fields(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {
                        "type": "delivery_record",
                        "record": {"delivery_id": "delivery-1"},
                        "feishu_message_id": "om_sensitive",
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "om_sensitive" not in result.diagnostics


def test_apply_gateway_event_rejects_sensitive_nested_action_values(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "delivery_pending",
                    "action": {
                        "type": "delivery_record",
                        "record": {
                            "delivery_id": "delivery-1",
                            "metadata": {"token": "secret-token-value"},
                        },
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "secret-token-value" not in result.diagnostics


def test_apply_gateway_event_rejects_fields_under_supported_action_type(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "status_update",
                    "action": {
                        "type": "status_card",
                        "title": "raw platform text",
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "status_update"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "raw platform text" not in result.diagnostics


def test_preflight_gateway_event_accepts_check_detail_without_leaking_it(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "state_dir_writable": True,
                    "checks": [
                        {
                            "name": "delivery_lifecycle",
                            "ok": True,
                            "detail": "raw platform text om_sensitive",
                        }
                    ],
                    "feishu_request": None,
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is True
    assert result.action["checks"] == [{"name": "delivery_lifecycle", "ok": True}]
    assert "raw platform text" not in json.dumps(result.action)
    assert "om_sensitive" not in json.dumps(result.action)
    assert result.diagnostics == ""


def test_preflight_gateway_event_rejects_malformed_feishu_request(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "state_dir_writable": True,
                    "checks": [{"name": "delivery_lifecycle", "ok": True}],
                    "feishu_request": {
                        "operation": "feishu.card.create",
                        "body": {
                            "msg_type": "interactive",
                            "open_id": "ou_sensitive",
                        },
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "ou_sensitive" not in result.diagnostics


def test_preflight_gateway_event_rejects_generic_feishu_request_path(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "state_dir_writable": True,
                    "checks": [{"name": "delivery_lifecycle", "ok": True}],
                    "feishu_request": {
                        "operation": "send_interactive_message",
                        "method": "POST",
                        "path": "/open-apis/contact/v3/users",
                        "params": {"receive_id_type": "chat_id"},
                        "body": {
                            "receive_id": "oc_preflight",
                            "msg_type": "interactive",
                            "content": '{"config":{"wide_screen_mode":true}}',
                            "uuid": "preflight-task-create",
                        },
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = preflight_gateway_event(tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


def _status_card_feishu_create_request():
    return {
        "operation": "send_interactive_message",
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "params": {"receive_id_type": "chat_id"},
        "body": {
            "receive_id": "oc_status",
            "msg_type": "interactive",
            "content": '{"config":{"wide_screen_mode":true}}',
            "uuid": "task-status-create",
        },
    }


def _status_card_feishu_patch_request():
    return {
        "operation": "patch_interactive_message",
        "method": "PATCH",
        "path": "/open-apis/im/v1/messages/om_status",
        "params": {},
        "body": {"content": '{"config":{"wide_screen_mode":true}}'},
    }


def _status_card_action(action):
    return {
        "ok": True,
        "event_type": "task_status",
        "action": {
            "type": "status_card",
            "card_action": action,
        },
    }


def _status_card_create_action(**overrides):
    action = {
        "type": "create",
        "card_id": "task-1",
        "state": "running",
        "text": "preflight started",
        "requires_final_reply": True,
        "fallback_text": "task running: preflight started",
        "feishu_card": {"config": {"wide_screen_mode": True}},
        "feishu_request": _status_card_feishu_create_request(),
    }
    action.update(overrides)
    return action


def _status_card_update_action(**overrides):
    action = {
        "type": "update",
        "card_id": "task-1",
        "state": "succeeded",
        "text": "preflight complete",
        "requires_final_reply": False,
        "fallback_text": "task succeeded: preflight complete",
        "feishu_card": {"config": {"wide_screen_mode": True}},
        "feishu_request": _status_card_feishu_patch_request(),
    }
    action.update(overrides)
    return action


def _status_card_suppressed_action(**overrides):
    action = {
        "type": "suppressed",
        "reason": "status_card_throttled",
        "fallback_text": "task running: preflight started",
        "feishu_card": {"config": {"wide_screen_mode": True}},
    }
    action.update(overrides)
    return action


def test_apply_gateway_event_rejects_status_card_missing_card_action(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "task_status",
                    "action": {"type": "status_card"},
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


@pytest.mark.parametrize("action_factory", [_status_card_create_action, _status_card_update_action])
@pytest.mark.parametrize(
    "missing_field",
    ["card_id", "state", "text", "requires_final_reply", "fallback_text", "feishu_card"],
)
def test_apply_gateway_event_rejects_status_card_create_update_missing_rust_required_fields(
    monkeypatch, tmp_path, action_factory, missing_field
):
    action = action_factory()
    action.pop(missing_field)

    def fake_run(*args, **kwargs):
        return _completed(stdout=json.dumps(_status_card_action(action)))

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


@pytest.mark.parametrize(
    "action_factory",
    [_status_card_create_action, _status_card_update_action, _status_card_suppressed_action],
)
def test_apply_gateway_event_accepts_complete_rust_status_card_actions(
    monkeypatch, tmp_path, action_factory
):
    action = action_factory()

    def fake_run(*args, **kwargs):
        return _completed(stdout=json.dumps(_status_card_action(action)))

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is True
    assert result.action == {"type": "status_card", "card_action": action}


def test_apply_gateway_event_rejects_status_card_suppressed_feishu_request(
    monkeypatch, tmp_path
):
    action = _status_card_suppressed_action(
        feishu_request=_status_card_feishu_patch_request()
    )

    def fake_run(*args, **kwargs):
        return _completed(stdout=json.dumps(_status_card_action(action)))

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None


def test_apply_gateway_event_exposes_status_card_create_feishu_request(
    monkeypatch, tmp_path
):
    feishu_request = _status_card_feishu_create_request()

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                _status_card_action(_status_card_create_action(feishu_request=feishu_request))
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is True
    assert result.action == {
        "type": "status_card",
        "card_action": {
            "type": "create",
            "card_id": "task-1",
            "state": "running",
            "text": "preflight started",
            "requires_final_reply": True,
            "fallback_text": "task running: preflight started",
            "feishu_card": {"config": {"wide_screen_mode": True}},
            "feishu_request": feishu_request,
        },
    }


def test_apply_gateway_event_exposes_status_card_patch_feishu_request(
    monkeypatch, tmp_path
):
    feishu_request = _status_card_feishu_patch_request()

    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                _status_card_action(_status_card_update_action(feishu_request=feishu_request))
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is True
    assert result.action["card_action"]["feishu_request"] == feishu_request


def test_apply_gateway_event_rejects_status_card_descriptor_extra_fields(
    monkeypatch, tmp_path
):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "event_type": "task_status",
                    "action": {
                        "type": "status_card",
                        "card_action": {
                            "type": "create",
                            "card_id": "task-1",
                            "state": "running",
                            "text": "preflight started",
                            "requires_final_reply": True,
                            "fallback_text": "task running: preflight started",
                            "feishu_card": {"config": {"wide_screen_mode": True}},
                            "feishu_request": {
                                "operation": "send_interactive_message",
                                "method": "POST",
                                "path": "/open-apis/im/v1/messages",
                                "params": {"receive_id_type": "chat_id"},
                                "body": {
                                    "receive_id": "oc_status",
                                    "msg_type": "interactive",
                                    "content": '{"config":{"wide_screen_mode":true}}',
                                    "uuid": "task-status-create",
                                    "headers": {"authorization": "Bearer raw-secret"},
                                },
                            },
                        },
                    },
                }
            )
        )

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "task_status"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_envelope"
    assert result.action is None
    assert "raw-secret" not in result.diagnostics

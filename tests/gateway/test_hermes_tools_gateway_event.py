import json
import subprocess

from gateway.hermes_tools_gateway_event import (
    apply_gateway_event,
    preflight_gateway_event,
)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["hermes-tools"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


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
                        "record": {"delivery_id": "delivery-1"},
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
        "record": {"delivery_id": "delivery-1"},
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


def test_preflight_gateway_event_success_invokes_preflight(monkeypatch, tmp_path):
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
                    "checks": [{"name": "delivery_lifecycle", "ok": True}],
                    "feishu_request": {
                        "operation": "feishu.card.create",
                        "body": {"msg_type": "interactive"},
                    },
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
    def fake_run(*args, **kwargs):
        return _completed(stdout="not json", stderr="panic", returncode=7)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_nonzero_exit"
    assert result.reason == "hermes-tools exited nonzero"
    assert "returncode=7" in result.diagnostics
    assert "panic" in result.diagnostics


def test_apply_gateway_event_invalid_json_fails_closed(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(stdout="{not json", returncode=0)

    monkeypatch.setattr(
        "gateway.hermes_tools_gateway_event.subprocess.run",
        fake_run,
    )

    result = apply_gateway_event({"type": "delivery_pending"}, tmp_path)

    assert result.ok is False
    assert result.failure_class == "hermes_tools_invalid_json"
    assert result.reason == "invalid hermes-tools JSON envelope"


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
                        "message": "state_io_error: permission denied",
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
    assert "permission denied" in result.diagnostics


def test_preflight_ok_false_envelope_uses_failure_class(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": False,
                    "state_dir_writable": False,
                    "checks": [{"name": "state_dir_writable", "ok": False}],
                    "failure_class": "state_dir_unusable",
                    "failure_detail": "state_dir_writable failed",
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
    assert "state_dir_writable failed" in result.diagnostics


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
    assert "raw_http_request" in result.diagnostics
    assert "https://example.test" not in result.diagnostics


def test_diagnostics_redact_sensitive_output_and_state_dir(monkeypatch, tmp_path):
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
    assert "<state-dir>" in combined
    assert "***" in combined

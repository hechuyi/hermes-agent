import dataclasses
import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.gateway_event_contract import GatewayEventResult
from gateway.feishu_legacy_guard import (
    FeishuBrokerContext,
    current_feishu_broker_context,
    feishu_broker_context,
    require_feishu_broker_context,
)


CONTRACT_HASH = "sha256:" + ("a" * 64)
GRANT_HANDLE = "broker_grant_handle:sha256:" + ("b" * 64)
ACTION_ID = "broker_action:sha256:" + ("c" * 64)
ROUTE_PARTITION_KEY = "route_snapshot:sha256:" + ("d" * 64)
SAFE_EVENT_HASH = "sha256:" + ("e" * 64)


class RecordingClient:
    def __init__(self):
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return SimpleNamespace(code=0, msg="ok", raw=SimpleNamespace(content='{"data":{}}'), data={})


def _tool_json(result: str) -> dict:
    return json.loads(result)


def _install_fake_lark(monkeypatch):
    class _AccessTokenType:
        TENANT = "tenant"

    class _HttpMethod:
        GET = "GET"
        POST = "POST"

    class _Builder:
        def __init__(self):
            self.payload = {}

        def http_method(self, value):
            self.payload["method"] = value
            return self

        def uri(self, value):
            self.payload["uri"] = value
            return self

        def token_types(self, value):
            self.payload["token_types"] = value
            return self

        def paths(self, value):
            self.payload["paths"] = value
            return self

        def queries(self, value):
            self.payload["queries"] = value
            return self

        def body(self, value):
            self.payload["body"] = value
            return self

        def build(self):
            return dict(self.payload)

    class _BaseRequest:
        @staticmethod
        def builder():
            return _Builder()

    monkeypatch.setitem(sys.modules, "lark_oapi", types.SimpleNamespace(AccessTokenType=_AccessTokenType))
    monkeypatch.setitem(sys.modules, "lark_oapi.core", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "lark_oapi.core.enum", types.SimpleNamespace(HttpMethod=_HttpMethod))
    monkeypatch.setitem(
        sys.modules,
        "lark_oapi.core.model.base_request",
        types.SimpleNamespace(BaseRequest=_BaseRequest),
    )


@pytest.fixture
def legacy_audit(monkeypatch, tmp_path):
    events = []

    def _apply(event, state_dir, **kwargs):
        events.append((dict(event), state_dir, kwargs))
        return GatewayEventResult(ok=True, event_type=event["type"], action={"type": "feishu_audit_event_record", "record": dict(event)})

    monkeypatch.setenv("HERMES_GATEWAY_EVENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_FEISHU_LEGACY_AUDIT_EVENT_HASH", SAFE_EVENT_HASH)
    monkeypatch.setattr("gateway.gateway_event_ledger.apply_gateway_event", _apply)
    return events


@pytest.fixture(autouse=True)
def clear_tool_caches():
    import model_tools
    from tools.registry import invalidate_check_fn_cache

    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()
    yield
    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()


def test_no_context_denies_with_stable_reason():
    allowed, reason = require_feishu_broker_context(
        "tool",
        "feishu_doc_read",
        args={"document_id_hash": "sha256:" + ("b" * 64)},
    )

    assert allowed is False
    assert reason == "feishu_legacy_tool_requires_broker"
    assert current_feishu_broker_context() is None


def test_json_args_broker_grant_cannot_create_context():
    allowed, reason = require_feishu_broker_context(
        "tool",
        "feishu_doc_read",
        args={
            "_feishu_broker_grant": {
                "grant_handle": "grant-safe",
                "action_id": "action-safe",
                "contract_hash": CONTRACT_HASH,
                "route_partition_key": "route-safe",
            }
        },
    )

    assert allowed is False
    assert reason == "feishu_legacy_tool_requires_broker"
    assert current_feishu_broker_context() is None


def test_contextvar_created_broker_context_allows_legacy_surface():
    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ) as context:
        allowed, reason = require_feishu_broker_context(
            "tool",
            "feishu_doc_read",
            args={"_feishu_broker_grant": "model-supplied-noop"},
        )

        assert allowed is True
        assert reason == ""
        assert current_feishu_broker_context() == context
        assert context == FeishuBrokerContext(
            grant_handle=GRANT_HANDLE,
            action_id=ACTION_ID,
            contract_hash=CONTRACT_HASH,
            route_partition_key=ROUTE_PARTITION_KEY,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.grant_handle = "changed"


def test_context_exit_restores_deny_state():
    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ):
        assert require_feishu_broker_context("tool", "feishu_doc_read") == (True, "")

    assert current_feishu_broker_context() is None
    assert require_feishu_broker_context("tool", "feishu_doc_read") == (
        False,
        "feishu_legacy_tool_requires_broker",
    )


def test_nested_context_restores_outer_then_empty_state():
    outer_grant = "broker_grant_handle:sha256:" + ("1" * 64)
    outer_action = "broker_action:sha256:" + ("2" * 64)
    outer_route = "route_snapshot:sha256:" + ("3" * 64)
    inner_grant = "broker_grant_handle:sha256:" + ("4" * 64)
    inner_action = "broker_action:sha256:" + ("5" * 64)
    inner_route = "route_snapshot:sha256:" + ("6" * 64)

    with feishu_broker_context(
        outer_grant,
        action_id=outer_action,
        contract_hash=CONTRACT_HASH,
        route_partition_key=outer_route,
    ) as outer_context:
        assert current_feishu_broker_context() == outer_context

        with feishu_broker_context(
            inner_grant,
            action_id=inner_action,
            contract_hash=CONTRACT_HASH,
            route_partition_key=inner_route,
        ) as inner_context:
            assert current_feishu_broker_context() == inner_context

        assert current_feishu_broker_context() == outer_context

    assert current_feishu_broker_context() is None


def test_context_restore_after_exception():
    with pytest.raises(RuntimeError, match="broker failure"):
        with feishu_broker_context(
            GRANT_HANDLE,
            action_id=ACTION_ID,
            contract_hash=CONTRACT_HASH,
            route_partition_key=ROUTE_PARTITION_KEY,
        ):
            assert require_feishu_broker_context("tool", "feishu_doc_read") == (
                True,
                "",
            )
            raise RuntimeError("broker failure")

    assert current_feishu_broker_context() is None
    assert require_feishu_broker_context("tool", "feishu_doc_read") == (
        False,
        "feishu_legacy_tool_requires_broker",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_handle", "grant-safe"),
        ("grant_handle", "raw-token-secret"),
        ("grant_handle", "open_id-ou_123"),
        ("grant_handle", "/tmp/raw/path"),
        ("action_id", "action-safe"),
        ("action_id", "user_id-123"),
        ("contract_hash", "not-a-contract-hash"),
        ("route_partition_key", "route-safe"),
        ("route_partition_key", "file_path-home"),
        ("route_partition_key", "route with spaces"),
    ],
)
def test_context_creation_rejects_unsafe_values(field, value):
    values = {
        "grant_handle": GRANT_HANDLE,
        "action_id": ACTION_ID,
        "contract_hash": CONTRACT_HASH,
        "route_partition_key": ROUTE_PARTITION_KEY,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        with feishu_broker_context(
            values["grant_handle"],
            action_id=values["action_id"],
            contract_hash=values["contract_hash"],
            route_partition_key=values["route_partition_key"],
        ):
            pass

    assert current_feishu_broker_context() is None


def test_doc_check_returns_false_without_broker_context(monkeypatch):
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())

    assert doc_tool._check_feishu() is False


def test_doc_handler_denies_before_injected_client_request_without_context(legacy_audit):
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    client = RecordingClient()
    doc_tool.set_client(client)
    try:
        result = _tool_json(doc_tool._handle_feishu_doc_read({"doc_token": "doccnUnsafeToken"}))
    finally:
        doc_tool.set_client(None)

    assert client.requests == []
    assert result["success"] is False
    assert result["failure_class"] == "feishu_legacy_tool_requires_broker"
    assert result["audit_event"] == "feishu_legacy_tool_denied"
    event = legacy_audit[-1][0]
    assert event["type"] == "feishu_legacy_tool_denied"
    assert event["tool"] == "feishu_doc_read"
    assert event["surface"] == "doc"
    assert event["failure_class"] == "feishu_legacy_tool_requires_broker"
    assert "doccnUnsafeToken" not in json.dumps(event)


def test_denial_audit_correlation_id_ignores_raw_external_material(
    monkeypatch, legacy_audit
):
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    raw_correlation = "ou_raw_token_secret_material"
    monkeypatch.setenv("HERMES_FEISHU_LEGACY_AUDIT_CORRELATION_ID", raw_correlation)

    result = _tool_json(doc_tool._handle_feishu_doc_read({"doc_token": "doccnUnsafeToken"}))

    assert result["failure_class"] == "feishu_legacy_tool_requires_broker"
    event = legacy_audit[-1][0]
    assert event["correlation_id"] != raw_correlation
    assert raw_correlation not in json.dumps(event)
    assert event["correlation_id"] == "feishu-legacy-denial"


def test_doc_handler_model_supplied_grant_does_not_bypass(legacy_audit):
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    client = RecordingClient()
    doc_tool.set_client(client)
    try:
        result = _tool_json(
            doc_tool._handle_feishu_doc_read(
                {
                    "doc_token": "doccnUnsafeToken",
                    "_feishu_broker_grant": {
                        "grant_handle": GRANT_HANDLE,
                        "action_id": ACTION_ID,
                        "contract_hash": CONTRACT_HASH,
                    },
                }
            )
        )
    finally:
        doc_tool.set_client(None)

    assert client.requests == []
    assert result["failure_class"] == "feishu_legacy_tool_requires_broker"


def test_doc_handler_with_broker_context_reaches_existing_client_path(monkeypatch, legacy_audit):
    _install_fake_lark(monkeypatch)
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    client = RecordingClient()
    doc_tool.set_client(client)
    try:
        with feishu_broker_context(
            GRANT_HANDLE,
            action_id=ACTION_ID,
            contract_hash=CONTRACT_HASH,
            route_partition_key=ROUTE_PARTITION_KEY,
        ):
            result = _tool_json(doc_tool._handle_feishu_doc_read({"doc_token": "doccnAllowed"}))
    finally:
        doc_tool.set_client(None)

    assert client.requests
    assert result["success"] is True
    assert legacy_audit == []


@pytest.mark.parametrize(
    ("tool_name", "handler", "args", "surface"),
    [
        (
            "feishu_drive_list_comments",
            "_handle_list_comments",
            {"file_token": "fileUnsafeToken"},
            "drive",
        ),
        (
            "feishu_drive_list_comment_replies",
            "_handle_list_replies",
            {"file_token": "fileUnsafeToken", "comment_id": "commentUnsafe"},
            "drive",
        ),
        (
            "feishu_drive_reply_comment",
            "_handle_reply_comment",
            {"file_token": "fileUnsafeToken", "comment_id": "commentUnsafe", "content": "raw body"},
            "drive",
        ),
        (
            "feishu_drive_add_comment",
            "_handle_add_comment",
            {"file_token": "fileUnsafeToken", "content": "raw body"},
            "drive",
        ),
    ],
)
def test_drive_checks_and_handlers_deny_before_client_request_without_context(
    monkeypatch, legacy_audit, tool_name, handler, args, surface
):
    drive_tool = importlib.import_module("tools.feishu_drive_tool")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    client = RecordingClient()
    drive_tool.set_client(client)
    try:
        assert drive_tool._check_feishu() is False
        result = _tool_json(getattr(drive_tool, handler)(args))
    finally:
        drive_tool.set_client(None)

    assert client.requests == []
    assert result["success"] is False
    assert result["failure_class"] == "feishu_legacy_tool_requires_broker"
    event = legacy_audit[-1][0]
    assert event["tool"] == tool_name
    assert event["surface"] == surface
    serialized = json.dumps(event)
    assert "fileUnsafeToken" not in serialized
    assert "commentUnsafe" not in serialized
    assert "raw body" not in serialized


def test_drive_do_request_guards_as_defense_in_depth(legacy_audit):
    drive_tool = importlib.import_module("tools.feishu_drive_tool")
    client = RecordingClient()

    code, msg, data = drive_tool._do_request(
        client,
        "GET",
        "/open-apis/drive/v1/files/:file_token/comments",
        paths={"file_token": "fileUnsafeToken"},
    )

    assert client.requests == []
    assert code is None
    assert msg == "feishu_legacy_tool_requires_broker"
    assert data == {"failure_class": "feishu_legacy_tool_requires_broker"}
    assert legacy_audit[-1][0]["tool"] == "feishu_drive._do_request"


def test_registry_and_model_tool_definitions_hide_legacy_tools_without_context(monkeypatch, legacy_audit):
    import model_tools
    from tools.registry import registry

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    legacy_names = {
        "feishu_doc_read",
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
        "feishu_drive_reply_comment",
        "feishu_drive_add_comment",
    }

    for toolset in ("feishu_doc", "feishu_drive", "hermes-feishu"):
        definitions = model_tools.get_tool_definitions([toolset], quiet_mode=True)
        names = {tool["function"]["name"] for tool in definitions}
        assert names.isdisjoint(legacy_names)

    direct = registry.get_definitions(legacy_names, quiet=True)
    assert {tool["function"]["name"] for tool in direct}.isdisjoint(legacy_names)


def test_feishu_tool_visibility_does_not_leak_after_broker_context_exits(monkeypatch, legacy_audit):
    import model_tools
    from tools.registry import registry

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ):
        context_defs = model_tools.get_tool_definitions(["feishu_doc", "feishu_drive"], quiet_mode=True)
        assert "feishu_doc_read" in {tool["function"]["name"] for tool in context_defs}
        assert registry.get_definitions({"feishu_doc_read"}, quiet=True)

    after_defs = model_tools.get_tool_definitions(["feishu_doc", "feishu_drive"], quiet_mode=True)
    after_names = {tool["function"]["name"] for tool in after_defs}
    assert "feishu_doc_read" not in after_names
    assert registry.get_definitions({"feishu_doc_read"}, quiet=True) == []


def test_direct_registry_dispatch_and_model_tools_call_deny_before_client_request(legacy_audit):
    import model_tools
    from tools.registry import registry
    from tools import feishu_doc_tool

    client = RecordingClient()
    feishu_doc_tool.set_client(client)
    try:
        registry_result = _tool_json(registry.dispatch("feishu_doc_read", {"doc_token": "doccnUnsafeToken"}))
        model_result = _tool_json(model_tools.handle_function_call("feishu_doc_read", {"doc_token": "doccnUnsafeToken"}))
    finally:
        feishu_doc_tool.set_client(None)

    assert client.requests == []
    assert registry_result["failure_class"] == "feishu_legacy_tool_requires_broker"
    assert model_result["failure_class"] == "feishu_legacy_tool_requires_broker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("func_name", "kwargs"),
    [
        ("add_comment_reaction", {"file_token": "fileUnsafeToken", "file_type": "docx", "reply_id": "replyUnsafe"}),
        ("delete_comment_reaction", {"file_token": "fileUnsafeToken", "file_type": "docx", "reply_id": "replyUnsafe"}),
    ],
)
async def test_comment_api_reaction_helpers_deny_before_client_request_without_context(
    legacy_audit, func_name, kwargs
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()

    result = await getattr(comment, func_name)(client, **kwargs)

    assert result is False
    assert client.requests == []
    assert legacy_audit[-1][0]["surface"] == "comment"


@pytest.mark.asyncio
async def test_comment_exec_request_audit_failure_returns_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()

    def _apply(event, state_dir, **kwargs):
        return GatewayEventResult(
            ok=False,
            event_type=event["type"],
            failure_class="gateway_event_state_io_failed",
            reason="state ledger IO failed",
        )

    monkeypatch.setenv("HERMES_GATEWAY_EVENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_FEISHU_LEGACY_AUDIT_EVENT_HASH", SAFE_EVENT_HASH)
    monkeypatch.setattr("gateway.gateway_event_ledger.apply_gateway_event", _apply)

    code, msg, data = await comment._exec_request(
        client,
        "GET",
        "/open-apis/drive/v1/files/:file_token/comments",
        paths={"file_token": "fileUnsafeToken"},
    )

    assert client.requests == []
    assert code is None
    assert msg == "feishu_denial_audit_unavailable"
    assert data["failure_class"] == "feishu_denial_audit_unavailable"
    assert data["audit_failure_class"] == "gateway_event_state_io_failed"


def _make_audit_write_fail(monkeypatch, tmp_path):
    def _apply(event, state_dir, **kwargs):
        return GatewayEventResult(
            ok=False,
            event_type=event["type"],
            failure_class="gateway_event_state_io_failed",
            reason="state ledger IO failed",
        )

    monkeypatch.setenv("HERMES_GATEWAY_EVENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_FEISHU_LEGACY_AUDIT_EVENT_HASH", SAFE_EVENT_HASH)
    monkeypatch.setattr("gateway.gateway_event_ledger.apply_gateway_event", _apply)


@pytest.mark.asyncio
async def test_comment_dict_helper_audit_failure_returns_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = await comment.query_document_meta(client, "fileUnsafeToken", "docx")

    assert client.requests == []
    assert result["failure_class"] == "feishu_denial_audit_unavailable"
    assert result["audit_failure_class"] == "gateway_event_state_io_failed"
    assert result["audit_event"] == "feishu_legacy_tool_denied"


@pytest.mark.asyncio
async def test_comment_list_helper_audit_failure_returns_falsey_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = await comment.list_comment_replies(
        client, "fileUnsafeToken", "docx", "commentUnsafe"
    )

    assert client.requests == []
    assert not result
    assert result.failure_class == "feishu_denial_audit_unavailable"
    assert result.audit_failure_class == "gateway_event_state_io_failed"


@pytest.mark.asyncio
async def test_comment_tuple_helper_audit_failure_returns_typed_reason(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = await comment.reply_to_comment(
        client, "fileUnsafeToken", "docx", "commentUnsafe", "raw body"
    )

    assert client.requests == []
    assert result == (False, "feishu_denial_audit_unavailable")


@pytest.mark.asyncio
async def test_comment_bool_helper_audit_failure_returns_falsey_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = await comment.add_comment_reaction(
        client,
        file_token="fileUnsafeToken",
        file_type="docx",
        reply_id="replyUnsafe",
    )

    assert client.requests == []
    assert not result
    assert result.failure_class == "feishu_denial_audit_unavailable"
    assert result.audit_failure_class == "gateway_event_state_io_failed"


def test_comment_string_helper_audit_failure_returns_falsey_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = comment._run_comment_agent(
        "prompt with raw path /tmp/x", RecordingClient(), "session"
    )

    assert not result
    assert result.failure_class == "feishu_denial_audit_unavailable"
    assert result.audit_failure_class == "gateway_event_state_io_failed"


@pytest.mark.asyncio
async def test_comment_event_handler_audit_failure_returns_typed_unavailable(
    monkeypatch, tmp_path
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    data = SimpleNamespace(
        event={
            "event_id": "eventUnsafe",
            "comment_id": "commentUnsafe",
            "reply_id": "replyUnsafe",
            "is_mentioned": True,
            "notice_meta": {
                "file_token": "fileUnsafeToken",
                "file_type": "docx",
                "notice_type": "add_reply",
                "from_user_id": {"open_id": "ou_user"},
                "to_user_id": {"open_id": "ou_bot"},
            },
        }
    )
    _make_audit_write_fail(monkeypatch, tmp_path)

    result = await comment.handle_drive_comment_event(
        client, data, self_open_id="ou_bot"
    )

    assert client.requests == []
    assert not result
    assert result.failure_class == "feishu_denial_audit_unavailable"
    assert result.audit_failure_class == "gateway_event_state_io_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("func_name", "args"),
    [
        ("query_document_meta", ("fileUnsafeToken", "docx")),
        ("batch_query_comment", ("fileUnsafeToken", "docx", "commentUnsafe")),
        ("list_whole_comments", ("fileUnsafeToken", "docx")),
        ("list_comment_replies", ("fileUnsafeToken", "docx", "commentUnsafe")),
        ("_reverse_lookup_wiki_token", ("docx", "fileUnsafeToken")),
        ("_resolve_wiki_nodes", ([{"url": "https://example.test", "doc_type": "wiki", "token": "wikiUnsafeToken"}],)),
    ],
)
async def test_comment_query_paths_deny_before_client_request_without_context(
    legacy_audit, func_name, args
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()

    result = await getattr(comment, func_name)(client, *args)

    assert client.requests == []
    assert result in ({}, [], None)
    assert legacy_audit[-1][0]["surface"] == "comment"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("func_name", "args", "expected"),
    [
        ("reply_to_comment", ("fileUnsafeToken", "docx", "commentUnsafe", "raw body"), (False, None)),
        ("add_whole_comment", ("fileUnsafeToken", "docx", "raw body"), False),
        ("deliver_comment_reply", ("fileUnsafeToken", "docx", "commentUnsafe", "raw body", False), False),
    ],
)
async def test_comment_write_paths_deny_before_client_request_without_context(
    legacy_audit, func_name, args, expected
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()

    result = await getattr(comment, func_name)(client, *args)

    assert client.requests == []
    assert result == expected
    event = legacy_audit[-1][0]
    assert event["surface"] == "comment"
    assert "raw body" not in json.dumps(event)


def test_comment_agent_injection_denies_without_broker_context(monkeypatch, legacy_audit):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    calls = []

    class _Agent:
        def __init__(self, *args, **kwargs):
            calls.append(("agent", args, kwargs))

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))

    result = comment._run_comment_agent("prompt with raw path /tmp/x", RecordingClient(), "session")

    assert result == ""
    assert calls == []
    event = legacy_audit[-1][0]
    assert event["tool"] == "feishu_comment._run_comment_agent"
    assert "/tmp/x" not in json.dumps(event)


@pytest.mark.asyncio
async def test_handle_drive_comment_event_denies_before_comment_client_calls(monkeypatch, legacy_audit):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    client = RecordingClient()
    data = SimpleNamespace(
        event={
            "event_id": "eventUnsafe",
            "comment_id": "commentUnsafe",
            "reply_id": "replyUnsafe",
            "is_mentioned": True,
            "notice_meta": {
                "file_token": "fileUnsafeToken",
                "file_type": "docx",
                "notice_type": "add_reply",
                "from_user_id": {"open_id": "ou_user"},
                "to_user_id": {"open_id": "ou_bot"},
            },
        }
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu_comment.add_comment_reaction",
        pytest.fail,
    )

    await comment.handle_drive_comment_event(client, data, self_open_id="ou_bot")

    assert client.requests == []
    event = legacy_audit[-1][0]
    assert event["tool"] == "feishu_comment.handle_drive_comment_event"
    assert event["surface"] == "comment"
    assert "fileUnsafeToken" not in json.dumps(event)


@pytest.mark.asyncio
async def test_comment_event_executor_preserves_broker_context_for_agent(
    monkeypatch, legacy_audit
):
    comment = importlib.import_module("gateway.platforms.feishu_comment")
    from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

    observations = []

    class _Agent:
        def __init__(self, *args, **kwargs):
            allowed, reason = require_feishu_broker_context(
                "comment", "feishu_comment._run_comment_agent"
            )
            observations.append(("init_context", allowed, reason))

        def run_conversation(self, prompt, conversation_history=None):
            from tools.feishu_doc_tool import get_client as get_doc_client
            from tools.feishu_drive_tool import get_client as get_drive_client

            allowed, reason = require_feishu_broker_context(
                "comment", "feishu_comment._run_comment_agent"
            )
            observations.append(("run_context", allowed, reason))
            observations.append(("doc_client_injected", get_doc_client() is client))
            observations.append(("drive_client_injected", get_drive_client() is client))
            return {"final_response": "NO_REPLY", "api_calls": 0, "messages": []}

    client = RecordingClient()
    data = SimpleNamespace(
        event={
            "event_id": "eventUnsafe",
            "comment_id": "commentUnsafe",
            "reply_id": "replyUnsafe",
            "is_mentioned": True,
            "notice_meta": {
                "file_token": "fileUnsafeToken",
                "file_type": "docx",
                "notice_type": "add_reply",
                "from_user_id": {"open_id": "ou_user"},
                "to_user_id": {"open_id": "ou_bot"},
            },
        }
    )
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))
    monkeypatch.setattr(
        comment,
        "_resolve_model_and_runtime",
        lambda: ("test-model", {"provider": "test"}),
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu_comment_rules.load_config", lambda: object()
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu_comment_rules.resolve_rule",
        lambda *args, **kwargs: ResolvedCommentRule(
            True, "allowlist", frozenset({"ou_user"}), "exact:docx"
        ),
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu_comment_rules.is_user_allowed",
        lambda rule, user: True,
    )
    monkeypatch.setattr(
        "gateway.platforms.feishu_comment_rules.has_wiki_keys", lambda cfg: False
    )
    async def _noop_comment_api(*args, **kwargs):
        return True

    monkeypatch.setattr(comment, "add_comment_reaction", _noop_comment_api)
    monkeypatch.setattr(comment, "delete_comment_reaction", _noop_comment_api)

    async def _query_meta(*args, **kwargs):
        return {"title": "Doc", "url": ""}

    async def _batch_comment(*args, **kwargs):
        return {"is_whole": False, "quote": "quoted"}

    async def _list_replies(*args, **kwargs):
        return [
            {
                "reply_id": "replyUnsafe",
                "user_id": "ou_user",
                "content": {
                    "elements": [
                        {"type": "text_run", "text_run": {"text": "hello"}}
                    ]
                },
            }
        ]

    monkeypatch.setattr(comment, "query_document_meta", _query_meta)
    monkeypatch.setattr(comment, "batch_query_comment", _batch_comment)
    monkeypatch.setattr(comment, "list_comment_replies", _list_replies)

    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ):
        await comment.handle_drive_comment_event(client, data, self_open_id="ou_bot")

    assert ("init_context", True, "") in observations
    assert ("run_context", True, "") in observations
    assert ("doc_client_injected", True) in observations
    assert ("drive_client_injected", True) in observations
    assert legacy_audit == []


def test_denial_audit_write_failure_returns_typed_failure(monkeypatch, tmp_path):
    doc_tool = importlib.import_module("tools.feishu_doc_tool")
    client = RecordingClient()

    def _apply(event, state_dir, **kwargs):
        return GatewayEventResult(
            ok=False,
            event_type=event["type"],
            failure_class="gateway_event_state_io_failed",
            reason="state ledger IO failed",
        )

    monkeypatch.setenv("HERMES_GATEWAY_EVENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_FEISHU_LEGACY_AUDIT_EVENT_HASH", SAFE_EVENT_HASH)
    monkeypatch.setattr("gateway.gateway_event_ledger.apply_gateway_event", _apply)
    doc_tool.set_client(client)
    try:
        result = _tool_json(doc_tool._handle_feishu_doc_read({"doc_token": "doccnUnsafeToken"}))
    finally:
        doc_tool.set_client(None)

    assert client.requests == []
    assert result["success"] is False
    assert result["failure_class"] == "feishu_denial_audit_unavailable"
    assert result["audit_failure_class"] == "gateway_event_state_io_failed"
    assert result["audit_event"] == "feishu_legacy_tool_denied"

"""Tests for topic-aware gateway progress updates."""

import asyncio
import importlib
import re
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


STATUS_CARD_DEFERRED_SKIP = pytest.mark.skip(
    reason="status-card/task card internalization intentionally deferred by user scope"
)


class ProgressCaptureAdapter(BasePlatformAdapter):
    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.typing = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": {"stopped": True}})

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class StatusCardProgressAdapter(ProgressCaptureAdapter):
    def __init__(
        self,
        platform=Platform.FEISHU,
        state_dir=None,
        fail_status_card_execute=False,
        status_card_error="status_card_execute_failed",
    ):
        super().__init__(platform=platform)
        self._hermes_tools_state_dir = state_dir
        self.status_card_actions = []
        self._status_card_counter = 0
        self.fail_status_card_execute = fail_status_card_execute
        self.status_card_error = status_card_error

    async def execute_status_card_action(
        self,
        action,
        *,
        delivery_id,
        inbound_id,
        session_id,
        correlation_id,
    ) -> SendResult:
        self.status_card_actions.append(
            {
                "action": action,
                "delivery_id": delivery_id,
                "inbound_id": inbound_id,
                "session_id": session_id,
                "correlation_id": correlation_id,
            }
        )
        if self.fail_status_card_execute:
            return SendResult(success=False, error=self.status_card_error)
        card_action = action.get("card_action", {}) if isinstance(action, dict) else {}
        if card_action.get("type") == "create":
            self._status_card_counter += 1
            return SendResult(success=True, message_id=f"status-card-{self._status_card_counter}")
        return SendResult(success=True, message_id="status-card-1")



class SmallLimitProgressAdapter(ProgressCaptureAdapter):
    """Adapter with a tiny platform limit to exercise progress rollover."""

    MAX_MESSAGE_LENGTH = 180

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self._next_id = 0
        self.oversized_edits = []
        self.oversized_sends = []

    def _mint_id(self):
        self._next_id += 1
        return f"progress-{self._next_id}"

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if len(content) > self.MAX_MESSAGE_LENGTH:
            self.oversized_sends.append(content)
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=self._mint_id())

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        if len(content) > self.MAX_MESSAGE_LENGTH:
            self.oversized_edits.append(content)
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)


class MetadataEditProgressCaptureAdapter(ProgressCaptureAdapter):
    async def edit_message(
        self, chat_id, message_id, content, *, finalize: bool = False, metadata=None
    ) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=message_id)


class NonEditingProgressCaptureAdapter(ProgressCaptureAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        raise AssertionError("non-editable adapters should not receive edit_message calls")


class FakeAgent:
    def __init__(self, **kwargs):
        # Capture anything passed via kwargs (older code path) but don't
        # freeze it — production now assigns tool_progress_callback after
        # construction (see gateway/run.py around the agent-cache hit),
        # so we must read it at call time, not at init.
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.35)
            cb("tool.started", "browser_navigate", "https://example.com", {})
            time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class NoProgressAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class LongPreviewAgent:
    """Agent that emits a tool call with a very long preview string."""
    LONG_CMD = "cd /home/teknium/.hermes/hermes-agent/.worktrees/hermes-d8860339 && source .venv/bin/activate && python -m pytest tests/gateway/test_run_progress_topics.py -n0 -q"

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.tool_progress_callback:
            self.tool_progress_callback("tool.started", "terminal", self.LONG_CMD, {})
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DelayedProgressAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.tool_progress_callback:
            self.tool_progress_callback("tool.started", "terminal", "first command", {})
        time.sleep(0.45)
        if self.tool_progress_callback:
            self.tool_progress_callback("tool.started", "terminal", "second command", {})
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class ManyProgressLinesAgent:
    """Emits enough tool-progress lines to exceed a single platform bubble."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        assert cb is not None
        cb("tool.started", "terminal", "first-short", {})
        # Let the progress task create the first editable bubble, then enqueue
        # the rest quickly.  The cancellation drain must roll them into fresh
        # editable bubbles instead of trying to edit the first one past limit.
        time.sleep(0.35)
        for idx in range(1, 8):
            cb("tool.started", "terminal", f"overflow-line-{idx}-" + "x" * 45, {})
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DelayedInterimAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("first interim")
        time.sleep(0.45)
        if self.interim_assistant_callback:
            self.interim_assistant_callback("second interim")
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner._draining = False
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _status_card_apply_result(action_type: str, state: str):
    return SimpleNamespace(
        ok=True,
        event_type="task_status",
        action={
            "type": "status_card",
            "card_action": {
                "type": action_type,
                "card_id": "task-card",
                "state": state,
                "text": f"{state} text",
                "requires_final_reply": state in {"completed", "failed"},
                "fallback_text": f"{state} fallback",
                "feishu_card": {"config": {"wide_screen_mode": True}},
                "feishu_request": {
                    "operation": (
                        "send_interactive_message"
                        if action_type == "create"
                        else "patch_interactive_message"
                    ),
                    "method": "POST" if action_type == "create" else "PATCH",
                    "path": (
                        "/open-apis/im/v1/messages"
                        if action_type == "create"
                        else "/open-apis/im/v1/messages/status-card-1"
                    ),
                    "params": {"receive_id_type": "chat_id"} if action_type == "create" else {},
                    "body": {"content": "{}"},
                },
            },
        },
        failure_class=None,
        reason=None,
        diagnostics="",
    )


def _install_fake_agent(monkeypatch, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


async def _run_feishu_status_card_agent(
    monkeypatch,
    tmp_path,
    agent_cls=FakeAgent,
    *,
    adapter=None,
    apply_result_factory=None,
):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    _install_fake_agent(monkeypatch, agent_cls)

    events = []

    async def fake_apply_gateway_event_async(event, state_dir, **kwargs):
        events.append(dict(event))
        if apply_result_factory is not None:
            return apply_result_factory(event)
        action_type = "update" if event.get("message_id") else "create"
        return _status_card_apply_result(action_type, event["state"])

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "apply_gateway_event_async", fake_apply_gateway_event_async, raising=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    if adapter is None:
        adapter = StatusCardProgressAdapter(state_dir=tmp_path / "hermes-tools-state")
    runner = _make_runner(adapter)
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id="topic_17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-feishu-status",
        session_key="agent:main:feishu:group:oc_chat:topic_17585",
        event_message_id="om_triggering_user_message",
    )
    return adapter, events, result


@pytest.mark.asyncio
async def test_run_agent_progress_stays_in_originating_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji for this fake-agent test

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert adapter.sent == [
        {
            "chat_id": "-1001",
            "content": '💻 terminal: "pwd"',
            "reply_to": None,
            "metadata": {"thread_id": "17585"},
        }
    ]
    assert adapter.edits
    assert all(call["metadata"] == {"thread_id": "17585"} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_progress_edits_keep_originating_topic_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = MetadataEditProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-progress-edit-topic",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert adapter.edits
    assert all(call["metadata"] == {"thread_id": "17585"} for call in adapter.edits)


@pytest.mark.asyncio
async def test_run_agent_progress_does_not_use_event_message_id_for_telegram_dm(monkeypatch, tmp_path):
    """Telegram DM progress must not reuse event message id as thread metadata."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-2",
        session_key="agent:main:telegram:dm:12345",
        event_message_id="777",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["metadata"] is None
    assert all(call["metadata"] is None for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_progress_uses_event_message_id_for_slack_dm(monkeypatch, tmp_path):
    """Slack DM progress should keep event ts fallback threading."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    # Since PR #8006, Slack's built-in display tier sets tool_progress="off"
    # by default. Override via config so this test still exercises the
    # progress-callback path the Slack DM event_message_id threading depends on.
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"platforms": {"slack": {"tool_progress": "all"}}}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.SLACK)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-3",
        session_key="agent:main:slack:dm:D123",
        event_message_id="1234567890.000001",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["metadata"] == {"thread_id": "1234567890.000001"}
    assert all(call["metadata"] == {"thread_id": "1234567890.000001"} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_feishu_progress_replies_inside_existing_thread(monkeypatch, tmp_path):
    """Feishu needs reply_to plus reply_in_thread metadata for topic-scoped progress."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.FEISHU)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id="topic_17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-feishu-progress",
        session_key="agent:main:feishu:group:oc_chat:topic_17585",
        event_message_id="om_triggering_user_message",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["reply_to"] == "om_triggering_user_message"
    assert adapter.sent[0]["metadata"] == {
        "thread_id": "topic_17585",
        "reply_to_message_id": "om_triggering_user_message",
    }
    assert adapter.edits
    assert adapter.edits[0]["message_id"] == "progress-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("state_attr", ["_gateway_event_state_dir", "_hermes_tools_state_dir"])
async def test_run_agent_feishu_state_dir_uses_normal_progress_fallback(
    monkeypatch, tmp_path, state_attr
):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    _install_fake_agent(monkeypatch, FakeAgent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    events = []

    async def fake_apply_gateway_event_async(event, state_dir, **kwargs):
        events.append((dict(event), state_dir))
        return SimpleNamespace(
            ok=True,
            event_type="task_status",
            action=None,
            failure_class=None,
            reason=None,
            diagnostics="",
        )

    monkeypatch.setattr(
        gateway_run,
        "apply_gateway_event_async",
        fake_apply_gateway_event_async,
        raising=False,
    )

    adapter = StatusCardProgressAdapter()
    setattr(adapter, state_attr, tmp_path / f"{state_attr.removeprefix('_')}-state")
    runner = _make_runner(adapter)
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id="topic_17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id=f"sess-feishu-progress-fallback-{state_attr}",
        session_key=f"agent:main:feishu:group:oc_chat:topic_17585:{state_attr}",
        event_message_id="om_triggering_user_message",
    )

    assert result["final_response"] == "done"
    assert events == []
    assert adapter.status_card_actions == []
    assert adapter.sent
    assert adapter.sent[0]["reply_to"] == "om_triggering_user_message"
    assert adapter.sent[0]["metadata"] == {
        "thread_id": "topic_17585",
        "reply_to_message_id": "om_triggering_user_message",
    }
    assert adapter.edits
    assert adapter.edits[0]["message_id"] == "progress-1"


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_progress_emits_task_status_create_then_patch(monkeypatch, tmp_path):
    adapter, events, result = await _run_feishu_status_card_agent(monkeypatch, tmp_path)

    assert result["final_response"] == "done"
    task_status_events = [event for event in events if event.get("type") == "task_status"]
    assert len(task_status_events) >= 2
    assert task_status_events[0]["state"] == "running"
    assert task_status_events[0]["receive_id_type"] == "chat_id"
    assert task_status_events[0]["receive_id"] == "oc_chat"
    assert task_status_events[0].get("message_id") is None
    assert task_status_events[0]["idempotency_key"]
    assert task_status_events[1]["state"] in {"running", "completed"}
    assert task_status_events[1]["message_id"] == "status-card-1"
    assert [call["action"]["card_action"]["type"] for call in adapter.status_card_actions[:2]] == [
        "create",
        "update",
    ]


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_status_card_delivery_ids_distinguish_patch_sequence(
    monkeypatch, tmp_path
):
    adapter, events, result = await _run_feishu_status_card_agent(monkeypatch, tmp_path)

    assert result["final_response"] == "done"
    task_status_events = [event for event in events if event.get("type") == "task_status"]
    assert len(task_status_events) >= 3
    assert [event["state"] for event in task_status_events[:3]] == [
        "running",
        "running",
        "completed",
    ]

    action_calls = adapter.status_card_actions[:3]
    assert [call["action"]["card_action"]["type"] for call in action_calls] == [
        "create",
        "update",
        "update",
    ]

    delivery_ids = [call["delivery_id"] for call in action_calls]
    assert len(delivery_ids) == len(set(delivery_ids))
    assert all(re.match(r"^[A-Za-z0-9_-]+$", delivery_id) for delivery_id in delivery_ids)
    assert "create" in delivery_ids[0]
    assert "patch" in delivery_ids[1]
    assert "patch" in delivery_ids[2]


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_hermes_status_card_delivery_ids_keep_sequence_when_task_id_is_max_length(
    monkeypatch, tmp_path
):
    gateway_run = importlib.import_module("gateway.run")
    events = []

    async def fake_apply_gateway_event_async(event, state_dir, **kwargs):
        events.append(dict(event))
        action_type = "update" if event.get("message_id") else "create"
        return _status_card_apply_result(action_type, event["state"])

    monkeypatch.setattr(
        gateway_run,
        "apply_gateway_event_async",
        fake_apply_gateway_event_async,
        raising=False,
    )

    adapter = StatusCardProgressAdapter(state_dir=tmp_path / "hermes-tools-state")
    task_id = "t" * 160
    context = gateway_run._HermesTaskStatusContext(
        task_id=task_id,
        session_id="session-long-task-id",
        inbound_id="inbound-long-task-id",
        correlation_id="corr-long-task-id",
        receive_id_type="chat_id",
        receive_id="oc_chat",
        idempotency_key="status-card-create-long-task-id",
    )

    assert await gateway_run._emit_hermes_task_status(
        adapter,
        context,
        state="running",
        text="started",
        allow_create=True,
    )
    assert await gateway_run._emit_hermes_task_status(
        adapter,
        context,
        state="running",
        text="progress",
    )
    assert await gateway_run._emit_hermes_task_status(
        adapter,
        context,
        state="completed",
        text="done",
    )

    assert [event["state"] for event in events] == ["running", "running", "completed"]
    delivery_ids = [call["delivery_id"] for call in adapter.status_card_actions]
    assert len(delivery_ids) == 3
    assert len(delivery_ids) == len(set(delivery_ids))
    assert all(re.match(r"^[A-Za-z0-9_-]+$", delivery_id) for delivery_id in delivery_ids)
    assert all(len(delivery_id) <= 160 for delivery_id in delivery_ids)
    assert delivery_ids[0].startswith("status-card-create-")
    assert delivery_ids[1].startswith("status-card-patch-1-")
    assert delivery_ids[2].startswith("status-card-patch-2-")


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_terminal_task_status_requires_final_reply_without_suppressing_normal_final(
    monkeypatch, tmp_path
):
    adapter, events, result = await _run_feishu_status_card_agent(monkeypatch, tmp_path)

    terminal_events = [
        event for event in events if event.get("type") == "task_status" and event.get("state") == "completed"
    ]
    assert terminal_events
    assert terminal_events[-1]["message_id"] == "status-card-1"
    terminal_actions = [
        call["action"]["card_action"]
        for call in adapter.status_card_actions
        if call["action"]["card_action"]["state"] == "completed"
    ]
    assert terminal_actions
    assert terminal_actions[-1]["requires_final_reply"] is True
    assert result["final_response"] == "done"
    assert result.get("already_sent") is not True


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_no_progress_does_not_create_completed_status_card(
    monkeypatch, tmp_path
):
    adapter, events, result = await _run_feishu_status_card_agent(
        monkeypatch,
        tmp_path,
        agent_cls=NoProgressAgent,
    )

    assert result["final_response"] == "done"
    assert events == []
    assert adapter.status_card_actions == []
    assert adapter.sent == []
    assert adapter.edits == []


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_terminal_task_status_without_binding_is_noop(monkeypatch, tmp_path):
    adapter, events, result = await _run_feishu_status_card_agent(
        monkeypatch,
        tmp_path,
        agent_cls=NoProgressAgent,
    )

    assert result["final_response"] == "done"
    assert not [event for event in events if event.get("state") == "completed"]
    assert not [
        call
        for call in adapter.status_card_actions
        if call["action"]["card_action"]["type"] == "create"
    ]


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_terminal_task_status_with_binding_patches_completed(
    monkeypatch, tmp_path
):
    adapter, events, result = await _run_feishu_status_card_agent(monkeypatch, tmp_path)

    assert result["final_response"] == "done"
    terminal_events = [
        event for event in events if event.get("type") == "task_status" and event.get("state") == "completed"
    ]
    assert terminal_events
    assert terminal_events[-1]["message_id"] == "status-card-1"
    terminal_actions = [
        call["action"]["card_action"]
        for call in adapter.status_card_actions
        if call["action"]["card_action"]["state"] == "completed"
    ]
    assert terminal_actions[-1]["type"] == "update"


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_status_card_apply_failure_suppresses_progress_fallback(
    monkeypatch, tmp_path
):
    def apply_failure(event):
        return SimpleNamespace(
            ok=False,
            event_type="task_status",
            action=None,
            failure_class="status_card_apply_failed",
            reason="status_card_apply_failed",
            diagnostics="",
        )

    adapter, events, result = await _run_feishu_status_card_agent(
        monkeypatch,
        tmp_path,
        apply_result_factory=apply_failure,
    )

    assert result["final_response"] == "done"
    assert [event for event in events if event.get("type") == "task_status"]
    assert adapter.status_card_actions == []
    assert adapter.sent == []
    assert adapter.edits == []


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_status_card_execute_failure_suppresses_progress_fallback(
    monkeypatch, tmp_path
):
    adapter = StatusCardProgressAdapter(
        state_dir=tmp_path / "hermes-tools-state",
        fail_status_card_execute=True,
    )

    adapter, events, result = await _run_feishu_status_card_agent(
        monkeypatch,
        tmp_path,
        adapter=adapter,
    )

    assert result["final_response"] == "done"
    assert [event for event in events if event.get("type") == "task_status"]
    assert adapter.status_card_actions
    assert adapter.sent == []
    assert adapter.edits == []


@STATUS_CARD_DEFERRED_SKIP
@pytest.mark.asyncio
async def test_run_agent_feishu_status_card_execute_failure_logs_stable_failure_class(
    monkeypatch, tmp_path, caplog
):
    raw_error = "POST /private/raw/path token=sk-REDACTED"
    adapter = StatusCardProgressAdapter(
        state_dir=tmp_path / "hermes-tools-state",
        fail_status_card_execute=True,
        status_card_error=raw_error,
    )

    adapter, events, result = await _run_feishu_status_card_agent(
        monkeypatch,
        tmp_path,
        adapter=adapter,
    )

    assert result["final_response"] == "done"
    assert [event for event in events if event.get("type") == "task_status"]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "failure_class=status_card_execute_failed" in log_text
    assert raw_error not in log_text
    assert "/private/raw/path" not in log_text
    assert "sk-REDACTED" not in log_text


@pytest.mark.asyncio
async def test_start_gateway_runs_stale_pending_scan_after_runner_start_before_wait(
    monkeypatch, tmp_path
):
    gateway_run = importlib.import_module("gateway.run")
    calls = []
    adapter = ProgressCaptureAdapter(platform=Platform.FEISHU)
    adapter._hermes_tools_state_dir = tmp_path / "hermes-tools-state"

    class _CleanExitRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self.should_exit_cleanly = True
            self.exit_reason = None

        async def start(self):
            calls.append("start")
            self.adapters = {Platform.FEISHU: adapter}
            return True

        async def stop(self):
            return None

        async def wait_for_shutdown(self):
            calls.append("wait")

    async def fake_scan(adapters, **kwargs):
        calls.append(("scan", dict(adapters)))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: None, raising=False)
    monkeypatch.setattr(gateway_run, "_run_planned_stop_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "GatewayRunner", _CleanExitRunner)
    monkeypatch.setattr(gateway_run, "_run_stale_pending_scan_for_adapters", fake_scan, raising=False)

    ok = await gateway_run.start_gateway(config=GatewayConfig(), replace=False, verbosity=None)

    assert ok is True
    assert calls[:2] == ["start", ("scan", {Platform.FEISHU: adapter})]
    assert "wait" not in calls
    assert adapter.sent == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_stale_pending_scan_deduplicates_shared_state_dir(monkeypatch, tmp_path):
    gateway_run = importlib.import_module("gateway.run")
    shared_state_dir = tmp_path / "shared-hermes-tools-state"
    gateway_event_state_dir = tmp_path / "shared-gateway-event-state"
    adapter_a = ProgressCaptureAdapter(platform=Platform.FEISHU)
    adapter_b = ProgressCaptureAdapter(platform=Platform.SLACK)
    adapter_a._hermes_tools_state_dir = shared_state_dir
    adapter_b._hermes_tools_state_dir = shared_state_dir
    adapter_a._gateway_event_state_dir = gateway_event_state_dir
    adapter_b._gateway_event_state_dir = gateway_event_state_dir
    calls = []

    async def fake_apply_gateway_event_async(event, state_dir, **kwargs):
        calls.append((dict(event), state_dir))
        return SimpleNamespace(
            ok=True,
            event_type="stale_pending_scan",
            action={
                "type": "stale_pending_alert",
                "alert_required": False,
                "resend_permitted": False,
                "count": 0,
                "records": [],
            },
            failure_class=None,
            reason=None,
            diagnostics="",
        )

    monkeypatch.setattr(gateway_run, "apply_gateway_event_ledger_async", fake_apply_gateway_event_async, raising=False)

    await gateway_run._run_stale_pending_scan_for_adapters(
        {Platform.FEISHU: adapter_a, Platform.SLACK: adapter_b}
    )

    assert len(calls) == 1
    assert calls[0][0]["type"] == "stale_pending_scan"
    assert calls[0][1] == gateway_event_state_dir


@pytest.mark.asyncio
async def test_cron_ticker_runs_periodic_stale_pending_scan_without_resend(monkeypatch, tmp_path):
    gateway_run = importlib.import_module("gateway.run")
    adapter = ProgressCaptureAdapter(platform=Platform.FEISHU)
    adapter._hermes_tools_state_dir = tmp_path / "hermes-tools-state"
    stop_event = threading.Event()
    applied_events = []

    async def fake_apply_gateway_event_async(event, state_dir, **kwargs):
        applied_events.append(dict(event))
        return SimpleNamespace(
            ok=True,
            event_type="stale_pending_scan",
            action={"type": "stale_pending_alert", "alert_required": False, "resend_permitted": False, "count": 0, "records": []},
            failure_class=None,
            reason=None,
            diagnostics="",
        )

    monkeypatch.setattr("cron.scheduler.tick", lambda **kwargs: None)
    monkeypatch.setattr(gateway_run, "apply_gateway_event_ledger_async", fake_apply_gateway_event_async, raising=False)

    thread = threading.Thread(
        target=gateway_run._start_cron_ticker,
        args=(stop_event,),
        kwargs={
            "adapters": {Platform.FEISHU: adapter},
            "loop": asyncio.get_running_loop(),
            "interval": 0.01,
            "stale_scan_interval_ticks": 1,
        },
    )
    thread.start()
    await asyncio.sleep(0.05)
    stop_event.set()
    thread.join(timeout=1)

    stale_events = [event for event in applied_events if event.get("type") == "stale_pending_scan"]
    assert stale_events
    assert all(event["max_age_seconds"] > 0 for event in stale_events)
    assert adapter.sent == []
    assert adapter.edits == []


# ---------------------------------------------------------------------------
# Preview truncation tests (all/new mode respects tool_preview_length)
# ---------------------------------------------------------------------------


def _run_long_preview_helper(monkeypatch, tmp_path, preview_length=0):
    """Shared setup for long-preview truncation tests.

    Returns (adapter, result) after running the agent with LongPreviewAgent.
    ``preview_length`` controls display.tool_preview_length in the config file
    that _run_agent reads — so the gateway picks it up the same way production does.
    """
    import asyncio
    import yaml

    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = LongPreviewAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    # Write config.yaml so _run_agent picks up tool_preview_length
    config = {"display": {"tool_preview_length": preview_length}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = asyncio.get_event_loop().run_until_complete(
        runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-trunc",
            session_key="agent:main:telegram:dm:12345",
        )
    )
    return adapter, result


def test_all_mode_default_truncation_40_chars(monkeypatch, tmp_path):
    """When tool_preview_length is 0 (default), all/new mode truncates to 40 chars."""
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=0)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # The long command should be truncated — total preview <= 40 chars
    assert "..." in content
    # Extract the preview part between quotes
    import re
    match = re.search(r'"(.+)"', content)
    assert match, f"No quoted preview found in: {content}"
    preview_text = match.group(1)
    assert len(preview_text) <= 40, f"Preview too long ({len(preview_text)}): {preview_text}"


def test_all_mode_respects_custom_preview_length(monkeypatch, tmp_path):
    """When tool_preview_length is explicitly set (e.g. 120), all/new mode uses that."""
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=120)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # With 120-char cap, the command (165 chars) should still be truncated but longer
    import re
    match = re.search(r'"(.+)"', content)
    assert match, f"No quoted preview found in: {content}"
    preview_text = match.group(1)
    # Should be longer than the 40-char default
    assert len(preview_text) > 40, f"Preview suspiciously short ({len(preview_text)}): {preview_text}"
    # But still capped at 120
    assert len(preview_text) <= 120, f"Preview too long ({len(preview_text)}): {preview_text}"


def test_all_mode_no_truncation_when_preview_fits(monkeypatch, tmp_path):
    """Short previews (under the cap) are not truncated."""
    # Set a generous cap — the LongPreviewAgent's command is ~165 chars
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=200)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # With a 200-char cap, the 165-char command should NOT be truncated
    assert "..." not in content, f"Preview was truncated when it shouldn't be: {content}"


class CommentaryAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("I'll inspect the repo first.", already_streamed=False)
        time.sleep(0.1)
        if self.stream_delta_callback:
            self.stream_delta_callback("done")
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class PreviewedResponseAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("You're welcome.", already_streamed=False)
        return {
            "final_response": "You're welcome.",
            "response_previewed": True,
            "messages": [],
            "api_calls": 1,
        }


class StreamingRefineAgent:
    def __init__(self, **kwargs):
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.stream_delta_callback:
            self.stream_delta_callback("Continuing to refine:")
        time.sleep(0.1)
        if self.stream_delta_callback:
            self.stream_delta_callback(" Final answer.")
        return {
            "final_response": "Continuing to refine: Final answer.",
            "response_previewed": True,
            "messages": [],
            "api_calls": 1,
        }


class QueuedCommentaryAgent:
    calls = 0

    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls += 1
        if type(self).calls == 1 and self.interim_assistant_callback:
            self.interim_assistant_callback("I'll inspect the repo first.", already_streamed=False)
        return {
            "final_response": f"final response {type(self).calls}",
            "messages": [],
            "api_calls": 1,
        }


class BackgroundReviewAgent:
    def __init__(self, **kwargs):
        self.background_review_callback = kwargs.get("background_review_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.background_review_callback:
            self.background_review_callback("💾 Skill 'prospect-scanner' created.")
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class VerboseAgent:
    """Agent that emits a tool call with args whose JSON exceeds 200 chars."""
    LONG_CODE = "x" * 300

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback(
            "tool.started", "execute_code", None,
            {"code": self.LONG_CODE},
        )
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


async def _run_with_agent(
    monkeypatch,
    tmp_path,
    agent_cls,
    *,
    session_id,
    pending_text=None,
    config_data=None,
    platform=Platform.TELEGRAM,
    chat_id="-1001",
    chat_type="group",
    thread_id="17585",
    adapter_cls=ProgressCaptureAdapter,
):
    if config_data:
        import yaml

        (tmp_path / "config.yaml").write_text(yaml.dump(config_data), encoding="utf-8")
    else:
        (tmp_path / "config.yaml").write_text(
            "",
            encoding="utf-8",
        )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = adapter_cls(platform=platform)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    if config_data and "streaming" in config_data:
        runner.config.streaming = StreamingConfig.from_dict(config_data["streaming"])
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    source = SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    session_key = f"agent:main:{platform.value}:{chat_type}:{chat_id}"
    if thread_id:
        session_key = f"{session_key}:{thread_id}"
    if pending_text is not None:
        adapter._pending_messages[session_key] = MessageEvent(
            text=pending_text,
            message_type=MessageType.TEXT,
            source=source,
            message_id="queued-1",
        )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )
    return adapter, result


@pytest.mark.asyncio
async def test_run_agent_rolls_progress_bubble_before_platform_limit(monkeypatch, tmp_path):
    """Tool progress should start a second editable bubble before Telegram's limit.

    Regression: once the first progress bubble grew past the platform limit,
    the gateway kept trying to edit that same oversized full transcript.  The
    Telegram adapter then split-and-sent a fresh continuation on every update,
    causing a noisy trail of one-line messages instead of a new editable bubble.
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        ManyProgressLinesAgent,
        session_id="sess-progress-overflow-rollover",
        config_data={
            "display": {
                "tool_progress": "all",
                "interim_assistant_messages": False,
                "tool_preview_length": 60,
            }
        },
        adapter_cls=SmallLimitProgressAdapter,
    )

    assert result["final_response"] == "done"
    assert isinstance(adapter, SmallLimitProgressAdapter)
    assert len(adapter.sent) >= 2, "expected a fresh progress bubble after the first filled"
    assert adapter.oversized_sends == []
    assert adapter.oversized_edits == []
    all_bubbles = [call["content"] for call in adapter.sent + adapter.edits]
    assert all(len(text) <= adapter.MAX_MESSAGE_LENGTH for text in all_bubbles)


@pytest.mark.asyncio
async def test_run_agent_surfaces_real_interim_commentary(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result.get("already_sent") is not True
    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_surfaces_interim_commentary_by_default(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-default-on",
    )

    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_suppresses_interim_commentary_when_disabled(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-disabled",
        config_data={"display": {"interim_assistant_messages": False}},
    )

    assert result.get("already_sent") is not True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_tool_progress_does_not_control_interim_commentary(monkeypatch, tmp_path):
    """tool_progress=all with interim_assistant_messages=false should not surface commentary."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-tool-progress",
        config_data={"display": {"tool_progress": "all", "interim_assistant_messages": False}},
    )

    assert result.get("already_sent") is not True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_streaming_does_not_enable_completed_interim_commentary(
    monkeypatch, tmp_path
):
    """Streaming alone with interim_assistant_messages=false should not surface commentary."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-streaming",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True},
        },
    )

    assert result["final_response"] == "done"
    assert result.get("already_sent") is not True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_display_streaming_does_not_enable_gateway_streaming(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-display-streaming-cli-only",
        config_data={
            "display": {
                "streaming": True,
                "interim_assistant_messages": True,
            },
            "streaming": {"enabled": False},
        },
    )

    assert result.get("already_sent") is not True
    assert adapter.edits == []
    assert [call["content"] for call in adapter.sent] == ["I'll inspect the repo first."]


@pytest.mark.asyncio
async def test_run_agent_interim_commentary_works_with_tool_progress_off(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-explicit-on",
        config_data={
            "display": {
                "tool_progress": "off",
                "interim_assistant_messages": True,
            },
        },
    )

    assert result.get("already_sent") is not True
    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_bluebubbles_uses_commentary_send_path_for_quick_replies(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-bluebubbles-commentary",
        config_data={"display": {"interim_assistant_messages": True}},
        platform=Platform.BLUEBUBBLES,
        chat_id="iMessage;-;user@example.com",
        chat_type="dm",
        thread_id=None,
        adapter_cls=NonEditingProgressCaptureAdapter,
    )

    assert result.get("already_sent") is not True
    assert [call["content"] for call in adapter.sent] == ["I'll inspect the repo first."]
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_run_agent_previewed_final_returns_for_outer_send_path(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        PreviewedResponseAgent,
        session_id="sess-previewed",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result.get("already_sent") is not True
    assert result["final_response"] == "You're welcome."
    assert adapter.sent == []
    await adapter.send("chat-outer", result["final_response"])
    assert [call["content"] for call in adapter.sent] == ["You're welcome."]


@pytest.mark.asyncio
async def test_run_agent_matrix_streaming_omits_cursor(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        StreamingRefineAgent,
        session_id="sess-matrix-streaming",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
        },
        platform=Platform.MATRIX,
        chat_id="!room:matrix.example.org",
        chat_type="group",
        thread_id="$thread",
    )

    assert result.get("already_sent") is not True
    assert result["final_response"] == "Continuing to refine: Final answer."
    await adapter.send("!room:matrix.example.org", result["final_response"])
    all_text = [call["content"] for call in adapter.sent] + [call["content"] for call in adapter.edits]
    assert all_text, "expected Matrix content to be sendable after durable gate"
    assert all("▉" not in text for text in all_text)
    assert any("Continuing to refine:" in text for text in all_text)


class TransformedStreamAgent:
    """Streams a response, then signals the gateway that a plugin hook
    (``transform_llm_output``) modified the final text after streaming
    finished. ``run_conversation`` returns ``response_transformed=True``
    plus a ``final_response`` that diverges from what was streamed.
    """

    def __init__(self, **kwargs):
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.stream_delta_callback:
            self.stream_delta_callback("original answer")
        return {
            "final_response": "original answer\n\n[plugin appended this]",
            "response_previewed": True,
            "response_transformed": True,
            "messages": [],
            "api_calls": 1,
        }


@pytest.mark.asyncio
async def test_transformed_response_edits_streamed_message_in_place(monkeypatch, tmp_path):
    """When a transform_llm_output hook modifies the response after streaming,
    the gateway must edit the existing streamed message in place with the full
    transformed content (so plugins like content filters / appenders reach the
    user) and still mark already_sent=True (no duplicate send).
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TransformedStreamAgent,
        session_id="sess-transformed-stream",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
        },
        platform=Platform.MATRIX,
        chat_id="!room:matrix.example.org",
        chat_type="group",
        thread_id="$thread",
        adapter_cls=MetadataEditProgressCaptureAdapter,
    )

    assert result.get("already_sent") is not True
    assert result["final_response"] == "original answer\n\n[plugin appended this]"
    assert adapter.sent == []
    assert adapter.edits == []
    await adapter.send("!room:matrix.example.org", result["final_response"])
    sent_texts = [call["content"] for call in adapter.sent]
    assert sent_texts == ["original answer\n\n[plugin appended this]"]


@pytest.mark.asyncio
async def test_run_agent_queued_message_does_not_treat_commentary_as_final(monkeypatch, tmp_path):
    QueuedCommentaryAgent.calls = 0
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        QueuedCommentaryAgent,
        session_id="sess-queued-commentary",
        pending_text="queued follow-up",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert result["final_response"] == "final response 2"
    assert "I'll inspect the repo first." in sent_texts
    assert "final response 1" in sent_texts


@pytest.mark.asyncio
async def test_run_agent_defers_background_review_notification_until_release(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        BackgroundReviewAgent,
        session_id="sess-bg-review-order",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result["final_response"] == "done"
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_base_processing_releases_post_delivery_callback_after_main_send():
    """Post-delivery callbacks on the adapter fire after the main response."""
    adapter = ProgressCaptureAdapter()

    async def _handler(event):
        return "done"

    adapter.set_message_handler(_handler)

    released = []

    def _post_delivery_cb():
        released.append(True)
        adapter.sent.append(
            {
                "chat_id": "bg-review",
                "content": "💾 Skill 'prospect-scanner' created.",
                "reply_to": None,
                "metadata": None,
            }
        )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )
    session_key = "agent:main:telegram:group:-1001:17585"
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._post_delivery_callbacks[session_key] = _post_delivery_cb

    await adapter._process_message_background(event, session_key)

    sent_texts = [call["content"] for call in adapter.sent]
    assert sent_texts == ["done", "💾 Skill 'prospect-scanner' created."]
    assert released == [True]


@pytest.mark.asyncio
async def test_run_agent_drops_tool_progress_after_generation_invalidation(monkeypatch, tmp_path):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"tool_progress": "all"}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DelayedProgressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal tool metadata

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-1",
        chat_type="dm",
        thread_id=None,
    )
    session_key = "agent:main:discord:dm:dm-1"
    runner._session_run_generation[session_key] = 1

    original_send = adapter.send
    invalidated = {"done": False}

    async def send_and_invalidate(chat_id, content, reply_to=None, metadata=None):
        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if "first command" in content and not invalidated["done"]:
            invalidated["done"] = True
            runner._invalidate_session_run_generation(session_key, reason="test_stop")
        return result

    adapter.send = send_and_invalidate

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-progress-stop",
        session_key=session_key,
        run_generation=1,
    )

    all_progress_text = " ".join(call["content"] for call in adapter.sent)
    all_progress_text += " ".join(call["content"] for call in adapter.edits)
    assert result["final_response"] == "done"
    assert 'first command' in all_progress_text
    assert 'second command' not in all_progress_text


@pytest.mark.asyncio
async def test_run_agent_drops_interim_commentary_after_generation_invalidation(monkeypatch, tmp_path):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"tool_progress": "off", "interim_assistant_messages": True}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DelayedInterimAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-2",
        chat_type="dm",
        thread_id=None,
    )
    session_key = "agent:main:discord:dm:dm-2"
    runner._session_run_generation[session_key] = 1

    original_send = adapter.send
    invalidated = {"done": False}

    async def send_and_invalidate(chat_id, content, reply_to=None, metadata=None):
        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if content == "first interim" and not invalidated["done"]:
            invalidated["done"] = True
            runner._invalidate_session_run_generation(session_key, reason="test_stop")
        return result

    adapter.send = send_and_invalidate

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-commentary-stop",
        session_key=session_key,
        run_generation=1,
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert result["final_response"] == "done"
    assert "first interim" in sent_texts
    assert "second interim" not in sent_texts


@pytest.mark.asyncio
async def test_keep_typing_stops_immediately_when_interrupt_event_is_set():
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        adapter._keep_typing(
            "dm-typing-stop",
            interval=30.0,
            stop_event=stop_event,
        )
    )
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.5)

    normal_typing_calls = [
        call for call in adapter.typing if call.get("metadata") != {"stopped": True}
    ]
    stopped_calls = [
        call for call in adapter.typing if call.get("metadata") == {"stopped": True}
    ]
    assert len(normal_typing_calls) == 1
    assert len(stopped_calls) == 1


@pytest.mark.asyncio
async def test_verbose_mode_does_not_truncate_args_by_default(monkeypatch, tmp_path):
    """Verbose mode with default tool_preview_length (0) should NOT truncate args.

    Previously, verbose mode capped args at 200 chars when tool_preview_length
    was 0 (default).  The user explicitly opted into verbose — show full detail.
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        VerboseAgent,
        session_id="sess-verbose-no-truncate",
        config_data={"display": {"tool_progress": "verbose", "tool_preview_length": 0}},
    )

    assert result["final_response"] == "done"
    # The full 300-char 'x' string should be present, not truncated to 200
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    assert VerboseAgent.LONG_CODE in all_content


@pytest.mark.asyncio
async def test_verbose_mode_respects_explicit_tool_preview_length(monkeypatch, tmp_path):
    """When tool_preview_length is set to a positive value, verbose truncates to that."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        VerboseAgent,
        session_id="sess-verbose-explicit-cap",
        config_data={"display": {"tool_progress": "verbose", "tool_preview_length": 50}},
    )

    assert result["final_response"] == "done"
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    # Should be truncated — full 300-char string NOT present
    assert VerboseAgent.LONG_CODE not in all_content
    # But should still contain the truncated portion with "..."
    assert "..." in all_content

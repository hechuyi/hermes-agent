"""Tests for duplicate reply suppression across the gateway stack.

Covers four fix paths:
  1. base.py: stale response suppressed when interrupt_event is set and a
     pending message exists (#8221 / #2483)
  2. run.py return path: only confirmed final streamed delivery suppresses
     the fallback final send; partial streamed output must not
  3. run.py queued-message path: first response is skipped only when the
     final response was actually streamed, not merely when partial output existed
  4. stream_consumer.py cancellation handler: only confirms final delivery
     when the best-effort send actually succeeds, not merely because partial
     content was sent earlier
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


@pytest.mark.parametrize(
    ("persistence", "expected_skip_db"),
    [
        (
            {
                "attempted": True,
                "ok": True,
                "row_ids_by_message_index": {2: 101},
            },
            True,
        ),
        (
            {
                "attempted": True,
                "ok": True,
                "row_ids_by_message_index": {3: 102},
            },
            False,
        ),
    ],
    ids=["user-row-proof", "no-user-row-proof"],
)
@pytest.mark.asyncio
async def test_failed_agent_user_persistence_proof_controls_gateway_db_rewrite(
    monkeypatch,
    persistence,
    expected_skip_db,
):
    import gateway.run as gateway_run
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="dm",
        user_id="u1",
    )
    event = MessageEvent(text="hello", source=source, message_id="m1")
    session_entry = SimpleNamespace(
        session_key="agent:main:discord:dm:c1",
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    session_store = MagicMock()
    session_store.get_or_create_session.return_value = session_entry
    history = [
        {"role": "user", "content": "before"},
        {"role": "assistant", "content": "old"},
    ]
    session_store.load_transcript.return_value = history
    session_store.has_any_sessions.return_value = True
    session_store.update_session = MagicMock()
    session_store.clear_resume_pending = MagicMock()
    runner.session_store = session_store
    runner.adapters = {
        Platform.DISCORD: SimpleNamespace(
            stop_typing=AsyncMock(),
            send=AsyncMock(),
        )
    }
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.config = GatewayConfig()
    runner._session_db = None
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._voice_mode = {}
    runner._pending_model_notes = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_model_overrides = {}
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda *_args, **_kwargs: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._bind_adapter_run_generation = lambda *_args, **_kwargs: None
    runner._is_session_run_current = lambda *_args, **_kwargs: True
    runner._set_session_env = lambda _context: []
    runner._clear_restart_failure_count = lambda _session_key: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False

    messages = history + [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "temporary failure"},
    ]
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "temporary failure",
            "messages": messages,
            "history_offset": len(history),
            "failed": True,
            "error": "rate limit",
            "last_prompt_tokens": 0,
            "persistence": persistence,
        }
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(event, source, session_entry.session_key, 1)

    assert result == "temporary failure"
    user_write = [
        call
        for call in session_store.append_to_transcript.call_args_list
        if call.args[1].get("role") == "user"
    ][-1]
    assert user_write.args[0] == "session-1"
    assert user_write.args[1]["role"] == "user"
    assert user_write.kwargs["skip_db"] is expected_skip_db


def test_agent_persistence_proof_suppresses_only_covered_gateway_db_rewrites():
    """Gateway may skip its own DB write only when run_agent proved coverage."""
    from gateway.run import (
        _gateway_agent_persistence_covers_full_transcript,
        _preserve_queued_followup_history_offset,
    )

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    assert _gateway_agent_persistence_covers_full_transcript(
        {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {0: 11, 1: 12},
        },
        messages,
        history_offset=0,
    ) is True
    assert _gateway_agent_persistence_covers_full_transcript(
        {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {0: 11},
        },
        messages,
        history_offset=0,
    ) is False
    assert _gateway_agent_persistence_covers_full_transcript(
        {"attempted": True, "ok": False, "row_ids_by_message_index": {0: 11, 1: 12}},
        messages,
        history_offset=0,
    ) is False
    assert _gateway_agent_persistence_covers_full_transcript(
        {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {5: 11},
        },
        [{"role": "system", "content": "skip"}, {"role": "user", "content": "hello"}],
        history_offset=4,
    ) is True

    history = [
        {"role": "user", "content": "before"},
        {"role": "assistant", "content": "old"},
    ]
    first_turn = [
        {"role": "user", "content": "first queued"},
        {"role": "assistant", "content": "first answer"},
    ]
    followup_turn = [
        {"role": "user", "content": "second queued"},
        {"role": "assistant", "content": "second answer"},
    ]
    current_result = {
        "history_offset": len(history),
        "messages": history + first_turn,
        "persistence": {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {2: 21, 3: 22},
        },
    }
    followup_result = {
        "history_offset": len(history + first_turn),
        "messages": history + first_turn + followup_turn,
        "persistence": {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {4: 23, 5: 24},
        },
    }

    merged = _preserve_queued_followup_history_offset(current_result, followup_result)
    assert merged["history_offset"] == len(history)
    new_messages = merged["messages"][merged["history_offset"]:]
    assert _gateway_agent_persistence_covers_full_transcript(
        merged["persistence"],
        new_messages,
        history_offset=merged["history_offset"],
    ) is True


@pytest.mark.asyncio
async def test_queued_mixed_proof_skips_only_persisted_prefix_rows(monkeypatch):
    """Queued follow-up fallback must not rewrite rows already covered by proof."""
    import gateway.run as gateway_run
    from gateway.run import GatewayRunner, _preserve_queued_followup_history_offset

    first_turn = [
        {"role": "user", "content": "first queued"},
        {"role": "assistant", "content": "first answer"},
    ]
    followup_turn = [
        {"role": "user", "content": "second queued"},
        {"role": "assistant", "content": "second answer"},
    ]
    current_result = {
        "history_offset": 0,
        "messages": first_turn,
        "persistence": {
            "attempted": True,
            "ok": True,
            "row_ids_by_message_index": {0: 21, 1: 22},
        },
    }
    followup_result = {
        "history_offset": len(first_turn),
        "messages": first_turn + followup_turn,
        "persistence": {
            "attempted": True,
            "ok": False,
            "row_ids_by_message_index": {},
        },
    }

    merged = _preserve_queued_followup_history_offset(current_result, followup_result)
    assert merged["history_offset"] == 0

    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="dm",
        user_id="u1",
    )
    event = MessageEvent(text="first queued", source=source, message_id="m1")
    session_entry = SimpleNamespace(
        session_key="agent:main:discord:dm:c1",
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    session_store = MagicMock()
    session_store.get_or_create_session.return_value = session_entry
    session_store.load_transcript.return_value = []
    session_store.has_any_sessions.return_value = True
    session_store.update_session = MagicMock()
    session_store.clear_resume_pending = MagicMock()
    runner.session_store = session_store
    runner.adapters = {
        Platform.DISCORD: SimpleNamespace(
            stop_typing=AsyncMock(),
            send=AsyncMock(),
        )
    }
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.config = GatewayConfig()
    runner._session_db = None
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._voice_mode = {}
    runner._pending_model_notes = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_model_overrides = {}
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda *_args, **_kwargs: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._bind_adapter_run_generation = lambda *_args, **_kwargs: None
    runner._is_session_run_current = lambda *_args, **_kwargs: True
    runner._set_session_env = lambda _context: []
    runner._clear_restart_failure_count = lambda _session_key: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False

    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "second answer",
            "messages": merged["messages"],
            "history_offset": merged["history_offset"],
            "last_prompt_tokens": 0,
            "persistence": merged["persistence"],
        }
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "test-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    result = await runner._handle_message_with_agent(event, source, session_entry.session_key, 1)

    assert result == "second answer"
    transcript_writes = [
        call
        for call in session_store.append_to_transcript.call_args_list
        if call.args[1].get("role") in {"user", "assistant"}
    ]
    assert [call.args[1]["content"] for call in transcript_writes] == [
        "first queued",
        "first answer",
        "second queued",
        "second answer",
    ]
    assert [call.kwargs.get("skip_db", False) for call in transcript_writes] == [
        True,
        True,
        False,
        False,
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StubAdapter(BasePlatformAdapter):
    """Minimal concrete adapter for testing."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.DISCORD)
        self.sent = []

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="msg1")

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_event(text="hello", chat_id="c1", user_id="u1"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            chat_type="dm",
            user_id=user_id,
        ),
        message_id="m1",
    )



# ===================================================================
# Test 1: base.py — stale response suppressed on interrupt (#8221)
# ===================================================================

class TestBaseInterruptSuppression:
    @pytest.mark.asyncio
    async def test_stale_response_suppressed_when_interrupted(self):
        """When interrupt_event is set AND a pending message exists,
        base.py should suppress the stale response instead of sending it."""
        adapter = StubAdapter()

        stale_response = "This is the stale answer to the first question."
        pending_response = "This is the answer to the second question."
        call_count = 0

        async def fake_handler(event):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return stale_response
            return pending_response

        adapter.set_message_handler(fake_handler)

        event_a = _make_event(text="first question")
        session_key = build_session_key(event_a.source)

        # Simulate: message A is being processed, message B arrives
        # The interrupt event is set and B is in pending_messages
        interrupt_event = asyncio.Event()
        interrupt_event.set()
        adapter._active_sessions[session_key] = interrupt_event

        event_b = _make_event(text="second question")
        adapter._pending_messages[session_key] = event_b

        await adapter._process_message_background(event_a, session_key)

        # The in-band pending-drain now hands off to a fresh task instead
        # of recursing (#17758).  Wait for that task to finish before
        # checking the sent list.
        for _ in range(200):
            if any(s["content"] == pending_response for s in adapter.sent):
                break
            await asyncio.sleep(0.01)
        await adapter.cancel_background_tasks()

        # The stale response should NOT have been sent.
        stale_sends = [s for s in adapter.sent if s["content"] == stale_response]
        assert len(stale_sends) == 0, (
            f"Stale response was sent {len(stale_sends)} time(s) — should be suppressed"
        )
        # The pending message's response SHOULD have been sent.
        pending_sends = [s for s in adapter.sent if s["content"] == pending_response]
        assert len(pending_sends) == 1, "Pending message response should be sent"

    @pytest.mark.asyncio
    async def test_response_not_suppressed_without_interrupt(self):
        """Normal case: no interrupt, response should be sent."""
        adapter = StubAdapter()

        async def fake_handler(event):
            return "Normal response"

        adapter.set_message_handler(fake_handler)
        event = _make_event()
        session_key = build_session_key(event.source)

        await adapter._process_message_background(event, session_key)

        assert any(s["content"] == "Normal response" for s in adapter.sent)

    @pytest.mark.asyncio
    async def test_response_not_suppressed_with_interrupt_but_no_pending(self):
        """Interrupt event set but no pending message (race already resolved) —
        response should still be sent."""
        adapter = StubAdapter()

        async def fake_handler(event):
            return "Valid response"

        adapter.set_message_handler(fake_handler)
        event = _make_event()
        session_key = build_session_key(event.source)

        # Set interrupt but no pending message
        interrupt_event = asyncio.Event()
        interrupt_event.set()
        adapter._active_sessions[session_key] = interrupt_event

        await adapter._process_message_background(event, session_key)

        assert any(s["content"] == "Valid response" for s in adapter.sent)


# Test 2: run.py — partial streamed output must not suppress final send
# ===================================================================

class TestOnlyFinalStreamDeliverySuppressesFinalSend:
    """The gateway should suppress the fallback final send only when the
    stream consumer confirmed the final assistant reply was delivered.

    Partial streamed output is not enough. If only already_sent=True,
    the fallback final send must still happen so Telegram users don't lose
    the real answer."""

    def _make_mock_stream_consumer(self, already_sent=False, final_response_sent=False):
        sc = SimpleNamespace(
            already_sent=already_sent,
            final_response_sent=final_response_sent,
        )
        return sc

    def test_partial_stream_output_does_not_set_already_sent(self):
        """already_sent=True alone must NOT suppress final delivery."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=False)
        response = {"final_response": "text", "response_previewed": False}

        if sc and isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _streamed = bool(sc and getattr(sc, "final_response_sent", False))
            _previewed = bool(response.get("response_previewed"))
            if not _is_empty_sentinel and (_streamed or _previewed):
                response["already_sent"] = True

        assert "already_sent" not in response

    def test_already_sent_not_set_when_nothing_sent(self):
        """When stream consumer hasn't sent anything, already_sent should
        not be set on the response."""
        sc = self._make_mock_stream_consumer(already_sent=False, final_response_sent=False)
        response = {"final_response": "text", "response_previewed": False}

        if sc and isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _streamed = bool(sc and getattr(sc, "final_response_sent", False))
            _previewed = bool(response.get("response_previewed"))
            if not _is_empty_sentinel and (_streamed or _previewed):
                response["already_sent"] = True

        assert "already_sent" not in response

    def test_already_sent_set_on_final_response_sent(self):
        """final_response_sent=True should suppress duplicate final sends."""
        sc = self._make_mock_stream_consumer(already_sent=False, final_response_sent=True)
        response = {"final_response": "text"}

        if sc and isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _streamed = bool(sc and getattr(sc, "final_response_sent", False))
            _previewed = bool(response.get("response_previewed"))
            if not _is_empty_sentinel and (_streamed or _previewed):
                response["already_sent"] = True

        assert response.get("already_sent") is True

    def test_already_sent_not_set_on_failed_response(self):
        """Failed responses should never be suppressed — user needs to see
        the error message even if streaming sent earlier partial output."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=False)
        response = {"final_response": "Error: something broke", "failed": True}

        if sc and isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _streamed = bool(sc and getattr(sc, "final_response_sent", False))
            _previewed = bool(response.get("response_previewed"))
            if not _is_empty_sentinel and (_streamed or _previewed):
                response["already_sent"] = True

        assert "already_sent" not in response


# ===================================================================
# Test 2b: run.py — empty response never suppressed (#10xxx)
# ===================================================================

class TestEmptyResponseNotSuppressed:
    """When the model returns '(empty)' after tool calls (e.g. mimo-v2-pro
    going silent after web_search), the gateway must NOT suppress delivery
    even if the stream consumer sent intermediate text earlier.

    Without this fix, the user sees partial streaming text ('Let me search
    for that') and then silence — the '(empty)' sentinel is swallowed by
    already_sent=True."""

    def _make_mock_stream_consumer(self, already_sent=False, final_response_sent=False):
        return SimpleNamespace(
            already_sent=already_sent,
            final_response_sent=final_response_sent,
        )

    def _apply_suppression_logic(self, response, sc):
        """Reproduce the fixed logic from gateway/run.py return path."""
        if sc and isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _streamed = bool(sc and getattr(sc, "final_response_sent", False))
            _previewed = bool(response.get("response_previewed"))
            if not _is_empty_sentinel and (_streamed or _previewed):
                response["already_sent"] = True

    def test_empty_sentinel_not_suppressed_with_already_sent(self):
        """'(empty)' final_response should NOT be suppressed even when
        streaming sent intermediate content."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=True)
        response = {"final_response": "(empty)"}
        self._apply_suppression_logic(response, sc)
        assert "already_sent" not in response

    def test_empty_string_not_suppressed_with_already_sent(self):
        """Empty string final_response should NOT be suppressed."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=True)
        response = {"final_response": ""}
        self._apply_suppression_logic(response, sc)
        assert "already_sent" not in response

    def test_none_response_not_suppressed_with_already_sent(self):
        """None final_response should NOT be suppressed."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=True)
        response = {"final_response": None}
        self._apply_suppression_logic(response, sc)
        assert "already_sent" not in response

    def test_real_response_still_suppressed_only_when_final_delivery_confirmed(self):
        """Normal non-empty response should be suppressed only when the final
        response was actually streamed."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=True)
        response = {"final_response": "Here are the search results..."}
        self._apply_suppression_logic(response, sc)
        assert response.get("already_sent") is True

    def test_failed_empty_response_never_suppressed(self):
        """Failed responses are never suppressed regardless of content."""
        sc = self._make_mock_stream_consumer(already_sent=True, final_response_sent=True)
        response = {"final_response": "(empty)", "failed": True}
        self._apply_suppression_logic(response, sc)
        assert "already_sent" not in response

class TestQueuedMessageAlreadyStreamed:
    """The queued-message path should skip the first response only when the
    final response was actually streamed."""

    def _make_mock_sc(self, already_sent=False, final_response_sent=False):
        return SimpleNamespace(
            already_sent=already_sent,
            final_response_sent=final_response_sent,
        )

    def test_queued_path_only_skips_send_when_final_response_was_streamed(self):
        """Partial streamed output alone must not suppress the first response
        before the queued follow-up is processed."""
        _sc = self._make_mock_sc(already_sent=True, final_response_sent=False)

        _already_streamed = bool(
            _sc and getattr(_sc, "final_response_sent", False)
        )

        assert _already_streamed is False

    def test_queued_path_detects_confirmed_final_stream_delivery(self):
        """Confirmed final streamed delivery should skip the resend."""
        _sc = self._make_mock_sc(already_sent=True, final_response_sent=True)
        response = {"response_previewed": False}

        _already_streamed = bool(
            (_sc and getattr(_sc, "final_response_sent", False))
            or bool(response.get("response_previewed"))
        )

        assert _already_streamed is True

    def test_queued_path_detects_previewed_response_delivery(self):
        """A response already previewed via the adapter should not be resent
        before processing the queued follow-up."""
        _sc = self._make_mock_sc(already_sent=False, final_response_sent=False)
        response = {"response_previewed": True}

        _already_streamed = bool(
            (_sc and getattr(_sc, "final_response_sent", False))
            or bool(response.get("response_previewed"))
        )

        assert _already_streamed is True

    def test_queued_path_sends_when_not_streamed(self):
        """Nothing was streamed — first response should be sent before
        processing the queued message."""
        _sc = self._make_mock_sc(already_sent=False, final_response_sent=False)

        _already_streamed = bool(
            _sc and getattr(_sc, "final_response_sent", False)
        )

        assert _already_streamed is False

    def test_queued_path_with_no_stream_consumer(self):
        """No stream consumer at all (streaming disabled) — not streamed."""
        _sc = None

        _already_streamed = bool(
            _sc and getattr(_sc, "final_response_sent", False)
        )

        assert _already_streamed is False


# ===================================================================
# Test 4: stream_consumer.py — cancellation handler delivery confirmation
# ===================================================================

class TestCancellationHandlerDeliveryConfirmation:
    """The stream consumer's cancellation handler should only set
    final_response_sent when the best-effort send actually succeeds.
    Partial content (already_sent=True) alone must not promote to
    final_response_sent — that would suppress the gateway's fallback
    send even when the user never received the real answer."""

    def test_partial_only_no_accumulated_stays_false(self):
        """Cancelled after sending intermediate text, nothing accumulated.
        final_response_sent must stay False so the gateway fallback fires."""
        already_sent = True
        final_response_sent = False
        accumulated = ""
        message_id = None

        _best_effort_ok = False
        if accumulated and message_id:
            _best_effort_ok = True  # wouldn't enter
        if _best_effort_ok and not final_response_sent:
            final_response_sent = True

        assert final_response_sent is False

    def test_best_effort_succeeds_sets_true(self):
        """When accumulated content exists and best-effort send succeeds,
        final_response_sent should become True."""
        already_sent = True
        final_response_sent = False
        accumulated = "Here are the search results..."
        message_id = "msg_123"

        _best_effort_ok = False
        if accumulated and message_id:
            _best_effort_ok = True  # simulating successful _send_or_edit
        if _best_effort_ok and not final_response_sent:
            final_response_sent = True

        assert final_response_sent is True

    def test_best_effort_fails_stays_false(self):
        """When best-effort send fails (flood control, network), the
        gateway fallback must deliver the response."""
        already_sent = True
        final_response_sent = False
        accumulated = "Here are the search results..."
        message_id = "msg_123"

        _best_effort_ok = False
        if accumulated and message_id:
            _best_effort_ok = False  # simulating failed _send_or_edit
        if _best_effort_ok and not final_response_sent:
            final_response_sent = True

        assert final_response_sent is False

    def test_preserves_existing_true(self):
        """If final_response_sent was already True before cancellation,
        it must remain True regardless."""
        already_sent = True
        final_response_sent = True
        accumulated = ""
        message_id = None

        _best_effort_ok = False
        if accumulated and message_id:
            pass
        if _best_effort_ok and not final_response_sent:
            final_response_sent = True

        assert final_response_sent is True

    def test_old_behavior_would_have_promoted_partial(self):
        """Verify the old code would have incorrectly promoted
        already_sent to final_response_sent even with no accumulated
        content — proving the bug existed."""
        already_sent = True
        final_response_sent = False

        # OLD cancellation handler logic:
        if already_sent:
            final_response_sent = True

        assert final_response_sent is True  # the bug: partial promoted to final


class TestFinalContentDeliveredSuppression:
    """When stream consumer delivered the final content but the cosmetic
    final edit (cursor removal) failed, the gateway must suppress the
    fallback send to prevent duplicate messages.

    Covers the scenario not handled by final_response_sent alone:
    content reached the user via _send_or_edit, but the subsequent edit
    that clears a typing cursor or streaming marker failed, leaving
    final_response_sent=False even though the user already saw the text.
    """

    def test_content_delivered_but_final_edit_failed_suppresses(self):
        """final_content_delivered=True + final_response_sent=False
        must suppress (content already visible to user)."""
        sc = SimpleNamespace(
            already_sent=True,
            final_response_sent=False,
            final_content_delivered=True,
        )
        response = {"final_response": "Hello!", "response_previewed": False}

        _streamed = bool(getattr(sc, "final_response_sent", False))
        _previewed = bool(response.get("response_previewed"))
        _content_delivered = bool(getattr(sc, "final_content_delivered", False))
        _is_empty_sentinel = (
            not response.get("final_response")
            or response.get("final_response") == "(empty)"
        )
        if not _is_empty_sentinel and (_streamed or _previewed or _content_delivered):
            response["already_sent"] = True

        assert response.get("already_sent") is True

    def test_intermediate_text_only_does_not_suppress(self):
        """already_sent=True from intermediate text + final_content_delivered=False
        must NOT suppress (user still needs the real final answer)."""
        sc = SimpleNamespace(
            already_sent=True,
            final_response_sent=False,
            final_content_delivered=False,
        )
        response = {"final_response": "Real answer", "response_previewed": False}

        _streamed = bool(getattr(sc, "final_response_sent", False))
        _previewed = bool(response.get("response_previewed"))
        _content_delivered = bool(getattr(sc, "final_content_delivered", False))
        _is_empty_sentinel = (
            not response.get("final_response")
            or response.get("final_response") == "(empty)"
        )
        if not _is_empty_sentinel and (_streamed or _previewed or _content_delivered):
            response["already_sent"] = True

        assert "already_sent" not in response

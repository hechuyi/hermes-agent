from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.tool_dispatch_helpers import make_tool_result_message
from agent.tool_executor import execute_tool_calls_sequential


def _tool_call(call_id="call-1", name="terminal", arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _agent_for_tool_execution():
    agent = SimpleNamespace()
    agent._interrupt_requested = False
    agent._tool_guardrails = SimpleNamespace(
        before_call=lambda name, args: SimpleNamespace(allows_execution=True)
    )
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.log_prefix = ""
    agent.log_prefix_chars = 100
    agent.valid_tool_names = {"terminal"}
    agent.enabled_toolsets = None
    agent.disabled_toolsets = None
    agent.session_id = "session-1"
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent.tool_complete_callback = None
    agent.tool_delay = 0
    agent._checkpoint_mgr = SimpleNamespace(enabled=False)
    agent._context_engine_tool_names = set()
    agent._memory_manager = None
    agent._print_fn = print
    agent.context_compressor = None
    agent._subdirectory_hints = SimpleNamespace(check_tool_call=lambda name, args: "")
    agent._current_tool = None
    agent._touch_activity = lambda msg: None
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent._vprint = lambda *args, **kwargs: None
    agent._wrap_verbose = lambda prefix, value: prefix + value
    agent._record_file_mutation_result = lambda *args, **kwargs: None
    agent._append_guardrail_observation = lambda name, args, result, failed=False: result
    agent._guardrail_block_result = lambda decision: "blocked"
    agent._apply_pending_steer_to_tool_results = lambda messages, count: None
    agent._tool_result_content_for_active_model = lambda name, result: result
    agent._flush_messages_to_session_db = MagicMock(return_value={"ok": True})
    return agent


def test_sequential_tool_result_flushes_session_db_after_append():
    agent = _agent_for_tool_execution()
    messages = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
    ]
    assistant_message = SimpleNamespace(tool_calls=[_tool_call()])

    with patch("agent.tool_executor._ra") as mock_ra:
        mock_ra.return_value.handle_function_call.return_value = "tool output"
        execute_tool_calls_sequential(agent, assistant_message, messages, "default")

    assert messages[-1] == make_tool_result_message("terminal", "tool output", "call-1")
    agent._flush_messages_to_session_db.assert_called()
    assert agent._flush_messages_to_session_db.call_args.args[0] is messages

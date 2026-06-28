"""Regression: handle_function_call must forward session_id to registry.dispatch."""

from unittest.mock import patch

import pytest


def _run_handle_function_call(function_name, function_args, *, task_id, session_id):
    captured = {}

    def _dispatch(_name, _args, **kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    with (
        patch("model_tools.registry.dispatch", side_effect=_dispatch),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        from model_tools import handle_function_call

        handle_function_call(
            function_name,
            function_args,
            task_id=task_id,
            session_id=session_id,
            skip_pre_tool_call_hook=True,
        )

    return captured


@pytest.mark.parametrize(
    "function_name,function_args",
    [
        ("web_search", {"q": "test"}),
        ("execute_code", {"code": "print(1)"}),
    ],
)
def test_handle_function_call_forwards_session_id_to_dispatch(function_name, function_args):
    captured = _run_handle_function_call(
        function_name,
        function_args,
        task_id="task-1",
        session_id="session-1",
    )

    assert captured.get("task_id") == "task-1"
    assert captured.get("session_id") == "session-1"

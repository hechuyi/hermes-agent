"""Tests for delegate_task spinner labelling."""

from agent.tool_executor import _delegate_spinner_label


def test_delegate_spinner_label_for_task_list_points_to_agents_dashboard():
    label = _delegate_spinner_label({"tasks": [{"goal": "a"}, {"goal": "b"}]})

    assert "delegating 2 tasks" in label
    assert "/agents to monitor" in label


def test_delegate_spinner_label_for_single_goal_points_to_agents_dashboard():
    label = _delegate_spinner_label({"goal": "inspect failing tests"})

    assert "inspect failing tests" in label
    assert "/agents to monitor" in label

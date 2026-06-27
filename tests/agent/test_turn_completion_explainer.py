import os
from unittest.mock import patch

from run_agent import AIAgent


def test_turn_completion_explainer_stays_quiet_for_normal_text_response():
    assert (
        AIAgent._format_turn_completion_explanation("text_response(finish_reason=stop)")
        == ""
    )


def test_turn_completion_explainer_explains_empty_response_exhaustion():
    explanation = AIAgent._format_turn_completion_explanation(
        "empty_response_exhausted"
    )

    assert explanation
    assert "empty content" in explanation
    assert "continue" in explanation.lower()


def test_turn_completion_explainer_explains_partial_stream_recovery():
    explanation = AIAgent._format_turn_completion_explanation(
        "partial_stream_recovery"
    )

    assert "partial" in explanation.lower()
    assert "continue" in explanation.lower()


def test_turn_completion_explainer_explains_max_iterations_prefix():
    explanation = AIAgent._format_turn_completion_explanation(
        "max_iterations_reached(90/90)"
    )

    assert "iteration" in explanation.lower()
    assert "continue" in explanation.lower()


def test_turn_completion_explainer_unknown_reason_is_silent():
    assert AIAgent._format_turn_completion_explanation("") == ""
    assert AIAgent._format_turn_completion_explanation("unknown") == ""
    assert AIAgent._format_turn_completion_explanation("guardrail_halt") == ""


def test_turn_completion_explainer_env_override_disables_default():
    agent = object.__new__(AIAgent)

    with patch.dict(
        os.environ, {"HERMES_TURN_COMPLETION_EXPLAINER": "off"}, clear=False
    ):
        assert agent._turn_completion_explainer_enabled() is False


def test_turn_completion_explainer_config_can_disable_default():
    agent = object.__new__(AIAgent)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TURN_COMPLETION_EXPLAINER", None)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"display": {"turn_completion_explainer": False}},
        ):
            assert agent._turn_completion_explainer_enabled() is False

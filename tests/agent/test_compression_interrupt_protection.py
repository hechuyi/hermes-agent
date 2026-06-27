"""Regression coverage for atomic compression summary calls."""

from unittest.mock import patch

import agent.auxiliary_client as aux


class TestAuxInterruptProtection:
    def test_protected_flag_defaults_false(self):
        assert aux._aux_interrupt_protected() is False

    def test_context_manager_sets_and_restores(self):
        with aux.aux_interrupt_protection():
            assert aux._aux_interrupt_protected() is True
        assert aux._aux_interrupt_protected() is False

    def test_context_manager_is_reentrant(self):
        with aux.aux_interrupt_protection():
            assert aux._aux_interrupt_protected() is True
            with aux.aux_interrupt_protection():
                assert aux._aux_interrupt_protected() is True
            assert aux._aux_interrupt_protected() is True
        assert aux._aux_interrupt_protected() is False

    def test_restores_on_exception(self):
        try:
            with aux.aux_interrupt_protection():
                raise ValueError("boom")
        except ValueError:
            pass
        assert aux._aux_interrupt_protected() is False


class TestCompressionProtectsSummaryCall:
    def test_compressor_call_site_uses_aux_interrupt_protection(self):
        from agent.context_compressor import ContextCompressor

        seen = {}

        class _Resp:
            class _Choice:
                class _Msg:
                    content = "summary ok"

                message = _Msg()

            choices = [_Choice()]

        def fake_call_llm(**kwargs):
            seen["protected"] = aux._aux_interrupt_protected()
            seen["task"] = kwargs.get("task")
            return _Resp()

        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            c = ContextCompressor(model="test", quiet_mode=True)

        msgs = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "working"},
        ]

        with patch("agent.context_compressor.call_llm", side_effect=fake_call_llm):
            summary = c._generate_summary(msgs)

        assert summary is not None
        assert seen == {"protected": True, "task": "compression"}
        assert aux._aux_interrupt_protected() is False

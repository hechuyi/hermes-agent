from types import SimpleNamespace

from agent.conversation_compression import compress_context


class _FakeCompressor:
    compression_count = 2
    _last_compress_aborted = False
    _last_summary_error = None
    _last_aux_model_failure_model = None
    _last_aux_model_failure_error = None
    last_prompt_tokens = 0
    last_completion_tokens = 0
    awaiting_real_usage_after_compression = False

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
        return [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "tail"},
        ]


def _agent():
    statuses = []
    vprints = []
    return SimpleNamespace(
        compression_enabled=True,
        _compression_feasibility_checked=True,
        session_id="s1",
        log_prefix="",
        model="m",
        platform="gateway",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        api_mode="chat_completions",
        tools=[],
        context_compressor=_FakeCompressor(),
        _session_db=None,
        _memory_manager=None,
        _todo_store=SimpleNamespace(format_for_injection=lambda: ""),
        _invalidate_system_prompt=lambda: None,
        _build_system_prompt=lambda system_message: system_message,
        _cached_system_prompt="system",
        _gateway_session_key=None,
        _emit_status=lambda message: statuses.append(message),
        _emit_warning=lambda message: statuses.append(message),
        _vprint=lambda message, force=False: vprints.append(message),
        _custom_providers={},
        statuses=statuses,
        vprints=vprints,
    )


def test_repeated_compression_warning_uses_status_and_is_deduped():
    agent = _agent()
    messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]

    compress_context(agent, messages, "system", approx_tokens=90_000)
    compress_context(agent, messages, "system", approx_tokens=90_000)

    repeated = [
        msg for msg in agent.statuses
        if "Session compressed 2 times" in msg
    ]
    assert len(repeated) == 1
    assert not any("Session compressed 2 times" in msg for msg in agent.vprints)

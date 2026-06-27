from types import SimpleNamespace

from agent.chat_completion_helpers import handle_max_iterations


class _Transport:
    @staticmethod
    def normalize_response(response):
        return SimpleNamespace(content=response.choices[0].message.content)


class _ClientFactory:
    def __init__(self, response):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: self._create(**kwargs))
        )
        self.response = response
        self.kwargs = None

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class _Agent:
    max_iterations = 60
    model = "test-model"
    provider = "openai"
    base_url = "https://api.openai.com/v1"
    _base_url_lower = base_url.lower()
    _cached_system_prompt = "You are helpful."
    ephemeral_system_prompt = ""
    prefill_messages = []
    api_mode = "chat_completions"
    max_tokens = None
    reasoning_config = None
    providers_allowed = []
    providers_ignored = []
    providers_order = []
    provider_sort = None
    openrouter_min_coding_score = None

    def __init__(self, response):
        self.client_factory = _ClientFactory(response)

    @staticmethod
    def _should_sanitize_tool_calls():
        return False

    @staticmethod
    def _copy_reasoning_content_for_api(_source, _target):
        return None

    @staticmethod
    def _sanitize_tool_calls_for_strict_api(_msg, model=None):
        return None

    @staticmethod
    def _sanitize_api_messages(messages):
        return messages

    @staticmethod
    def _drop_thinking_only_and_merge_users(messages):
        return messages

    @staticmethod
    def _supports_reasoning_extra_body():
        return False

    @staticmethod
    def _is_openrouter_url():
        return False

    @staticmethod
    def _max_tokens_param(max_tokens):
        return {"max_tokens": max_tokens}

    def _ensure_primary_openai_client(self, reason):
        assert reason == "iteration_limit_summary"
        return self.client_factory

    @staticmethod
    def _get_transport():
        return _Transport()


def _response(content):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def test_max_iterations_summary_strips_schema_foreign_message_keys():
    agent = _Agent(_response("Summary"))
    messages = [
        {"role": "user", "content": "do work"},
        {
            "role": "assistant",
            "content": "working",
            "finish_reason": "tool_calls",
            "reasoning": "private reasoning",
            "_thinking_prefill": True,
            "_empty_recovery_synthetic": True,
            "codex_reasoning_items": [{"id": "rs_1"}],
            "codex_message_items": [{"id": "msg_1"}],
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "tool_name": "terminal",
            "content": "ok",
            "_gateway_internal": "keep out of API payload",
        },
    ]

    assert handle_max_iterations(agent, messages, api_call_count=60) == "Summary"

    sent_messages = agent.client_factory.kwargs["messages"]
    for msg in sent_messages:
        assert "tool_name" not in msg
        assert "codex_reasoning_items" not in msg
        assert "codex_message_items" not in msg
        assert "finish_reason" not in msg
        assert "reasoning" not in msg
        assert not any(isinstance(key, str) and key.startswith("_") for key in msg)

    assert messages[1]["codex_reasoning_items"] == [{"id": "rs_1"}]
    assert messages[2]["tool_name"] == "terminal"

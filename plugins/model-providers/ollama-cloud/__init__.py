"""Ollama Cloud provider profile.

Ollama Cloud's OpenAI-compatible ``/v1/chat/completions`` endpoint supports
top-level ``reasoning_effort``. Hermes maps ``xhigh`` to ``max`` here so the
provider can expose the higher DeepSeek V4 thinking tier through the normal
reasoning config path.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class OllamaCloudProfile(ProviderProfile):
    """Ollama Cloud provider profile."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not isinstance(reasoning_config, dict):
            return extra_body, top_level

        if reasoning_config.get("enabled") is False:
            return extra_body, top_level

        effort = (reasoning_config.get("effort") or "").strip().lower()
        if not effort or effort == "none":
            return extra_body, top_level

        if effort in {"xhigh", "max"}:
            top_level["reasoning_effort"] = "max"
        elif effort in {"low", "medium", "high"}:
            top_level["reasoning_effort"] = effort
        else:
            top_level["reasoning_effort"] = effort

        return extra_body, top_level


ollama_cloud = OllamaCloudProfile(
    name="ollama-cloud",
    aliases=("ollama_cloud",),
    default_aux_model="nemotron-3-nano:30b",
    env_vars=("OLLAMA_API_KEY",),
    base_url="https://ollama.com/v1",
)

register_provider(ollama_cloud)

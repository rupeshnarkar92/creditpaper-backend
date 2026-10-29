from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    NVIDIA = "nvidia"
    OPENAI = "openai"
    GOOGLE = "google"
    COMPATIBLE = "compatible"


def current_provider() -> LLMProvider:
    """The active LLM provider, read fresh on every call.

    Read at call time, not module scope: a module-level capture would freeze
    the value at process start, so flipping LLM_PROVIDER mid-run would silently
    not take effect for an already-running job. Mirrors the pattern in
    `_resolve_effort` and the geo guard in client.py.

    An empty string or an unrecognised value falls back to ANTHROPIC. The
    intent is to make a typo in the env var degrade to the default behaviour
    rather than raise at first LLM call.
    """
    # Tenant settings are read by request entry-point scopes, not process globals.
    raw = os.getenv("LLM_PROVIDER", "").strip().lower()
    if raw == LLMProvider.OPENAI.value:
        return LLMProvider.OPENAI
    if raw == LLMProvider.GOOGLE.value:
        return LLMProvider.GOOGLE
    if raw == LLMProvider.COMPATIBLE.value:
        return LLMProvider.COMPATIBLE
    if raw == LLMProvider.NVIDIA.value:
        return LLMProvider.NVIDIA
    if raw == LLMProvider.OLLAMA.value:
        return LLMProvider.OLLAMA
    if raw == LLMProvider.OPENROUTER.value:
        return LLMProvider.OPENROUTER
    return LLMProvider.ANTHROPIC


@dataclass
class LLMResponse:
    """Normalised response shape returned by messages_create regardless of provider.

    Consumers read `.text` (the concatenated assistant text) and `.stop_reason`
    (mapped to Anthropic's vocabulary: end_turn, max_tokens,
    model_context_window_exceeded). `.input_tokens` / `.output_tokens` carry the
    call's usage, and `.raw` carries the underlying provider response for tests
    and debug.
    """

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    raw: object

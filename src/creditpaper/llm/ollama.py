"""Ollama native API chat adapter.

Uses the native /api/chat endpoint (not /v1/chat/completions) because
the OpenAI-compatible endpoint ignores `options.num_ctx` which we need
for vision models processing PDF page images.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_S = 600.0
_DEFAULT_MAX_CONCURRENCY = 1

_semaphore: threading.Semaphore | None = None
_sem_size = 0
_stage_timeout: ContextVar[float | None] = ContextVar("ollama_stage_timeout", default=None)


@contextmanager
def ollama_stage_timeout(stage: str):
    """Apply a bounded, independently configurable timeout to one model stage."""
    suffix = stage.replace("-", "_").upper()
    value = float(os.getenv(f"OLLAMA_{suffix}_TIMEOUT_S", os.getenv("OLLAMA_TIMEOUT_S", "600")))
    if not 1 <= value <= 1800:
        raise ValueError(f"OLLAMA_{suffix}_TIMEOUT_S must be between 1 and 1800 seconds")
    token = _stage_timeout.set(value)
    try:
        yield value
    finally:
        _stage_timeout.reset(token)


def _semaphore_size() -> int:
    global _semaphore, _sem_size
    if _semaphore is None:
        _sem_size = int(os.getenv("OLLAMA_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY)))
        if _sem_size < 1:
            raise ValueError(f"OLLAMA_MAX_CONCURRENCY must be >= 1, got {_sem_size}")
        _semaphore = threading.Semaphore(_sem_size)
    return _sem_size


def ollama_chat(
    messages: list[dict[str, Any]],
    *,
    model: str,
    timeout_s: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    json_mode: bool = False,
    _transport: Callable[..., Any] | None = None,
) -> dict:
    """POST to native /api/chat and return a response shaped like OpenAI.

    Uses the native Ollama API so that `options.num_ctx` is respected.
    Returns a dict with `choices[0].message.content` and `usage` keys
    so the caller (client.py) doesn't need to change.
    """
    base_url = os.getenv("OLLAMA_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    if urlsplit(base_url).scheme not in {"http", "https"}:
        raise ValueError("OLLAMA_BASE_URL must use HTTP or HTTPS")
    # Strip trailing /v1 if user set the old URL pattern
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    url = f"{base_url}/api/chat"
    timeout = timeout_s if timeout_s is not None else _stage_timeout.get()
    if timeout is None:
        timeout = float(os.getenv("OLLAMA_TIMEOUT_S", str(_DEFAULT_TIMEOUT_S)))

    options: dict[str, Any] = {
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "32768")),
    }
    if max_tokens is not None:
        options["num_predict"] = int(max_tokens)
    if temperature is not None:
        options["temperature"] = float(temperature)
    if top_p is not None:
        options["top_p"] = float(top_p)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        "options": options,
    }
    think = os.getenv("OLLAMA_THINK", "auto").strip().lower()
    if think == "auto":
        # gpt-oss exposes reasoning levels; Qwen's API uses a boolean and its
        # default thinking mode can consume the entire extraction token budget.
        think = "low" if "gpt-oss" in model.lower() else "false"
    if think in {"low", "medium", "high"}:
        payload["think"] = think
    elif think in {"false", "0", "no", "off"}:
        payload["think"] = False
    if json_mode:
        payload["format"] = "json"

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    if _transport is None:
        _semaphore_size()
        assert _semaphore is not None
        with _semaphore:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # nosec B310 - scheme validated above
                raw = resp.read()
    else:
        _semaphore_size()
        assert _semaphore is not None
        with _semaphore:
            with _transport(request, timeout=timeout) as resp:
                raw = resp.read()

    native = json.loads(raw.decode("utf-8"))

    # Normalize to OpenAI-compatible shape so client.py doesn't change
    return {
        "choices": [
            {
                "message": native.get("message", {}),
                "finish_reason": "stop" if native.get("done") else "length",
            }
        ],
        "usage": {
            "prompt_tokens": native.get("prompt_eval_count", 0),
            "completion_tokens": native.get("eval_count", 0),
        },
    }

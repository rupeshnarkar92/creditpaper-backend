"""The single instrumented LLM call site — the choke point.

EVERY system LLM call routes through `messages_create`. A structural test
(`tests/test_llm_choke_point.py`) fails CI if any other module calls
`.messages.create(` or `.messages.stream(` directly. That test exists because a
bypass was found in production code that had been silently truncating house-style
playbooks feeding every section agent — no truncation guard, no ledger entry, and
nobody noticed.

What lives here and why it must stay together:
  - the truncation guard: `stop_reason == "max_tokens"` raises, and a distinct
    error for context overflow, because one needs a bigger cap and the other
    smaller input;
  - the streaming threshold, because the SDK refuses non-streaming requests it
    estimates will exceed ~10 minutes;
  - per-stage effort resolution and the inference-geo guard, which belong beside
    the call they modify: effort below a stage's floor stops the stage working
    rather than making it cheaper, and geo on a pre-4.6 model is a 400.

Usage is recorded to `creditpaper.obs.ledger`. Moved out of `obs/cost.py`
2026-08-21: transport is not observability, and every agent was importing its
transport from a module named for telemetry.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit

from creditpaper.llm.ollama import ollama_chat
from creditpaper.llm.pdf_render import render_pdf_pages
from creditpaper.llm.providers import LLMProvider, LLMResponse, current_provider
from creditpaper.obs.ledger import (
    UsageRecord,
    active_ledger,
    active_tenant_id,
    cost_usd,
    note_unscoped_call,
    prices_for,
)
from creditpaper.obs.timing import timed

# `inference_geo` 400s on pre-4.6 models. This is the known-incompatible set we
# have hit in practice, NOT an exhaustive parse of every model string that would
# fail — we deliberately don't try to extract version numbers from arbitrary
# model IDs (too easy to mis-parse a future naming scheme and either miss a real
# incompatibility or false-positive on a compatible one). Extend this set by name
# as new incompatible models are identified.
_GEO_INCOMPATIBLE_MODELS = frozenset(
    {
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    }
)


def _geo_incompatible(model: str) -> bool:
    """Prefix match, so a dated id is caught too.

    Exact membership missed `claude-haiku-4-5-20251001` — the pinned form of a
    model this set exists to protect — so the guard passed and the API returned
    a 400 instead. Same defect as the exact-match pricing lookup, in the same
    file, found at the same time; both are now anchored prefix tests.
    """
    return any(model.startswith(m) for m in _GEO_INCOMPATIBLE_MODELS)


# Models that answer a `temperature` with a 400 ("deprecated for this model"),
# measured on claude-sonnet-5 on 2026-10-08. Prefixes, so pinned dated forms match.
_TEMPERATURE_REJECTED = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")


def _rejects_temperature(model: str) -> bool:
    return any(model.startswith(m) for m in _TEMPERATURE_REJECTED)


# Accepted `output_config.effort` levels. On Opus 5 (and Sonnet 5) thinking is on
# by default and effort defaults to "high" server-side, which is what made
# generation cost roughly double and wall-clock roughly triple after the Opus 5
# migration; this is the dial that walks it back. Order is low->max so the
# ValueError below lists them in the order an operator would try them.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# The SDK refuses non-streaming requests it estimates will exceed ~10 minutes and
# raises on large max_tokens. At or above this cap we stream and take the
# accumulated final message, which is the same object a non-streaming call would
# return — so raising caps (the anti-truncation work) never trips the SDK's own
# guard. The largest prose call (agents/section.py) sets max_tokens exactly equal
# to this constant, so the comparison MUST include equality — a strict `>` would
# route that call, the one this constant exists to protect, onto the non-streaming
# path.
STREAM_AT_OR_ABOVE_MAX_TOKENS = 16_000

# -- shared client and rate governor -------------------------------------------
# A fresh `anthropic.Anthropic()` was constructed per call and never closed, so
# every call built its own connection pool and TLS handshake — with up to six
# ingest workers each shipping a full base64 PDF, and a nested two-wide pool
# inside valuation on top of that. One client, built once, reused.
#
# The SDK's own default timeout is generous and its retry count is small, and
# neither was set explicitly, so the behaviour under a 429 or a mid-stream
# disconnect was whatever the installed SDK version happened to do. Both are now
# named here, where they can be seen and tuned.
_DEFAULT_TIMEOUT_S = 600.0
_DEFAULT_MAX_RETRIES = 4
# Concurrency across the WHOLE process, not per pool. Ingest (6) can have
# valuation's nested pool (2) inside it, so the pools alone permit ~12 in flight
# and nothing bounded the total. The governor is the bound.
#
# 32, not 8. At 8 this was the binding constraint on every ingestion and it was
# invisible: a corpus pass with 24 worker threads and another with 96 both ran
# at ~5.9 windows/s, because only 8 calls were ever in flight. 8 x ~1.4s is
# 5.7 calls/s, which is exactly what both measured.
#
# Measured 2026-09-25 on 200 windows of blind_03:
#
#     governor  8   38s    5.3 windows/s
#     governor 48   10s   20.0 windows/s
#
# 48 worked but drew ten rate-limit responses that the SDK retried, and a retry
# is paid-for work thrown away. 32 keeps most of the gain with headroom under
# the provider's limit. `ANTHROPIC_MAX_CONCURRENCY` raises it per deployment,
# which is the right place to spend a higher tier.
_DEFAULT_MAX_CONCURRENCY = 32

_client_lock = threading.Lock()
_shared_client = None
_shared_openrouter_client = None
_shared_nvidia_client = None
_shared_http_client = None
_governor: threading.Semaphore | None = None
_governor_size = 0
_ACTIVE_PROVIDER_CONFIG: ContextVar[Any | None] = ContextVar(
    "creditpaper_active_provider_config", default=None
)
_ACTIVE_PROVIDER: ContextVar[LLMProvider | None] = ContextVar("creditpaper_active_provider", default=None)
_REQUEST_PROVIDER: ContextVar[LLMProvider | None] = ContextVar("creditpaper_request_provider", default=None)
_REQUEST_MODEL: ContextVar[str | None] = ContextVar("creditpaper_request_model", default=None)
# The generation provider policy (DECISIONS 2026-10-07), enforced HERE rather
# than by narrowing scopes at the call site: a nested `provider_session` re-binds
# the tenant config, so a policy applied only by the caller fails open. While it
# is active, every call runs on Anthropic; a tenant's own Anthropic key is used
# but its single configured model does not replace the stage model, a tenant's
# key for any other provider is not used at all, and a request-scoped model
# override is ignored. Pinned stage models are priced from the platform table,
# not the tenant's per-model price.
_GENERATION_POLICY: ContextVar[bool] = ContextVar("creditpaper_generation_policy", default=False)


@contextmanager
def generation_policy() -> Iterator[None]:
    token = _GENERATION_POLICY.set(True)
    try:
        yield
    finally:
        _GENERATION_POLICY.reset(token)


def _pricing_config(model: str) -> Any | None:
    """The tenant config whose prices apply to `model`, or None for the platform table."""
    config = active_provider_config()
    if config is not None and _GENERATION_POLICY.get() and model != config.model:
        return None
    return config


@contextmanager
def provider_model_scope(provider: LLMProvider | str, model: str) -> Iterator[None]:
    """Route this task and context-propagating child calls to one registry choice.

    Tenant BYOK remains authoritative in ``messages_create``; this scope only
    supplies task routing when no tenant provider config is active. The
    parallel extraction/corpus pipelines use this around their worker pools so
    the registry's provider/model choice reaches child calls via copied context.
    """
    provider_token = _REQUEST_PROVIDER.set(LLMProvider(provider))
    model_token = _REQUEST_MODEL.set(model)
    try:
        yield
    finally:
        _REQUEST_MODEL.reset(model_token)
        _REQUEST_PROVIDER.reset(provider_token)


@contextmanager
def provider_request_scope(config: Any | None) -> Iterator[None]:
    """Bind tenant provider settings to this task and copied worker contexts."""
    provider = LLMProvider(config.provider) if config else None
    config_token = _ACTIVE_PROVIDER_CONFIG.set(config)
    provider_token = _ACTIVE_PROVIDER.set(provider)
    try:
        yield
    finally:
        _ACTIVE_PROVIDER.reset(provider_token)
        _ACTIVE_PROVIDER_CONFIG.reset(config_token)


def active_provider_config() -> Any | None:
    return _ACTIVE_PROVIDER_CONFIG.get()


def current_transport_provider() -> LLMProvider:
    """Tenant scope, then task override, then process provider."""
    return _ACTIVE_PROVIDER.get() or _REQUEST_PROVIDER.get() or current_provider()


def _select_provider(
    provider: LLMProvider | str | None, *, policy: bool, tenant_config: Any | None
) -> LLMProvider:
    """The one routing rule `messages_create` and `effective_provider` share.

    The generation policy always runs on Anthropic; otherwise a tenant's own key
    decides; otherwise the caller's argument, then the request scope, then the
    process provider."""
    if policy:
        # Production generation remains pinned to Anthropic. Local/test may
        # explicitly opt into Ollama so the complete paper flow can be tested
        # without a paid API key.
        is_local = os.getenv("APP_ENV", "").strip().lower() in {"local", "test"}
        local_ollama = is_local and os.getenv("OLLAMA_GENERATION_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        local_google = is_local and os.getenv("GOOGLE_GENERATION_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if local_ollama:
            return LLMProvider.OLLAMA
        if local_google and current_provider() is LLMProvider.GOOGLE:
            return LLMProvider.GOOGLE
        return LLMProvider.ANTHROPIC
    if tenant_config is not None:
        return LLMProvider(tenant_config.provider)
    if provider is not None:
        return LLMProvider(provider)
    return _REQUEST_PROVIDER.get() or current_transport_provider()


def effective_provider(provider: LLMProvider | str | None = None) -> LLMProvider:
    """The provider a `messages_create(provider=provider)` call made here would reach."""
    return _select_provider(provider, policy=_GENERATION_POLICY.get(), tenant_config=active_provider_config())


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if val < 1:
        raise ValueError(f"{name}={val} must be at least 1")
    return val


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if val <= 0:
        raise ValueError(f"{name}={val} must be positive")
    return val


def _process_google_config():
    """Return local/test Gemini configuration without persisting its plaintext key."""
    if os.getenv("APP_ENV", "").strip().lower() not in {"local", "test"}:
        raise ValueError("Process-level Google provider is restricted to APP_ENV=local or test")
    model = os.getenv("LLM_EXTRACT_MODEL", "").strip()
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not model:
        raise ValueError("LLM_EXTRACT_MODEL is required when LLM_PROVIDER=google")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is required when LLM_PROVIDER=google")
    config = SimpleNamespace(
        provider=LLMProvider.GOOGLE.value,
        model=model,
        base_url=None,
        input_price_per_million_usd=None,
        output_price_per_million_usd=None,
    )
    return config, api_key


def shared_client(provider: LLMProvider | None = None):
    """A process-wide Messages API client for the selected hosted provider.

    Lazy rather than module-scope: importing this module must not require an API
    key, or every test and every offline tool that merely imports an agent would
    fail at import time.

    `provider`, when given, is used instead of `current_provider()` — this is
    what lets `messages_create(provider=...)` route a single call to the
    OpenRouter or Anthropic client without depending on `LLM_PROVIDER`.
    """
    global _shared_client, _shared_openrouter_client
    resolved = provider if provider is not None else current_provider()
    if resolved == LLMProvider.OPENROUTER:
        if _shared_openrouter_client is None:
            with _client_lock:
                if _shared_openrouter_client is None:
                    import anthropic

                    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
                    if not api_key:
                        raise ValueError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter")
                    # The SDK appends /v1/messages. Keep /v1 out of the base URL.
                    _shared_openrouter_client = anthropic.Anthropic(
                        api_key="",
                        auth_token=api_key,
                        default_headers={"X-Api-Key": anthropic.omit},
                        base_url="https://openrouter.ai/api",
                        timeout=_env_float("ANTHROPIC_TIMEOUT_S", _DEFAULT_TIMEOUT_S),
                        max_retries=_env_int("ANTHROPIC_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
                    )
        return _shared_openrouter_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                import anthropic

                _shared_client = anthropic.Anthropic(
                    timeout=_env_float("ANTHROPIC_TIMEOUT_S", _DEFAULT_TIMEOUT_S),
                    max_retries=_env_int("ANTHROPIC_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
                )
    return _shared_client


def _rate_governor() -> threading.Semaphore:
    """Bounds in-flight calls process-wide.

    Sized once, on first use. A thread waiting here holds no permit and makes no
    nested LLM call, so nesting a pool inside a pool queues rather than
    deadlocks — that property is what makes one process-wide governor safe
    despite the nested pools.
    """
    global _governor, _governor_size
    if _governor is None:
        with _client_lock:
            if _governor is None:
                _governor_size = _env_int("ANTHROPIC_MAX_CONCURRENCY", _DEFAULT_MAX_CONCURRENCY)
                _governor = threading.Semaphore(_governor_size)
    return _governor


class TruncatedResponseError(RuntimeError):
    """A call hit its max_tokens cap and returned a cut-off body.

    Never swallowed: this project has shipped four separate silent-truncation
    defects (thesis JSON, section 6, valuation comps, reviewer findings), each
    found only by noticing bad output downstream. Raising here makes the class
    of bug impossible. On Opus 5 / Sonnet 5 thinking shares the max_tokens
    budget with the response, so a cap tuned pre-migration can truncate.
    """


class ContextWindowExceededError(RuntimeError):
    """The INPUT exceeded the model's context window.

    Deliberately distinct from TruncatedResponseError: this one is not fixed by
    raising max_tokens, it needs less input (split the document, compact the
    conversation). Conflating the two sends you tuning the wrong dial.
    """


def _resolve_effort(stage: str | None) -> str | None:
    """`output_config.effort` level for `stage`, or None to omit `output_config`
    entirely (leaves the API default, currently "high", in force).

    Checks ANTHROPIC_EFFORT_<STAGE> first, then falls back to the global
    ANTHROPIC_EFFORT; unset at both means None. Uses `os.getenv(name)` (not the
    `... or None` idiom used for the geo guard above) because here a var that is
    SET-BUT-EMPTY must be treated as an operator mistake and raise, not silently
    collapse into "unset" — `or None` would swallow that distinction.
    """
    candidates = [f"ANTHROPIC_EFFORT_{stage.upper()}"] if stage else []
    candidates.append("ANTHROPIC_EFFORT")
    for name in candidates:
        val = os.getenv(name)
        if val is None:
            continue
        if val not in _EFFORT_LEVELS:
            raise ValueError(
                f"{name}={val!r} is not a valid effort level: accepted values are "
                f"{', '.join(_EFFORT_LEVELS)}. Unset {name} to use the API default "
                '(currently "high") instead.'
            )
        return val
    return None


def _record(rec: UsageRecord, *, billable: bool) -> None:
    """Record usage to the active run ledger and, when scoped, tenant telemetry."""
    tenant_id = active_tenant_id()
    config = active_provider_config()
    provider_name = rec.provider or (config.provider if config else current_transport_provider().value)
    rec.provider = provider_name
    if tenant_id:
        from creditpaper.llm.tenant_config import record_usage

        rec.tenant_id = tenant_id
        record_usage(tenant_id, provider_name, rec)

    led = active_ledger()
    if led is None:
        if billable:
            note_unscoped_call(rec.tool)
        return
    led.record(rec)


@timed("llm")
def _anthropic_call(
    *,
    tool: str,
    client: Any,
    kwargs: dict,
    geo: str | None,
    allow_truncation: bool,
    provider: LLMProvider,
) -> LLMResponse:
    """Run the Anthropic path: cache wrapping, geo guard, effort, rate governor,
    streaming threshold, and the actual `client.messages.create` / `.stream` call.

    Returns an `LLMResponse` carrying the underlying Message in `.raw` so the
    `.usage` / `.content` attributes the 7 current call sites read stay reachable
    until Task 5 migrates them to `.text` / `.input_tokens` / `.output_tokens`.

    `provider` picks the OpenRouter-vs-Anthropic shared client explicitly —
    passed through from `messages_create`'s already-resolved provider (which
    may be a per-call override) rather than re-reading `current_provider()`
    here, so a `provider=` override reaches the actual wire client.
    """
    t0 = time.monotonic()
    model = kwargs.get("model", "")
    billable = client is None or client is _shared_client or client is _shared_openrouter_client
    client = client or shared_client(provider)
    try:
        with _rate_governor():
            max_tokens = kwargs.get("max_tokens", 0)
            if max_tokens >= STREAM_AT_OR_ABOVE_MAX_TOKENS and hasattr(client.messages, "stream"):
                with client.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
            else:
                resp = client.messages.create(**kwargs)
    except BaseException:
        # A timeout, a mid-stream disconnect, or a 429/5xx that outlived the
        # SDK's own retries recorded NOTHING — no cost, no latency, no failure —
        # while Anthropic billed the input it had already processed. A run of
        # failures therefore reported as a cheap run.
        #
        # There is no usage object on this path, so cost is 0.0 and the ledger
        # is honest about that via `failed`: the report says it made calls it
        # cannot price rather than quietly omitting them. BaseException, not
        # Exception, so a KeyboardInterrupt mid-run still leaves a trace of what
        # was in flight.
        _record(
            UsageRecord(
                tool=tool,
                model=model,
                latency_s=time.monotonic() - t0,
                provider=provider.value,
                unpriced=(
                    prices_for(model) is None
                    and not (
                        (config := _pricing_config(model)) is not None
                        and config.input_price_per_million_usd is not None
                        and config.output_price_per_million_usd is not None
                    )
                ),
                failed=True,
            ),
            billable=billable,
        )
        raise
    dt = time.monotonic() - t0

    # getattr, not attribute access: test stubs and older response shapes may
    # not carry stop_reason at all.
    stop_reason = getattr(resp, "stop_reason", None)
    context_exceeded = stop_reason == "model_context_window_exceeded"
    truncated = stop_reason == "max_tokens"

    # Record BEFORE raising, and only once: both error paths below still consumed
    # billable input tokens, and Anthropic bills for them whether or not this
    # function raises afterwards. Recording after a `raise` never runs, so a
    # truncated or context-overflowed call would spend real money that never
    # lands in the cost-of-sale ledger — exactly the silent misprice this ledger
    # exists to prevent. One record call, not one per branch, so the recording
    # logic itself can't drift between the success and error paths.
    u = resp.usage
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    tenant_config = _pricing_config(model)
    if tenant_config:
        from creditpaper.llm.tenant_config import cost_for_provider

        call_cost, unpriced = cost_for_provider(
            model, u.input_tokens or 0, u.output_tokens or 0, cw, cr, config=tenant_config
        )
    else:
        call_cost = cost_usd(model, u.input_tokens or 0, u.output_tokens or 0, cw, cr, inference_geo=geo)
        unpriced = prices_for(model) is None
    _record(
        UsageRecord(
            tool=tool,
            model=model,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write_tokens=cw,
            cache_read_tokens=cr,
            cost_usd=call_cost,
            latency_s=dt,
            unpriced=unpriced,
            truncated=truncated,
            provider=provider.value,
        ),
        billable=billable,
    )

    if context_exceeded:
        raise ContextWindowExceededError(
            f"{tool}: input exceeded the context window on {model}. "
            f"Reduce input (split the document); raising max_tokens will not help."
        )
    if truncated and not allow_truncation:
        out = getattr(resp.usage, "output_tokens", 0)
        raise TruncatedResponseError(
            f"{tool}: response truncated at max_tokens={kwargs.get('max_tokens', 0)} on {model} "
            f"(produced {out} output tokens). Raise max_tokens — on this model "
            f"thinking and response text share that budget."
        )

    content_list = getattr(resp, "content", None) or []
    text = "".join(b.text for b in content_list if getattr(b, "type", None) == "text")
    # getattr (not attribute access) keeps the helper tolerant of test stubs that
    # don't define a `content` attribute.
    return LLMResponse(
        text=text,
        stop_reason=stop_reason or "",
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        raw=resp,
    )


@timed("llm")
def _ollama_call(
    *,
    tool: str,
    kwargs: dict,
    allow_truncation: bool,
    provider: LLMProvider,
    stage: str | None = None,
) -> LLMResponse:
    """Run the Ollama path: convert Anthropic-shaped kwargs to OpenAI chat format,
    call ollama_chat, and normalise the response.

    Anthropic-specific kwargs (`cache_control`, `output_config.effort`,
    `inference_geo`) are silently ignored — the OpenAI chat format has no
    equivalent and `ollama_chat` would error on unknown fields.

    PDF documents in the `messages` payload are rendered to page images via
    `render_pdf_pages` and re-emitted as `image_url` blocks, because
    qwen2.5-vl reads images, not raw PDF bytes.
    """
    model = kwargs.get("model", "")
    messages = _anthropic_kwargs_to_ollama_messages(kwargs, provider=provider, model=model)
    t0 = time.monotonic()
    resp = ollama_chat(
        messages=messages,
        model=model,
        max_tokens=kwargs.get("max_tokens"),
        temperature=kwargs.get("temperature"),
        top_p=kwargs.get("top_p"),
        json_mode=(
            stage in {"extract", "classify", "structural", "support", "adjudicate"}
            or bool((kwargs.get("output_config") or {}).get("format"))
        ),
    )
    dt = time.monotonic() - t0

    choice = (resp.get("choices") or [{}])[0]
    finish_reason = choice.get("finish_reason", "")
    message = choice.get("message") or {}
    text = message.get("content", "") or ""
    stop_reason = (
        "end_turn"
        if finish_reason == "stop"
        else "max_tokens"
        if finish_reason == "length"
        else finish_reason
    )
    usage = resp.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    tenant_config = active_provider_config()
    tenant_cost = 0.0
    tenant_unpriced = True
    if tenant_config:
        from creditpaper.llm.tenant_config import cost_for_provider

        tenant_cost, tenant_unpriced = cost_for_provider(
            model, input_tokens, output_tokens, config=tenant_config
        )
    _record(
        UsageRecord(
            tool=tool,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=tenant_cost,
            provider=LLMProvider.OLLAMA.value,
            latency_s=dt,
            unpriced=tenant_unpriced,
            truncated=(finish_reason == "length"),
        ),
        billable=False,
    )

    if finish_reason == "length" and not allow_truncation:
        raise TruncatedResponseError(
            f"{tool}: response truncated at max_tokens={kwargs.get('max_tokens', 0)} on {model} "
            f"(produced {output_tokens} output tokens). Raise max_tokens."
        )

    return LLMResponse(
        text=text,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        raw=resp,
    )


def _anthropic_kwargs_to_ollama_messages(
    kwargs: dict,
    *,
    provider: LLMProvider | None = None,
    model: str = "",
) -> list[dict]:
    """Translate Anthropic `messages` / `system` kwargs into OpenAI chat format.

    Text content passes through unchanged. A `document` block (base64 PDF) is
    rendered to per-page PNGs and re-emitted as `image_url` data URIs — that is
    how qwen2.5-vl ingests documents. Any other Anthropic block type is coerced
    to its `text` field if present, otherwise dropped with a warning-shaped
    `None` (the call will still run; the caller learns from the empty result).
    """
    from creditpaper.llm.registry import ollama_model_supports_images

    chat_messages: list[dict] = []
    accepts_images = ollama_model_supports_images(model)
    system = kwargs.get("system")
    if isinstance(system, str) and system.strip():
        chat_messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        # Anthropic's list-of-blocks form (what cache_system produces) — concat the
        # text parts. cache_control is silently dropped; OpenAI has no equivalent.
        text_parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
        if text_parts:
            chat_messages.append({"role": "system", "content": "\n".join(text_parts)})

    for m in kwargs.get("messages", []) or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            chat_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            text_parts: list[str] = []
            images: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "document" and accepts_images:
                    source = block.get("source") or {}
                    if source.get("type") == "base64" and source.get("media_type") == "application/pdf":
                        pdf_bytes = base64.b64decode(source.get("data", ""))
                        # Ollama: limit to 3 pages (vision tokens are expensive)
                        selected = provider or current_transport_provider()
                        max_p = 3 if selected == LLMProvider.OLLAMA else 30
                        for page_bytes in render_pdf_pages(pdf_bytes, max_pages=max_p):
                            images.append(base64.b64encode(page_bytes).decode("ascii"))
                elif btype == "image" and accepts_images:
                    source = block.get("source") or {}
                    if source.get("type") == "base64" and source.get("data"):
                        images.append(source["data"])
            # Ollama: cap total images to avoid context overflow
            selected = provider or current_transport_provider()
            if images and selected == LLMProvider.OLLAMA:
                max_imgs = int(os.getenv("OLLAMA_MAX_IMAGES", "3"))
                if len(images) > max_imgs:
                    images = images[:max_imgs]
            msg: dict = {"role": role, "content": "\n".join(text_parts)}
            if images:
                msg["images"] = images
            chat_messages.append(msg)
        else:
            chat_messages.append({"role": role, "content": ""})

    return chat_messages


def messages_create(
    *,
    tool: str,
    stage: str | None = None,
    provider: LLMProvider | None = None,
    client=None,
    cache_system: bool = False,
    allow_truncation: bool = False,
    **kwargs,
):
    """Instrumented LLM call. Records usage to the active ledger (no-op when
    none is active). All system LLM calls route through here so cost is captured
    in one place — extraction and generation alike.

    `provider`, when given, overrides `current_provider()` for THIS call only.
    Nothing is written to the environment, so this is thread-safe and leaves
    every other concurrent call (and every call after this one returns) reading
    `LLM_PROVIDER` exactly as before. This is what lets `llm.registry.resolve()`
    pick a provider per ingestion task without a global mutable switch: the
    caller passes the resolved provider through to this one call, and the
    process-wide default is untouched.

    With `provider=None` (the default), provider selection is governed by
    `LLM_PROVIDER` (read fresh on every call):
      - `anthropic` (default): wraps `client.messages.create` / `.stream`, with
        cache_control wrapping, `output_config.effort`, `inference_geo`, the
        rate governor, the streaming threshold, the truncation guard and the
        cost ledger all enforced exactly as before.
      - `openrouter`: uses OpenRouter's Anthropic-compatible Messages endpoint
        with OPENROUTER_API_KEY and OPENROUTER_*_MODEL settings. PDF, image,
        caching, streaming and truncation handling use the same SDK path.
        ANTHROPIC_INFERENCE_GEO is not sent; routing is managed by OpenRouter.
      - `ollama`: routes through the OpenAI-compatible Ollama HTTP API via
        `ollama_chat`, normalising the response to `LLMResponse`. PDF documents
        are rendered to per-page images first because qwen2.5-vl reads images,
        not base64 PDFs. Anthropic-only kwargs (`cache_control`,
        `output_config.effort`, `inference_geo`) are silently ignored on this
        path; cost is recorded as $0 with `unpriced=True`.

    `stage` names the pipeline stage (e.g. "section", "review", "extract") for
    ANTHROPIC_EFFORT_<STAGE> lookup — deliberately coarser than `tool`, which
    carries per-call detail (`agent:section6`, `extract:valuation/comps`) too
    granular to key a cost/latency dial on: an operator tuning effort wants one
    knob per stage, not one per section number or per extractor sub-call.

    `cache_system=True` (no-op on the Ollama path) wraps a string `system`
    prompt in a `cache_control` block so sequential calls that reuse the SAME
    system (the review agent's 8 calls, a section's draft→repair→retry, the §9
    tail) read it instead of re-processing it. Purely additive: the prompt
    bytes are unchanged, so output is unchanged; if the prefix is below the
    model's cacheable minimum it silently no-ops at no cost.

    `allow_truncation=True` suppresses the `TruncatedResponseError` raised on
    `stop_reason == "max_tokens"` (or Ollama `finish_reason == "length"`). The
    call is still recorded with `truncated=True` so the report flags it. Apply
    this only where a truncated body is genuinely actionable downstream; the
    default is to raise because silent truncation has shipped four separate
    paper defects in this codebase already."""
    policy = _GENERATION_POLICY.get()
    tenant_config = active_provider_config()
    if policy and tenant_config is not None and LLMProvider(tenant_config.provider) != LLMProvider.ANTHROPIC:
        with provider_request_scope(None):
            return messages_create(
                tool=tool,
                stage=stage,
                provider=LLMProvider.ANTHROPIC,
                client=client,
                cache_system=cache_system,
                allow_truncation=allow_truncation,
                **kwargs,
            )
    provider = _select_provider(provider, policy=policy, tenant_config=tenant_config)
    if tenant_config is not None:
        if not policy:
            kwargs["model"] = tenant_config.model
        if provider in (LLMProvider.OPENAI, LLMProvider.GOOGLE, LLMProvider.COMPATIBLE):
            return _byok_http_call(provider, tenant_config, tool, client, kwargs, allow_truncation)
    elif not policy:
        model_override = _REQUEST_MODEL.get()
        if provider is not None and model_override:
            kwargs["model"] = model_override

    if provider == LLMProvider.GOOGLE:
        config, api_key = _process_google_config()
        kwargs["model"] = str(kwargs.get("model") or config.model)
        return _byok_http_call(
            provider,
            config,
            tool,
            client,
            kwargs,
            allow_truncation,
            api_key=api_key,
        )

    if provider == LLMProvider.NVIDIA:
        return _nvidia_call(
            tool=tool,
            client=client,
            kwargs=kwargs,
            allow_truncation=allow_truncation,
            provider=provider,
        )
    if provider == LLMProvider.OLLAMA:
        # Ollama ignores cache_control, output_config.effort, inference_geo.
        # Don't pre-process the kwargs — pass them straight to the converter.
        return _ollama_call(
            tool=tool,
            kwargs=kwargs,
            allow_truncation=allow_truncation,
            provider=provider,
            stage=stage,
        )

    # -- Messages API preprocessing (Anthropic and OpenRouter) ---------------
    if cache_system:
        sys = kwargs.get("system")
        if isinstance(sys, str) and sys.strip():
            kwargs["system"] = [{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}]
    model = kwargs.get("model", "")
    # Read at call time, not module scope: a module-level capture would freeze
    # the value at process start, so flipping the env var would silently not
    # take effect for an already-running job.
    geo = (os.getenv("ANTHROPIC_INFERENCE_GEO") or None) if provider == LLMProvider.ANTHROPIC else None
    if geo:
        if _geo_incompatible(model):
            # Name both offending settings — the operator needs to know which of
            # the two to change, not just that "something" is wrong.
            raise ValueError(
                f"{model} is incompatible with ANTHROPIC_INFERENCE_GEO={geo!r}: "
                f"inference_geo requires Claude 4.6+ and {model} returns a 400. "
                f"Unset ANTHROPIC_INFERENCE_GEO or use a Claude 4.6+ model."
            )
        kwargs["inference_geo"] = geo
    if "temperature" in kwargs and _rejects_temperature(model):
        # A sampling hint, not a requirement: a model that refuses it (Sonnet 5:
        # "temperature is deprecated for this model", a 400) is simply not sent
        # one, so a tenant's own key on such a model does not fail every call.
        kwargs.pop("temperature")
    # Same call-time-not-module-scope reasoning as the geo read above: freezing
    # this at import would mean flipping the env var mid-run silently would not
    # take effect for an already-running job.
    effort = _resolve_effort(stage)
    if effort:
        # Goes inside output_config, not top-level -- the API rejects a
        # top-level `effort` kwarg on these models. MERGED, never assigned: a
        # caller's `format` (its JSON schema) lives in the same object, and
        # replacing it silently turned every structured call into free text.
        # A caller that set its own effort keeps it.
        output_config = dict(kwargs.get("output_config") or {})
        output_config.setdefault("effort", effort)
        kwargs["output_config"] = output_config

    close_tenant_client = tenant_config is not None and client is None
    if close_tenant_client:
        import anthropic

        from creditpaper.llm.tenant_config import decrypt_api_key

        client = anthropic.Anthropic(
            api_key=decrypt_api_key(tenant_config),
            timeout=_env_float("ANTHROPIC_TIMEOUT_S", _DEFAULT_TIMEOUT_S),
            max_retries=_env_int("ANTHROPIC_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
        )
    try:
        return _anthropic_call(
            tool=tool,
            client=client,
            kwargs=kwargs,
            geo=geo,
            allow_truncation=allow_truncation,
            provider=provider,
        )
    finally:
        if close_tenant_client:
            client.close()


@timed("llm")
def _byok_http_call(provider, config, tool, client, kwargs, allow_truncation, *, api_key=None):
    """Call a tenant-configured OpenAI, Google or compatible endpoint.

    Credentials are decrypted only immediately before the request. Compatible
    URLs are revalidated at the transport boundary, redirects are disabled, and
    provider response bodies are never included in raised transport errors.
    """
    import httpx

    from creditpaper.llm.tenant_config import cost_for_provider, decrypt_api_key

    global _shared_http_client
    model = str(kwargs.get("model") or config.model)
    if provider == LLMProvider.COMPATIBLE:
        _validate_public_https_url(config.base_url)
    key = api_key if api_key is not None else decrypt_api_key(config)
    if client is None:
        if _shared_http_client is None:
            with _client_lock:
                if _shared_http_client is None:
                    _shared_http_client = httpx.Client(follow_redirects=False, trust_env=False)
        client = _shared_http_client
    billable = client is _shared_http_client
    started = time.monotonic()
    record = UsageRecord(tool=tool, model=model, provider=provider.value, unpriced=True, failed=True)
    try:
        if provider == LLMProvider.GOOGLE:
            safe_model = quote(model.removeprefix("models/"), safe="-_.")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{safe_model}:generateContent"
            payload = _google_payload(kwargs)
            headers = {"x-goog-api-key": key}
        else:
            base = "https://api.openai.com/v1" if provider == LLMProvider.OPENAI else config.base_url
            _validate_public_https_url(base)
            url = base.rstrip("/") + "/chat/completions"
            payload = _openai_payload(kwargs)
            headers = {"Authorization": f"Bearer {key}"}
        with _rate_governor():
            response = client.post(
                url,
                json=payload,
                headers=headers,
                timeout=_env_float("LLM_PROVIDER_TIMEOUT_S", 600.0),
                follow_redirects=False,
            )

        if response.status_code >= 400:
            raise RuntimeError(f"{provider.value} inference failed (HTTP {response.status_code})")
        raw = response.json()
        if provider == LLMProvider.GOOGLE:
            candidate = raw["candidates"][0]
            text = "".join(part.get("text", "") for part in candidate["content"].get("parts", []))
            finish = str(candidate.get("finishReason", "STOP")).upper()
            usage = raw.get("usageMetadata") or {}
            record.input_tokens = int(usage.get("promptTokenCount") or 0)
            record.output_tokens = int(usage.get("candidatesTokenCount") or 0)
            truncated = finish in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}
        else:
            choice = raw["choices"][0]
            text = choice["message"].get("content")
            if isinstance(text, list):
                text = "".join(
                    part.get("text", "")
                    for part in text
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            truncated = choice["finish_reason"] == "length"
            usage = raw.get("usage") or {}
            record.input_tokens = int(usage.get("prompt_tokens") or 0)
            record.output_tokens = int(usage.get("completion_tokens") or 0)
        record.truncated = truncated
        record.failed = False
        record.cost_usd, record.unpriced = cost_for_provider(
            model, record.input_tokens, record.output_tokens, config=config
        )
        if truncated and not allow_truncation:
            raise TruncatedResponseError(
                f"{provider.value} response reached max_tokens; increase the configured call budget"
            )
        if not isinstance(text, str) or not text.strip():
            record.failed = True
            raise RuntimeError(f"{provider.value} returned an empty completion")
        return LLMResponse(
            text,
            "max_tokens" if truncated else "end_turn",
            record.input_tokens,
            record.output_tokens,
            raw,
        )
    except httpx.TimeoutException:
        raise RuntimeError(f"{provider.value} inference timed out") from None
    except httpx.HTTPError:
        raise RuntimeError(f"{provider.value} inference transport failed") from None
    finally:
        record.latency_s = time.monotonic() - started
        _record(record, billable=billable)


def _validate_public_https_url(base_url: str | None) -> None:
    """Reject compatible API URLs with credentials, redirects, or non-public DNS."""
    if not base_url:
        raise ValueError("Compatible provider base URL is required")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Compatible provider base URL must be a public HTTPS URL")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        raise ValueError("Compatible provider host could not be resolved") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Compatible provider host must resolve only to public IP addresses")


def _google_payload(kwargs: dict) -> dict:
    contents = [
        {
            "role": "model" if message.get("role") == "assistant" else "user",
            "parts": _google_parts(message.get("content")),
        }
        for message in kwargs.get("messages", [])
    ]
    generation = {}
    if "max_tokens" in kwargs:
        generation["maxOutputTokens"] = kwargs["max_tokens"]
    if "temperature" in kwargs:
        generation["temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        generation["topP"] = kwargs["top_p"]
    if kwargs.get("stop_sequences"):
        generation["stopSequences"] = kwargs["stop_sequences"]
    body: dict = {"contents": contents}
    system = _text_content(kwargs.get("system"))
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if generation:
        body["generationConfig"] = generation
    return body


def _google_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append({"text": block.get("text", "")})
            continue
        source = block.get("source") or {}
        if block.get("type") in {"image", "document"} and source.get("type") == "base64":
            media = source.get("media_type", "image/png")
            if block.get("type") == "document" and media == "application/pdf":
                pdf = base64.b64decode(source.get("data", ""), validate=True)
                parts.extend(
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(page).decode("ascii"),
                        }
                    }
                    for page in render_pdf_pages(pdf, max_pages=30)
                )
            else:
                parts.append({"inlineData": {"mimeType": media, "data": source.get("data", "")}})
    return parts


def _text_content(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _openai_payload(kwargs: dict) -> dict:
    messages = []
    system = _text_content(kwargs.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for message in kwargs.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") in {"image", "document"}:
                    source = block.get("source") or {}
                    if source.get("type") != "base64":
                        continue
                    media = source.get("media_type", "image/png")
                    if block.get("type") == "document" and media == "application/pdf":
                        pdf = base64.b64decode(source.get("data", ""), validate=True)
                        for page in render_pdf_pages(pdf, max_pages=30):
                            encoded = base64.b64encode(page).decode("ascii")
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                                }
                            )
                    else:
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{source.get('data', '')}"},
                            }
                        )
            content = parts
        messages.append({"role": message.get("role", "user"), "content": content or ""})
    payload: dict = {"model": kwargs.get("model"), "messages": messages}
    for name in ("max_tokens", "temperature", "top_p"):
        if name in kwargs:
            payload[name] = kwargs[name]
    if kwargs.get("stop_sequences"):
        payload["stop"] = kwargs["stop_sequences"]
    return payload


def _nvidia_messages(kwargs: dict) -> list[dict]:
    """Convert existing messages to text chat; PDF/OCR remains local."""
    from io import BytesIO

    from PIL import Image

    from creditpaper.processing.extractors.pdf import extract_pdf
    from creditpaper.processing.ocr import ocr_page

    def text_content(content):
        if isinstance(content, str):
            return content
        parts = []
        for block in content or []:
            kind = block.get("type")
            source = block.get("source") or {}
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind == "document":
                if source.get("type") == "text":
                    parts.append(source.get("data", ""))
                elif source.get("type") == "base64" and source.get("media_type") == "application/pdf":
                    result = extract_pdf(
                        base64.b64decode(source["data"], validate=True), original_filename="document.pdf"
                    )
                    if not any(p.get("text", "").strip() for p in result.page_texts):
                        raise ValueError("PDF contains no readable text; local OCR is required")
                    parts.extend(f"[Page {p['page']}]\n{p['text']}" for p in result.page_texts)
                else:
                    raise ValueError("Unsupported NVIDIA document source; provide locally extracted text")
            elif kind == "image":
                if source.get("type") != "base64":
                    raise ValueError("NVIDIA text inference requires local image OCR")
                with Image.open(BytesIO(base64.b64decode(source["data"], validate=True))) as img:
                    text = ocr_page(img)
                if not text.strip():
                    raise ValueError("Image contains no readable OCR text")
                parts.append(text)
            else:
                raise ValueError("Unsupported content block for NVIDIA text inference")
        return "\n\n".join(parts)

    messages = []
    if kwargs.get("system"):
        messages.append({"role": "system", "content": text_content(kwargs["system"])})
    messages.extend(
        {"role": m["role"], "content": text_content(m.get("content"))} for m in kwargs.get("messages", [])
    )
    return messages


def _nvidia_call(
    *, tool: str, client: Any, kwargs: dict, allow_truncation: bool, provider: LLMProvider
) -> LLMResponse:
    """One OpenAI-compatible HTTP call, without routing or implicit retries.

    Errors intentionally exclude provider bodies and request data: either may
    contain prompts, document text or credentials. The HTTP client is injectable
    for offline tests and reused for connection pooling in production.
    """
    import httpx

    global _shared_nvidia_client
    model = str(kwargs.get("model") or "").strip()
    if not model:
        raise ValueError("An environment-configured NVIDIA model is required")
    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("NVIDIA_API_KEY is required when LLM_PROVIDER=nvidia")
    base_url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    payload = {"model": model, "messages": _nvidia_messages(kwargs), "stream": False}
    thinking = os.getenv("NVIDIA_ENABLE_THINKING", "").strip().lower()
    if thinking:
        if thinking not in ("true", "false"):
            raise ValueError("NVIDIA_ENABLE_THINKING must be true, false, or unset")
        payload["chat_template_kwargs"] = {"enable_thinking": thinking == "true"}
    for name in ("max_tokens", "temperature", "top_p"):
        if name in kwargs:
            payload[name] = kwargs[name]
    if kwargs.get("stop_sequences"):
        payload["stop"] = kwargs["stop_sequences"]
    if client is None:
        if _shared_nvidia_client is None:
            with _client_lock:
                if _shared_nvidia_client is None:
                    _shared_nvidia_client = httpx.Client(follow_redirects=False)
        client = _shared_nvidia_client
    return _nvidia_request(
        tool=tool,
        client=client,
        model=model,
        base_url=base_url,
        api_key=api_key,
        payload=payload,
        allow_truncation=allow_truncation,
        provider=provider,
    )


@timed("llm")
def _nvidia_request(*, tool, client, model, base_url, api_key, payload, allow_truncation, provider):
    import httpx

    t0 = time.monotonic()
    rec = UsageRecord(tool=tool, model=model, provider=provider.value, unpriced=True, failed=True)
    try:
        with _rate_governor():
            response = client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_env_float("NVIDIA_TIMEOUT_S", 120.0),
            )
        if response.status_code >= 400:
            # Inspect only error codes; never propagate the provider's message/body.
            if response.status_code == 400:
                try:
                    code = response.json().get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                if code in ("context_length_exceeded", "model_context_window_exceeded"):
                    raise ContextWindowExceededError(
                        "NVIDIA input exceeds the context window; split the document"
                    )
            raise RuntimeError(f"NVIDIA inference failed (HTTP {response.status_code})")
        try:
            raw = response.json()
            choice = raw["choices"][0]
            text = choice["message"]["content"]
            finish = choice["finish_reason"]
            usage = raw.get("usage") or {}
            rec.input_tokens = int(usage.get("prompt_tokens") or 0)
            rec.output_tokens = int(usage.get("completion_tokens") or 0)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError("NVIDIA returned an invalid or empty completion") from None
        rec.truncated = finish == "length"
        rec.failed = False
        if rec.truncated and not allow_truncation:
            raise TruncatedResponseError(
                "NVIDIA response reached max_tokens; increase the configured call budget"
            )
        if not isinstance(text, str) or not text.strip():
            rec.failed = True
            raise RuntimeError("NVIDIA returned an invalid or empty completion")
        if finish not in ("stop", "length"):
            rec.failed = True
            raise RuntimeError("NVIDIA completion did not finish successfully")
        return LLMResponse(
            text=text,
            stop_reason="max_tokens" if rec.truncated else "end_turn",
            input_tokens=rec.input_tokens,
            output_tokens=rec.output_tokens,
            raw=raw,
        )
    except httpx.TimeoutException:
        raise RuntimeError("NVIDIA inference timed out") from None
    except httpx.HTTPError:
        raise RuntimeError("NVIDIA inference transport failed") from None
    finally:
        rec.latency_s = time.monotonic() - t0
        _record(rec, billable=client is _shared_nvidia_client)

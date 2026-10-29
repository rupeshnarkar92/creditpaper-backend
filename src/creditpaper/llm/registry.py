"""Which model runs which ingestion task, and what that transport can do.

Every call still goes through `llm.client.messages_create` — this only chooses
the provider and model per task, so swapping one task's model is configuration,
not a code change. `messages_create` itself now accepts an optional
`provider: LLMProvider | None` that overrides `current_provider()` for exactly
that call (no environment mutation, thread-safe); this module is what decides
which provider and model a caller should pass.

Three rules encoded here:

  * SECOND_READ must not share a model FAMILY with EXTRACT. Reading twice with
    the same family repeats the same mistake; different families fail
    differently, which is what makes agreement meaningful. There is no default
    second reader — it must be configured explicitly (`LLM_TASK_SECOND_READ`).
  * Capabilities are declared per transport so the pipeline degrades on
    purpose. Without prompt caching, a document is re-sent per window; without
    structured output, the caller validates the schema and re-asks once.
    Each flag below is a claim about the `messages_create` code path, not
    about the provider's API in general, and carries a citation to the exact
    branch that makes it true or false.
  * Process-routed tenant documents may only reach providers cleared by the
    data-handling policy (`docs/DATA_HANDLING.md`): Anthropic directly, or
    self-hosted Ollama. Other process providers are refused outside
    `local`/`test`. Explicit tenant BYOK is a separate, user-authorized route
    and is resolved ahead of both the process and task environment.

Anthropic model defaults are NOT hardcoded here. They come from
`creditpaper.config` (`extract_model()`, `review_model()`), which is the one
place stage tiers are decided and which already carries the
`ANTHROPIC_INFERENCE_GEO` compatibility reasoning (a pre-4.6 model, including
`claude-haiku-4-5`, 400s when geo is set — see `config.py` and
`llm/client.py`'s `_GEO_INCOMPATIBLE_MODELS`). Reusing `extract_model()` /
`review_model()` sidesteps that trap entirely rather than re-hardcoding a
Haiku default and re-deriving the same caveat.

Candidates below are to be SCORED on the blind set, not assumed. That includes
`typesafe:jev`, whose published speed, price and calibrated-confidence claims
are the vendor's own and have been measured on none of our documents; it may
not receive tenant data before its data-processing and retention terms are in
writing (`docs/DATA_HANDLING.md`), and it has no transport in
`creditpaper.llm.client` at all — it exists in `CANDIDATES` only, for the
scoring harness to name, and `resolve()` refuses it like any other provider
with no wire-up.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel

from creditpaper import config
from creditpaper.llm.providers import LLMProvider, current_provider


class Task(str, Enum):
    CLASSIFY = "classify"
    STRUCTURAL = "structural"
    EXTRACT = "extract"
    SECOND_READ = "second_read"
    ADJUDICATE = "adjudicate"
    # The semantic support check (ingest/support.py, Plan C Task 12): "does
    # this span state that <field> = <value>?". Defaults to EXTRACT's model;
    # LLM_TASK_SUPPORT overrides it like any other task.
    SUPPORT = "support"


class Capability(str, Enum):
    PROMPT_CACHE = "prompt_cache"
    STRUCTURED_OUTPUT = "structured_output"
    IMAGES = "images"
    DOCUMENTS = "documents"
    BATCH = "batch"


# Providers that may receive process-routed tenant documents today, per
# docs/DATA_HANDLING.md. Explicit tenant BYOK is a separate, user-authorized
# route and stays authoritative over this process/task registry.
TENANT_DATA_CLEARED: frozenset[str] = frozenset({"anthropic", "ollama"})

# Providers with a process-level transport in messages_create. Google is also
# available process-wide for explicitly configured local/test runs. OpenAI and
# arbitrary compatible endpoints still require active tenant BYOK config.
_PROCESS_PROVIDERS = frozenset(
    {
        LLMProvider.ANTHROPIC,
        LLMProvider.OPENROUTER,
        LLMProvider.NVIDIA,
        LLMProvider.OLLAMA,
        LLMProvider.GOOGLE,
    }
)
_BYOK_PROVIDERS = frozenset(
    {LLMProvider.ANTHROPIC, LLMProvider.OPENAI, LLMProvider.GOOGLE, LLMProvider.COMPATIBLE}
)

# Anthropic's default model per task, delegated to creditpaper.config so the
# ANTHROPIC_EXTRACT_MODEL / ANTHROPIC_REVIEW_MODEL env vars and the geo
# compatibility reasoning stay in that one place. CLASSIFY and STRUCTURAL share
# EXTRACT's tier: both are "look at the document" ingestion passes, not the
# generative judgement PROSE is priced for. ADJUDICATE reuses REVIEW's tier:
# resolving a first-read/second-read disagreement is a fidelity-review task,
# not a drafting one. SUPPORT shares EXTRACT's resolved model/provider;
# SECOND_READ has no entry here — see `_resolve_second_read`.
_ANTHROPIC_DEFAULT_MODEL: dict[Task, Callable[[], str]] = {
    Task.CLASSIFY: config.extract_model,
    Task.STRUCTURAL: config.extract_model,
    Task.EXTRACT: config.extract_model,
    Task.ADJUDICATE: config.review_model,
}

# Capability flags, one comment block per flag, each citing the exact
# `messages_create` branch (src/creditpaper/llm/client.py) that makes the
# claim true or false. These describe what a call THROUGH messages_create can
# actually do on each transport — not the provider's API in general.
#
# PROMPT_CACHE — `cache_system=True` wraps the `system` prompt in a
# `cache_control` block before the Anthropic/OpenRouter call (client.py:566-
# 569). Anthropic and OpenRouter share that exact code path: `shared_client()`
# (client.py:139-175) hands OpenRouter the same `anthropic.Anthropic()` SDK
# instance pointed at OpenRouter's base_url, and `_anthropic_call`
# (client.py:266-292) forwards `system` unchanged either way — confirmed by
# `test_openrouter_request_auth_documents_and_usage` asserting the
# `cache_control` block reaches the wire. Ollama's docstring
# (client.py:390-396) says Anthropic-only kwargs including `cache_control`
# are silently ignored, and `_ollama_call` (client.py:381) never looks at
# `cache_system`. NVIDIA's `_nvidia_call` (client.py:654-699) builds its own
# payload from scratch with no such field.
#
# STRUCTURED_OUTPUT — Anthropic/OpenRouter forward arbitrary kwargs (`tools`,
# `tool_choice`, JSON schemas) straight through to
# `client.messages.create(**kwargs)` (client.py:292); nothing in the
# preprocessing (client.py:565-594) strips them. NVIDIA's `_nvidia_call`
# payload (client.py:671-679) whitelists only
# model/messages/stream/max_tokens/temperature/top_p/stop — `tools` is never
# read, so a caller's schema is silently dropped. Ollama's
# `_anthropic_kwargs_to_ollama_messages` (client.py:448-493) has no
# tools/response_format handling either.
#
# IMAGES — Anthropic/OpenRouter pass an `image` content block through
# unchanged to the wire. Ollama's converter re-emits PDF pages and image
# blocks as base64 `images` entries (client.py:483-493) because qwen2.5-vl
# reads images directly — the model itself sees the pixels, so this is True.
# NVIDIA's `_nvidia_messages` (client.py:603-632) instead runs local OCR
# (`ocr_page`) on any image block and sends only the extracted text — the
# model is never shown the image, so this is False.
#
# DOCUMENTS — Anthropic/OpenRouter pass a `document` (base64 PDF) content
# block through unchanged. NVIDIA's `_nvidia_messages` (client.py:603-632)
# extracts PDF text locally with `extract_pdf` and sends plain text instead —
# the model never receives the document itself. Ollama's converter
# (client.py:448-493) handles only `text` and `image` blocks; a `document`
# block has no case and is dropped, so Ollama never receives one either.
#
# BATCH — no call site in `client.py` ever calls `.messages.batches.create`;
# the choke-point test's `_WIRE_CALLS` forbids that call outside `client.py`,
# and `client.py` itself makes no such call on any provider branch. No
# transport therefore has BATCH via `messages_create`, regardless of what the
# provider's raw API supports.
#
# Tenant BYOK OpenAI/Google/compatible adapters convert PDF document blocks to
# locally rendered images and preserve image blocks; they do not forward prompt
# caching or structured-output controls.
_PROVIDER_CAPABILITIES: dict[LLMProvider, frozenset[Capability]] = {
    LLMProvider.ANTHROPIC: frozenset(
        {Capability.PROMPT_CACHE, Capability.STRUCTURED_OUTPUT, Capability.IMAGES, Capability.DOCUMENTS}
    ),
    LLMProvider.OPENROUTER: frozenset(
        {Capability.PROMPT_CACHE, Capability.STRUCTURED_OUTPUT, Capability.IMAGES, Capability.DOCUMENTS}
    ),
    LLMProvider.NVIDIA: frozenset(),
    # Ollama capabilities are model-dependent and are completed in resolve().
    # Text-only models such as gpt-oss must never receive rendered PDF pages.
    LLMProvider.OLLAMA: frozenset(),
    LLMProvider.OPENAI: frozenset({Capability.IMAGES}),
    LLMProvider.GOOGLE: frozenset({Capability.IMAGES}),
    LLMProvider.COMPATIBLE: frozenset({Capability.IMAGES}),
}


def ollama_model_supports_images(model: str) -> bool:
    """Return whether an installed Ollama model is expected to accept images.

    Operators may explicitly list vision model names through
    ``OLLAMA_VISION_MODELS``.  The conservative built-in detection covers the
    vision families used by this project; unknown models are treated as text
    only so we do not silently send unusable image payloads.
    """
    normalized = model.strip().lower()
    configured = {
        item.strip().lower() for item in os.getenv("OLLAMA_VISION_MODELS", "").split(",") if item.strip()
    }
    if normalized in configured:
        return True
    return any(marker in normalized for marker in ("qwen2.5-vl", "qwen2.5vl", "llava", "minicpm-v"))


# Candidates to be SCORED on the blind set, not assumed correct. `typesafe:jev`
# appears ONLY here — it has no transport in `creditpaper.llm.client`, and
# `resolve()` refuses it (and any other providerless entry) exactly like an
# unknown provider string.
CANDIDATES: dict[Task, tuple[str, ...]] = {
    Task.CLASSIFY: ("typesafe:jev", "anthropic:claude-haiku-4-5-20251001", "ollama:qwen2.5-vl"),
    Task.STRUCTURAL: ("anthropic:claude-haiku-4-5-20251001", "ollama:qwen2.5-vl"),
    Task.EXTRACT: ("anthropic:claude-opus-5", "anthropic:claude-sonnet-5"),
    Task.SECOND_READ: ("nvidia:nvidia/nemotron-3.5-lightning-30b-a3b", "ollama:qwen2.5-vl"),
    Task.ADJUDICATE: ("anthropic:claude-opus-5",),
    # Jev's Choice primitive fits "supports / contradicts / says nothing"; the
    # Ollama entry is the different-family candidate. Both unscored.
    Task.SUPPORT: (
        "anthropic:claude-sonnet-5",
        "anthropic:claude-haiku-4-5-20251001",
        "typesafe:jev",
        "ollama:qwen2.5-vl",
    ),
}


class ModelChoice(BaseModel):
    provider: str
    model: str
    max_cost_usd: Decimal | None = None
    capabilities: frozenset[Capability]


def _parse_override(task: Task, override: str) -> tuple[str, str]:
    if ":" not in override:
        raise ValueError(f"LLM_TASK_{task.name} must be 'provider:model' (with a colon), got {override!r}")
    provider, _, model = override.partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not provider or not model:
        raise ValueError(f"LLM_TASK_{task.name} must include both provider and model, got {override!r}")
    return provider, model


def _known_provider(provider: str, *, task: Task) -> LLMProvider:
    try:
        return LLMProvider(provider)
    except ValueError:
        raise ValueError(
            f"unknown provider {provider!r} for task {task.value}: no transport in llm/client.py"
        ) from None


def _require_tenant_clearance(provider: str, *, task: Task) -> None:
    if provider in TENANT_DATA_CLEARED:
        return
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env in {"local", "test"}:
        return
    raise ValueError(
        f"{provider!r} may not receive tenant data for task {task.value!r} outside local/test: "
        f"no data-processing terms are in writing for it (see docs/DATA_HANDLING.md); "
        f"only {sorted(TENANT_DATA_CLEARED)} are cleared"
    )


def _cost_ceiling(task: Task) -> Decimal | None:
    var = f"LLM_TASK_{task.name}_MAX_USD"
    raw = os.getenv(var)
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{var} must be a decimal number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{var} must be greater than 0, got {raw!r}")
    return value


def _model_family(provider: str, model: str) -> str:
    """The vendor whose weights actually answer the call.

    `openrouter:anthropic/claude-...` is served by Anthropic even though the
    provider field says OpenRouter, so the family is read from the model id's
    vendor prefix when OpenRouter is the provider. Every other provider's
    family is the provider itself: Ollama and NVIDIA each serve one vendor's
    weights regardless of the model string.
    """
    if provider == LLMProvider.OPENROUTER.value:
        vendor, sep, _rest = model.partition("/")
        return vendor.strip().lower() if sep and vendor.strip() else provider
    return provider


def _active_provider_config():
    # Import lazily: client imports the provider enum, while the registry is
    # also used by the client-facing extraction helpers.
    from creditpaper.llm.client import active_provider_config

    return active_provider_config()


def _plain_route(task: Task) -> tuple[str, str | None, bool]:
    """Provider/model source, with tenant BYOK taking precedence over task env."""
    tenant_config = _active_provider_config()
    if tenant_config is not None:
        return str(tenant_config.provider), str(tenant_config.model), True

    override = os.getenv(f"LLM_TASK_{task.name}")
    if override and override.strip():
        provider, model = _parse_override(task, override.strip())
        return provider, model, False
    if task is Task.SUPPORT:
        return _plain_route(Task.EXTRACT)

    # The task registry supplies the process provider when no task override is
    # present. Model defaults remain centralized in config.extract_model() /
    # review_model(), which already interpret NVIDIA, Ollama and OpenRouter env.
    process_provider = current_provider()
    return process_provider.value, None, False


def _resolve_plain(task: Task) -> tuple[str, str]:
    provider, model, _tenant_configured = _plain_route(task)
    if model is None:
        model_task = Task.EXTRACT if task is Task.SUPPORT else task
        model = _ANTHROPIC_DEFAULT_MODEL[model_task]()
    return provider, model


def resolve_provider(task: Task) -> LLMProvider:
    """Provider selected for preprocessing before a task's model call.

    This resolves transport-only work such as choosing a page budget or whether
    PDF pages should be rendered, and applies the same provider/clearance checks
    as ``resolve()`` so an unsupported route is refused before file processing.
    """
    tenant_configured = _active_provider_config() is not None
    if task is Task.SECOND_READ:
        if tenant_configured:
            raise ValueError("SECOND_READ cannot use a task override while tenant BYOK is active")
        provider, _model = _resolve_second_read()
        return _validate_route(provider, task, tenant_configured=False)
    provider, _model, tenant_configured = _plain_route(task)
    return _validate_route(provider, task, tenant_configured=tenant_configured)


def _validate_route(provider: str, task: Task, *, tenant_configured: bool) -> LLMProvider:
    llm_provider = _known_provider(provider, task=task)
    if tenant_configured:
        if llm_provider not in _BYOK_PROVIDERS:
            raise ValueError(f"{provider!r} is not supported by tenant BYOK")
        return llm_provider
    if llm_provider not in _PROCESS_PROVIDERS:
        raise ValueError(f"{provider!r} requires an active tenant BYOK configuration")
    _require_tenant_clearance(provider, task=task)
    return llm_provider


def _resolve_second_read() -> tuple[str, str]:
    override = os.getenv(f"LLM_TASK_{Task.SECOND_READ.name}")
    if not override or not override.strip():
        raise ValueError("SECOND_READ not configured")
    return _parse_override(Task.SECOND_READ, override.strip())


def resolve(task: Task) -> ModelChoice:
    """Resolve a task to a provider, model and capability set.

    `LLM_TASK_<TASK>` (e.g. `LLM_TASK_CLASSIFY=ollama:qwen2.5-vl`) overrides the
    process provider/model for that task only; the value must be
    `"provider:model"` with a colon. Tenant BYOK, when active, remains
    authoritative. Without either override, the process `LLM_PROVIDER` is used
    and model tiers still come from `creditpaper.config`.
    """
    tenant_configured = _active_provider_config() is not None
    if task is Task.SECOND_READ:
        provider, model = _resolve_second_read()
        if tenant_configured:
            raise ValueError("SECOND_READ cannot use a task override while tenant BYOK is active")
    else:
        provider, model = _resolve_plain(task)

    llm_provider = _validate_route(provider, task, tenant_configured=tenant_configured)

    if task is Task.SECOND_READ:
        extract_provider, extract_model_id = _resolve_plain(Task.EXTRACT)
        extract_family = _model_family(extract_provider, extract_model_id)
        second_family = _model_family(provider, model)
        if second_family == extract_family:
            raise ValueError(
                f"SECOND_READ model family {second_family!r} matches EXTRACT's — "
                "configure LLM_TASK_SECOND_READ with a genuinely different model family"
            )

    if not model.strip():
        raise ValueError(f"no model is configured for task {task.value!r}")

    capabilities = _PROVIDER_CAPABILITIES[llm_provider]
    if llm_provider is LLMProvider.OLLAMA and ollama_model_supports_images(model):
        capabilities = capabilities | {Capability.IMAGES}

    return ModelChoice(
        provider=provider,
        model=model,
        max_cost_usd=_cost_ceiling(task),
        capabilities=capabilities,
    )


def supports(choice: ModelChoice, capability: Capability) -> bool:
    return capability in choice.capabilities


# --- compatibility with the devel_jason line -------------------------------
#
# `ingest/dual_read.py` came from the other ingestion line and calls these two.
# Both lines are kept while the merged model is measured (2026-09-23 decision),
# so the registry carries the surface rather than dual_read being rewritten to
# a design that may not survive the comparison.


def default_provider(task: Task) -> str:
    """The provider a task falls back to when nothing is configured.

    Exposed so the SECOND_READ-differs-from-EXTRACT rule can be asserted
    without configuring a model: the rule is a property of the table, not of
    whatever happens to be in the environment.
    """
    return CANDIDATES[task][0].split(":", 1)[0]


def resolve_optional(task: Task) -> ModelChoice | None:
    """`resolve`, but None rather than raising when the task is UNCONFIGURED.

    A deployment with no second model family is a CHOICE — `dual_read` then
    does one read and says so. A MISSPELLED provider is a mistake and must
    still raise, so the absence is detected directly rather than by catching
    whatever `resolve` throws: a blanket `except ValueError` swallowed
    "unknown provider" too, and turned a typo into a silently single-read
    deployment.
    """
    if task is Task.SECOND_READ:
        override = os.getenv(f"LLM_TASK_{task.name}")
        if not override or not override.strip():
            return None
    return resolve(task)

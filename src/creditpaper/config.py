"""Which model runs each stage, in one place.

Six modules called `os.getenv("ANTHROPIC_*_MODEL", "<hardcoded-id>")`, with the
prose default duplicated across three of them. Duplicated defaults drift: a model
migration has to find every copy, and one missed line leaves a stage quietly on
the old tier while the ledger reports the new one — the same class of problem as
the three copies of `OUTPUT_DIR` and the two prose parsers.

Read per call rather than captured at import, so a test or a worker can change a
tier without reloading modules, and so `.env` loaded by a script still applies.
That matters: `scripts/*.py` call `load_dotenv()` at the top of `main()`, which is
AFTER the imports.

Deliberately NOT a settings object with everything in it. Effort and inference geo
stay in `llm/client.py`, at the choke point that applies them, because that is where
their guards live (a pre-4.6 model with `inference_geo` set is a 400, and effort
below a stage's floor stops the stage working). Splitting a rule from its guard is
how the guard gets forgotten.
"""

from __future__ import annotations

import os

from creditpaper.llm.providers import LLMProvider, current_provider

# Tier defaults, and the reasoning behind them (DECISIONS.md 2026-08-13).
#
# PROSE — Opus: thesis, sections and house-style distillation are the generative
# credit judgement that differentiates the output.
#
# EXTRACT — Sonnet: cost is input-dominated (128K-token valuation reports) while
# the task is locate-and-quote, not deep reasoning. Sonnet 5 is also the first
# Sonnet with high-resolution vision, a real accuracy gain on scanned reports.
#
# REVIEW — Sonnet, argued rather than assumed. Promoting it to Opus was considered
# and rejected: the fidelity reviewer's defect is over-strictness from incomplete
# context, not weak reasoning, and Opus over-verifies by default, which would make
# it worse. Every historical improvement came from better context or wiring.
#
# PROOFREAD — Sonnet, not Haiku, solely because Haiku 4.5 rejects `inference_geo`
# and would be the one call unable to be pinned alongside everything else.
_DEFAULTS = {
    "ANTHROPIC_PROSE_MODEL": "claude-opus-5",
    # Haiku 4.5 at temperature 0 (DECISIONS 2026-10-09): measured equal to or better than
    # Sonnet 5 on both blind deals at ~1/10 the cost. It rejects inference_geo (see above).
    "ANTHROPIC_EXTRACT_MODEL": "claude-haiku-4-5-20251001",
    "ANTHROPIC_REVIEW_MODEL": "claude-sonnet-5",
    "ANTHROPIC_PROOFREAD_MODEL": "claude-sonnet-5",
}

# The generation harness's stage models (llm/stages.py). Fixed, not env-tunable:
# generation always runs on these Anthropic models whatever a firm's own key is
# for (DECISIONS 2026-10-07), and the evaluation results apply to them.
GENERATION_OPUS_MODEL = "claude-opus-5-5"
GENERATION_SONNET_MODEL = "claude-sonnet-5-5"


def _model(var: str, override: str | None = None) -> str:
    from creditpaper.llm.client import active_provider_config

    tenant_config = active_provider_config()
    if tenant_config:
        return override or tenant_config.model
    if current_provider() in {LLMProvider.NVIDIA, LLMProvider.GOOGLE}:
        name = "LLM_EXTRACT_MODEL" if var == "ANTHROPIC_EXTRACT_MODEL" else "LLM_PROSE_MODEL"
        value = (override or os.getenv(name) or "").strip()
        if not value:
            raise ValueError(f"{name} is required when LLM_PROVIDER={current_provider().value}")
        return value
    if current_provider() == LLMProvider.OLLAMA:
        name = "LLM_EXTRACT_MODEL" if var == "ANTHROPIC_EXTRACT_MODEL" else "LLM_PROSE_MODEL"
        value = override or os.getenv(name) or ""
        if value and value.strip():
            return value.strip()
    if current_provider() == LLMProvider.OPENROUTER:
        router_var = var.replace("ANTHROPIC_", "OPENROUTER_", 1)
        return override or os.getenv(router_var) or f"anthropic/{_DEFAULTS[var]}"
    return override or os.getenv(var) or _DEFAULTS[var]


def prose_model(override: str | None = None) -> str:
    """Thesis, section drafting, house-style distillation."""
    return _model("ANTHROPIC_PROSE_MODEL", override)


def extract_model(override: str | None = None) -> str:
    """Document extraction."""
    return _model("ANTHROPIC_EXTRACT_MODEL", override)


def review_model(override: str | None = None) -> str:
    """The claims-fidelity reviewer."""
    return _model("ANTHROPIC_REVIEW_MODEL", override)


def proofread_model(override: str | None = None) -> str:
    """Whole-paper spelling and grammar QC."""
    return _model("ANTHROPIC_PROOFREAD_MODEL", override)


def configured_models() -> dict[str, str]:
    """Every stage's resolved model — for the run manifest and the cost summary.

    The architecture spec requires a manifest recording model ids per run, so two
    differing papers can be explained by diffing manifests. That is closer to an
    AFSL requirement than a convenience, and it needs one function that knows the
    whole set.
    """
    return {
        "prose": prose_model(),
        "extract": extract_model(),
        "review": review_model(),
        "proofread": proofread_model(),
    }


# Confidence threshold for extraction routing.
# Facts with confidence below this value are routed to human review queue.
EXTRACTION_CONFIDENCE_THRESHOLD = float(os.getenv("EXTRACTION_CONFIDENCE_THRESHOLD", "0.7"))

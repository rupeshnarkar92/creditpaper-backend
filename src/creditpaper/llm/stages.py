"""Which model each generation stage runs on, and under whose key.

Generation always runs on Anthropic (DECISIONS 2026-10-07): it depends on
prompt caching with explicit breakpoints, `output_config.format` and per-stage
tiering, which other providers drop silently. A firm's own Anthropic key is
used with these stage models; a firm whose own key is for another provider has
that key dropped for generation, so the platform's Anthropic key is used. The
policy is enforced inside `messages_create` (`generation_policy`), so a nested
provider session cannot re-enable a dropped key. Cost is still attributed to
the firm: `provider_session` opens the tenant usage session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from creditpaper.config import GENERATION_OPUS_MODEL as OPUS
from creditpaper.config import GENERATION_SONNET_MODEL as SONNET
from creditpaper.llm.client import generation_policy


class GenStage(StrEnum):
    SPECIALIST = "specialist"
    THESIS = "thesis"
    PLAN = "plan"
    WRITER = "writer"
    COHERENCE = "coherence"
    SUMMARY = "summary"
    REVIEW = "review"
    CLAIMS = "claims"
    CLAIM_CHECK = "claim_check"


STAGE_MODELS: dict[GenStage, str] = {
    GenStage.SPECIALIST: OPUS,
    GenStage.THESIS: OPUS,
    GenStage.PLAN: OPUS,
    GenStage.WRITER: SONNET,
    GenStage.COHERENCE: SONNET,
    GenStage.SUMMARY: SONNET,
    GenStage.REVIEW: OPUS,
    GenStage.CLAIMS: OPUS,
    GenStage.CLAIM_CHECK: OPUS,
}


def model_for(stage: GenStage) -> str:
    is_local = os.getenv("APP_ENV", "").strip().lower() in {"local", "test"}
    if is_local and os.getenv("OLLAMA_GENERATION_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        model = os.getenv("LLM_PROSE_MODEL", "").strip()
        if not model:
            raise ValueError("LLM_PROSE_MODEL is required when local Ollama generation is enabled")
        return model
    if is_local and os.getenv("GOOGLE_GENERATION_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        model = os.getenv("LLM_PROSE_MODEL", "").strip()
        if not model:
            raise ValueError("LLM_PROSE_MODEL is required when local Google generation is enabled")
        return model
    return STAGE_MODELS[stage]


@contextmanager
def generation_provider_scope(tenant_id: str) -> Iterator[None]:
    """Open the tenant's provider session and apply the generation provider policy."""
    from creditpaper.llm.tenant_config import provider_session

    with provider_session(tenant_id), generation_policy():
        yield

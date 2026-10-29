"""The harvester: one window of pages to a model, back as raw fact dicts.

`general.extract_corpus` takes a `Harvester` — `Callable[[str], list[dict]]` —
and everything else about general extraction is deterministic. This module is
the one that talks to a model, so it is the only place the corpus path can
become non-reproducible.

IT LIVES IN `src/` BECAUSE THE MEASURED PATH MUST BE THE SHIPPED PATH. Until
2026-09-23 the only implementation was a local function inside
`scripts/general_extraction_probe.py`, so every number behind this design —
100% path validity, 95.3% verified, $1.18 a deal — was measured against code
production would not have run. The probe now imports this module, which is
what makes those numbers a claim about the product rather than about a script.

Every call goes through `llm/client.py::messages_create`: the truncation guard,
the streaming threshold and the cost ledger live there, and a structural test
fails CI if any other file reaches the SDK directly.

TWO REPLY ROUTES, ONE ANSWER (spec §3.1). On Anthropic the reply is
schema-enforced: `path` is an enum of the field list's templates and the keys
come back separately, so a path outside the list cannot be returned. Every other
provider drops `output_config` silently, so there the same instruction is sent
as text and the reply parsed strictly. The route is chosen per call, from where
`messages_create` will actually send it. On both, a reply that cannot be read
raises `HarvestReplyError`: it used to come back as `[]`, which is also the
answer of a window that states nothing, so a malformed draw silently emptied a
window. The corpus path counts a raising window as unread.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from creditpaper.ingest.general import Harvester
from creditpaper.ingest.harvest_prompt import build_system, build_system_lines, path_templates

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "HarvestPrompt",
    "HarvestReplyError",
    "build_harvester",
    "harvest_prompt",
    "harvest_window",
    "parse_facts",
]

# Thinking and response SHARE this budget on Sonnet 5, so a window that yields
# a lot of facts truncates at a smaller number and the guard in `llm/client.py`
# raises rather than quietly returning half a draw. Half a draw is the failure
# this design cannot tolerate: it looks exactly like a window that states less.
#
# 8000 was not enough. A real document in the production run truncated on it and
# took the whole ingestion down with it. Output tokens are billed on what is
# USED, not on what is reserved, so a headroom that is never reached is free;
# the only thing a small budget buys is this failure.
DEFAULT_MAX_TOKENS = 16000

# A window dense enough to truncate even the raised budget is retried once with
# this much room before it is given up on. Doubling rather than nudging: the
# second attempt exists to succeed, and a window that needs 17k is no more
# likely than one that needs 30k.
RETRY_MAX_TOKENS = 32000

_KEY_SLOT = "[<key>]"


class HarvestReplyError(RuntimeError):
    """The reply could not be read as a harvest answer: refused, malformed, or not the reply object."""


@dataclass(frozen=True)
class HarvestPrompt:
    """Both routes' system prompts and the structured reply schema, built once per field reference."""

    text: str
    structured: str | None
    schema: type[BaseModel] | None


def _reply_schema(templates: Sequence[str]) -> type[BaseModel]:
    item = create_model(
        "HarvestItem",
        __config__=ConfigDict(extra="forbid"),
        path=(Literal[tuple(templates)], ...),  # type: ignore[valid-type]
        keys=(list[str], ...),
        value=(str | float | bool, ...),
        line=(int, ...),
    )
    return create_model(
        "HarvestReply",
        __config__=ConfigDict(extra="forbid"),
        facts=(list[item], ...),  # type: ignore[valid-type]
    )


def harvest_prompt(reference: str, *, line_anchored: bool = True) -> HarvestPrompt:
    """The prompts for one field reference. Only the line-anchored form has a structured route."""
    if not line_anchored:
        return HarvestPrompt(text=build_system(reference), structured=None, schema=None)
    templates = path_templates(reference)
    return HarvestPrompt(
        text=build_system_lines(reference),
        structured=build_system_lines(reference, structured=True),
        schema=_reply_schema(templates) if templates else None,
    )


def parse_facts(text: str) -> list[dict[str, Any]]:
    """The `facts` array out of a text reply, strictly.

    An empty list is a REAL ANSWER — "this window states nothing in the field
    list" — and roughly half the corpus says it. So an unreadable reply must NOT
    return the same empty list: it raises, and the window is counted unread. A
    reply wrapped in prose or a code fence is still read, because the object is
    unambiguous; anything else is not the reply object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise HarvestReplyError("the reply holds no JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HarvestReplyError(f"the reply is not valid JSON ({exc.msg})") from exc
    facts = parsed.get("facts") if isinstance(parsed, dict) else None
    if not isinstance(facts, list):
        raise HarvestReplyError("the reply has no `facts` list")
    return facts


def _filled(template: str, keys: Sequence[str]) -> str:
    """The template with its keys put in, in order; left unfilled (so invalid) when the count is wrong."""
    if template.count(_KEY_SLOT) != len(keys):
        return template
    parts = iter(keys)
    return re.sub(re.escape(_KEY_SLOT), lambda _m: f"[{next(parts)}]", template)


def _structured_facts(data: Any) -> list[dict[str, Any]]:
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        raise HarvestReplyError("the structured reply has no `facts` list")
    out: list[dict[str, Any]] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        keys = item.get("keys") or []
        out.append(
            {
                "path": _filled(str(item.get("path", "")), [str(k) for k in keys]),
                "value": item.get("value"),
                "line": item.get("line"),
            }
        )
    return out


def harvest_window(
    prompt: HarvestPrompt | str,
    window_text: str,
    *,
    model_id: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tool: str = "ingest:corpus",
    provider: Any | None = None,
    temperature: float | None = None,
) -> list[dict[str, Any]]:
    """One window to one model, with ALREADY-BUILT prompts.

    Separate from `build_harvester` so a caller that has its own reason to hold
    the prompt — the probe and the determinism script both build it once and
    reuse it across runs — shares this code rather than reimplementing the call.
    That reimplementation is exactly what let the measured path and the shipped
    path diverge before 2026-09-23. A bare string is a text-route prompt.
    """
    from creditpaper.llm import client as llm_client
    from creditpaper.llm.providers import LLMProvider
    from creditpaper.llm.structured import (
        ModelRefusalError,
        StructuredOutputError,
        StructuredTruncatedError,
        structured_json,
    )

    if isinstance(prompt, str):
        prompt = HarvestPrompt(text=prompt, structured=None, schema=None)
    structured = (
        prompt.structured is not None
        and prompt.schema is not None
        and llm_client.effective_provider(provider) == LLMProvider.ANTHROPIC
    )
    # Sent only when set: production sends none, so a measurement of one value
    # differs from the shipped call in that lever alone.
    sampling = {} if temperature is None else {"temperature": temperature}
    messages = [{"role": "user", "content": window_text}]

    def text_call(budget: int) -> list[dict[str, Any]]:
        resp = llm_client.messages_create(
            **sampling,
            tool=tool,
            stage="extract",
            model=model_id,
            provider=provider,
            max_tokens=budget,
            cache_system=True,
            system=prompt.text,
            messages=messages,
        )
        return parse_facts(getattr(resp, "text", "") or "")

    def structured_call(budget: int) -> list[dict[str, Any]]:
        assert prompt.structured is not None and prompt.schema is not None
        try:
            data = structured_json(
                tool=tool,
                stage="extract",
                schema=prompt.schema,
                system=prompt.structured,
                messages=messages,
                max_tokens=budget,
                model=model_id,
                provider=LLMProvider.ANTHROPIC,
                cache_system=True,
                **sampling,
            )
        except StructuredTruncatedError as exc:
            if isinstance(exc.__cause__, llm_client.TruncatedResponseError):
                raise exc.__cause__ from None
            raise HarvestReplyError(str(exc)) from exc
        except (ModelRefusalError, StructuredOutputError) as exc:
            raise HarvestReplyError(str(exc)) from exc
        return _structured_facts(data)

    call = structured_call if structured else text_call
    try:
        return call(max_tokens)
    except llm_client.TruncatedResponseError:
        # Retried here rather than left to the caller because only this function
        # knows the budget is the cause. The alternative the guard protects
        # against - accepting the truncated text - is still refused: if the
        # second attempt truncates too, the error propagates and the window is
        # recorded as unread rather than read as saying less than it says.
        if max_tokens >= RETRY_MAX_TOKENS:
            raise
        return call(RETRY_MAX_TOKENS)


def build_harvester(
    *,
    model_id: str,
    reference: str,
    line_anchored: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tool: str = "ingest:corpus",
    provider: Any | None = None,
    temperature: float | None = None,
) -> Harvester:
    """A `general.Harvester` bound to one model and one field reference.

    The prompts — the field reference, which is ~2,775 tokens — are built ONCE
    here and marked for caching, so the corpus pays for them at the cache rate on
    every window after the first. That is most of why a deal costs ~$1 rather
    than ~$10.

    `line_anchored` is the default because the model returning a LINE NUMBER
    rather than a quote is what took verification from 79.5% to 92.6%: the
    clause is then cut from page text we already hold, so it anchors by
    construction and the three ways finding a quote fails cannot happen.
    """
    prompt = harvest_prompt(reference, line_anchored=line_anchored)

    def harvest(window_text: str) -> list[dict[str, Any]]:
        return harvest_window(
            prompt,
            window_text,
            model_id=model_id,
            max_tokens=max_tokens,
            tool=tool,
            provider=provider,
            temperature=temperature,
        )

    return harvest

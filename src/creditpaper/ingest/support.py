"""The semantic support check: the third verification leg (Plan C Task 12).

Deterministic verification proves location, figure, unit, currency symbol and
GST basis. It cannot reliably judge polarity ("the limit of $X was not
approved") or role ("Tranche A is $5m and Tranche B is $6m"): pattern lists
either miss real negations or flag ordinary credit wording (DECISIONS.md
2026-09-19). This module asks a model one narrow question instead:

    does this span, read in its context, state that <field> = <value>?

The answer is JSON only - ``supports`` (yes / no / unclear), ``reason_code``
(NEGATED, WRONG_ROLE, DIFFERENT_BASIS, OTHER), ``role`` and ``confidence`` -
validated here, with ONE re-ask on an invalid reply. A second invalid reply is
recorded as "unclear" at confidence 0 (``valid=False``): the fact goes to
review, it is never passed on a reply nobody could read.

The prompt is a versioned file, ``prompts/support/support.v1.txt``. A model may
carry its own override, ``support.v1.<model-slug>.txt`` next to it (spec §5);
``prompt_version`` names the file and a digest of its text, so a recorded
outcome says exactly which prompt produced it.

The model comes from ``llm.registry.resolve(Task.SUPPORT)``: EXTRACT's model by
default, ``LLM_TASK_SUPPORT=provider:model`` to override it per environment.
Every call goes through ``llm.client.messages_create`` (ledger, truncation
guard). A transport error propagates: the queue retries a job, a verifier never
invents an answer.

How ``verify.verify_fact`` uses the outcome (it owns those rules, and a model
can never override a failed anchor or value leg) is in its module docstring.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from creditpaper.ingest.values import value_text
from creditpaper.llm.providers import LLMProvider
from creditpaper.llm.registry import ModelChoice, Task, resolve

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "support"
PROMPT_NAME = "support.v1"
# Room for thinking: on Opus 5 / Sonnet 5 thinking is on by default and shares
# max_tokens with the reply (llm/client.py); 400 truncated on the first eval run.
# ANTHROPIC_EFFORT_SUPPORT is the cost dial.
MAX_TOKENS = 4000
TOOL = "ingest:support"
STAGE = "support"

Supports = Literal["yes", "no", "unclear"]


class SupportReason(str, Enum):
    """Why the span does not (or may not) state the fact."""

    NEGATED = "NEGATED"
    WRONG_ROLE = "WRONG_ROLE"
    DIFFERENT_BASIS = "DIFFERENT_BASIS"
    OTHER = "OTHER"


class SupportOutcome(BaseModel):
    """One answer to the support question, kept on ``FactVerification`` for audit."""

    model_config = ConfigDict(frozen=True)

    supports: Supports
    confidence: float = Field(ge=0, le=1)
    # None for "yes"; always set for "no" and "unclear".
    reason_code: SupportReason | None = None
    # What the text says the value is ("Tranche B limit"), or None.
    role: str | None = None
    # "provider:model".
    model_id: str
    prompt_version: str
    # The model's reply as received (the last one, after a re-ask), for audit.
    answer: str = ""
    # False when neither reply validated: recorded as unclear at confidence 0.
    valid: bool = True
    reasked: bool = False


class SupportChecker(Protocol):
    """A support check bound to one field: what ``verify_fact`` calls."""

    def __call__(self, *, value: object, span_text: str, page_context: str) -> SupportOutcome: ...


class _Answer(BaseModel):
    """The reply's schema. ``extra="forbid"``: a reply with other keys is invalid."""

    model_config = ConfigDict(extra="forbid")

    supports: Supports
    reason_code: SupportReason | None = None
    role: str | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _reason_matches_answer(self) -> _Answer:
        if self.supports == "yes" and self.reason_code is not None:
            raise ValueError('reason_code must be null when supports is "yes"')
        if self.supports != "yes" and self.reason_code is None:
            raise ValueError(f'reason_code is required when supports is "{self.supports}"')
        return self


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", model.lower()).strip("-")


@cache
def _prompt_file(model: str) -> Path:
    own = PROMPT_DIR / f"{PROMPT_NAME}.{_slug(model)}.txt"
    return own if own.is_file() else PROMPT_DIR / f"{PROMPT_NAME}.txt"


def prompt(model: str) -> str:
    """The support prompt for ``model``: its own override, else the shared one."""
    return _prompt_file(model).read_text(encoding="utf-8").strip()


def prompt_version(model: str) -> str:
    """The prompt file's name and a digest of its text: ``support.v1@1a2b3c4d5e6f``."""
    path = _prompt_file(model)
    digest = hashlib.sha256(prompt(model).encode("utf-8")).hexdigest()[:12]
    return f"{path.stem}@{digest}"


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal):
        text = value_text(value)
        if text is not None and re.fullmatch(r"-?\d+", text):
            return f"{int(text):,}"
        return text or str(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _marked_context(span_text: str, page_context: str) -> str:
    """The context with the span marked by <span> tags (appended when not inside)."""
    at = page_context.find(span_text) if span_text else -1
    if at < 0:
        return f"{page_context}\n<span>{span_text}</span>"
    return f"{page_context[:at]}<span>{span_text}</span>{page_context[at + len(span_text) :]}"


def question(
    field_path: str, definition: str, value: object, span_text: str, page_context: str, kind: str | None
) -> str:
    """The user message: the field, the value and the marked document text."""
    unit = f"\nUNIT: {kind}" if kind else ""
    return (
        f"FIELD: {field_path}\n"
        f"DEFINITION: {definition}{unit}\n"
        f"VALUE: {_render_value(value)}\n\n"
        f"<context>\n{_marked_context(span_text, page_context)}\n</context>"
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse(text: str) -> _Answer:
    body = _FENCE.sub("", text.strip())
    return _Answer.model_validate(json.loads(body))


def check_support(
    field_path: str,
    definition: str,
    value: object,
    span_text: str,
    page_context: str,
    *,
    kind: str | None = None,
    task: Task = Task.SUPPORT,
    choice: ModelChoice | None = None,
    client: Any = None,
) -> SupportOutcome:
    """Ask whether ``span_text`` (inside ``page_context``) states ``field_path`` = ``value``.

    ``definition`` says what the field means, in words the model can hold the
    span against. ``choice`` overrides the registry (the evaluation harness uses
    it to score candidates); ``client`` is handed to ``messages_create`` (tests
    pass a fake transport).

    TODO(Plan C Task 9): take a registry ``Concept`` in place of
    ``field_path`` + ``definition`` once the concept registry lands.
    """
    from creditpaper.llm import client as llm_client
    from creditpaper.llm.structured import json_schema_for

    resolved = choice if choice is not None else resolve(task)
    model_id = f"{resolved.provider}:{resolved.model}"
    version = prompt_version(resolved.model)
    system = prompt(resolved.model)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": question(field_path, definition, value, span_text, page_context, kind)}
    ]
    provider = LLMProvider(resolved.provider)
    # Spec §3.1: the reply schema is enforced where it can be (the Anthropic SDK
    # path); every other provider drops `output_config`, so there the strict
    # text parse below is the whole guard. Chosen from where the call will go.
    structured = llm_client.effective_provider(provider) == LLMProvider.ANTHROPIC
    reply_format = (
        {"output_config": {"format": {"type": "json_schema", "schema": json_schema_for(_Answer)}}}
        if structured
        else {}
    )
    refused = False

    def ask() -> str:
        nonlocal refused
        resp = llm_client.messages_create(
            **reply_format,
            client=client,
            tool=TOOL,
            stage=STAGE,
            provider=provider,
            model=resolved.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
        )
        refused = getattr(resp, "stop_reason", None) == "refusal"
        return str(resp.text).strip()

    def unreadable(answer: str, *, reasked: bool) -> SupportOutcome:
        return SupportOutcome(
            supports="unclear",
            confidence=0.0,
            reason_code=SupportReason.OTHER,
            model_id=model_id,
            prompt_version=version,
            answer=answer,
            valid=False,
            reasked=reasked,
        )

    reply = ask()
    if refused:
        # Asking again would get the same refusal; a refusal is never a pass.
        return unreadable(reply, reasked=False)
    try:
        answer = _parse(reply)
        reasked = False
    except (ValueError, ValidationError) as first:  # json.JSONDecodeError is a ValueError
        messages.extend(
            [
                {"role": "assistant", "content": reply or "(empty)"},
                {
                    "role": "user",
                    "content": f"That reply was not valid ({_short(first)}). "
                    "Reply with the one JSON object only, exactly as specified.",
                },
            ]
        )
        reply = ask()
        reasked = True
        if refused:
            return unreadable(reply, reasked=True)
        try:
            answer = _parse(reply)
        except (ValueError, ValidationError):
            return unreadable(reply, reasked=True)
    return SupportOutcome(
        supports=answer.supports,
        confidence=answer.confidence,
        reason_code=answer.reason_code,
        role=(answer.role or "").strip() or None,
        model_id=model_id,
        prompt_version=version,
        answer=reply,
        reasked=reasked,
    )


def _short(exc: Exception) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return first[:200]


def checker_for(
    field_path: str,
    definition: str,
    *,
    kind: str | None = None,
    task: Task = Task.SUPPORT,
    choice: ModelChoice | None = None,
    client: Any = None,
) -> SupportChecker:
    """``check_support`` bound to one field, in the shape ``verify_fact`` calls."""

    def checker(*, value: object, span_text: str, page_context: str) -> SupportOutcome:
        return check_support(
            field_path,
            definition,
            value,
            span_text,
            page_context,
            kind=kind,
            task=task,
            choice=choice,
            client=client,
        )

    return checker

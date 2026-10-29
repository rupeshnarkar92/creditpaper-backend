"""One fixed question per material concept, asked of every window (spec §3.3).

The open harvest lets the model choose what to mention, so measured across
reads a material scalar still goes missing between two reads of the same
documents (DECISIONS 2026-09-24, `MaterialIntact`). Missing is the failure the
corpus bar exists to catch, because a fact that vanishes cannot be corrected.

This pass names every material scalar concept and requires an answer for each:
`{stated: true, value, line}` where the window states it, `stated: false` where not. Its
answers are FOLDED into the window's merged draw before the draw is cached:

- a path the open draws missed is added, counted as one read of the passes, so
  it arrives at the minority confidence band and an analyst sees it;
- a path read the same way is left alone;
- a path read differently is CONTESTED (the material value joins
  `conflicting`), never silently resolved either way;
- a null is kept as a marker (`value` None, so nothing downstream reads it as a
  fact) so the run can say which material concepts no window stated.

Same two routes as the harvest: schema-enforced on Anthropic, strict text
elsewhere. An unreadable reply raises `MaterialReplyError`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from creditpaper.model.concepts import REGISTRY, Concept

__all__ = [
    "MATERIAL_MAX_TOKENS",
    "MaterialRead",
    "MaterialReader",
    "MaterialReplyError",
    "build_material_reader",
    "fold",
    "material_system",
    "material_targets",
    "read_material",
]

# 38 targets answer mostly null; a dense window states a dozen. Billed on use.
MATERIAL_MAX_TOKENS = 4000
_TOOL = "ingest:material"


class MaterialReplyError(RuntimeError):
    """The material reply could not be read: refused, malformed, or not the reply object."""


@dataclass(frozen=True)
class MaterialRead:
    """One window's answers: facts in the harvest item shape, and which targets it did not state."""

    facts: list[dict[str, Any]] = field(default_factory=list)
    not_stated: frozenset[str] = frozenset()
    asked: frozenset[str] = frozenset()


MaterialReader = Callable[[str], MaterialRead]


@cache
def material_targets() -> tuple[Concept, ...]:
    """Every material SCALAR concept, in path order. Collections are a recall question, not this one."""
    return tuple(
        sorted((c for c in REGISTRY.values() if c.material and "[" not in c.path), key=lambda c: c.path)
    )


class _Answer(BaseModel):
    """One concept's answer. A FIXED shape, no unions: the API compiles at most 16
    union-typed parameters (`llm.structured.MAX_UNION_PARAMETERS`) and there are 38
    concepts, so "not stated" is `stated: false`, not null. The value is the text as
    stated; the corpus path types it from the field reference."""

    model_config = ConfigDict(extra="forbid")

    stated: bool
    value: str
    line: int


@cache
def _schema() -> type[BaseModel]:
    fields: dict[str, Any] = {
        f"f{i}": (_Answer, Field(..., alias=c.path)) for i, c in enumerate(material_targets())
    }
    return create_model(
        "MaterialReply",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        **fields,
    )


def material_system(*, structured: bool) -> str:
    """The instruction: one line per target, with its definition so the model holds the value to it."""
    listed = "\n".join(f"- {c.path} ({c.kind}): {c.definition}" for c in material_targets())
    shape = (
        '{"stated": true, "value": "<the value as stated>", "line": <its line number>}, or '
        '{"stated": false, "value": "", "line": 0} where these pages do not state it.'
    )
    reply = (
        f"Reply in the required format: every field above, each {shape}"
        if structured
        else "Return one JSON object, and nothing else, with EVERY field above as a key; "
        f"each value is {shape}"
    )
    return (
        "You read commercial property finance documents for a lender.\n\n"
        "You will be given some consecutive pages from ONE document, with every line numbered as "
        "`N| text`. For EACH field below, say whether these pages state it.\n\n"
        f"FIELDS\n{listed}\n\n"
        "RULES\n"
        "- Answer every field. Where these pages state the field, give the value as stated and the "
        "number of the line the value appears on. Where they do not, answer stated false.\n"
        "- Only what the pages STATE. Do not infer, calculate or carry over knowledge. A value that "
        "is stated of something else (a different facility, a superseded figure) is not this field.\n"
        "- The line number must be one shown in the input.\n\n"
        f"{reply}"
    )


def _read_object(data: Any, *, all_asked: bool) -> MaterialRead:
    if not isinstance(data, dict):
        raise MaterialReplyError("the reply is not a JSON object")
    targets = {c.path for c in material_targets()}
    facts: list[dict[str, Any]] = []
    not_stated: set[str] = set()
    asked: set[str] = set()
    for path in sorted(targets):
        if path not in data:
            if all_asked:
                raise MaterialReplyError(f"the reply has no answer for {path}")
            continue
        asked.add(path)
        answer = data[path]
        stated = isinstance(answer, dict) and answer.get("stated", True) is not False
        if (
            stated
            and isinstance(answer, dict)
            and answer.get("value") not in (None, "")
            and isinstance(answer.get("line"), int)
        ):
            facts.append({"path": path, "value": answer["value"], "line": answer["line"]})
        else:
            not_stated.add(path)
    return MaterialRead(facts=facts, not_stated=frozenset(not_stated), asked=frozenset(asked))


def _text_object(text: str) -> Any:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise MaterialReplyError("the reply holds no JSON object")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MaterialReplyError(f"the reply is not valid JSON ({exc.msg})") from exc


def read_material(
    window_text: str,
    *,
    model_id: str,
    provider: Any | None = None,
    max_tokens: int = MATERIAL_MAX_TOKENS,
    tool: str = _TOOL,
    temperature: float | None = None,
) -> MaterialRead:
    """Ask one window every material question. Raises `MaterialReplyError` when the reply cannot be read."""
    from creditpaper.llm import client as llm_client
    from creditpaper.llm.providers import LLMProvider
    from creditpaper.llm.structured import StructuredOutputError, structured_json

    messages = [{"role": "user", "content": window_text}]
    # Sent only when set, as on the harvest: production sends none.
    sampling: dict[str, Any] = {} if temperature is None else {"temperature": temperature}
    if llm_client.effective_provider(provider) == LLMProvider.ANTHROPIC:
        try:
            data = structured_json(
                tool=tool,
                stage="extract",
                schema=_schema(),
                system=material_system(structured=True),
                messages=messages,
                max_tokens=max_tokens,
                model=model_id,
                provider=LLMProvider.ANTHROPIC,
                cache_system=True,
                temperature=temperature,
            )
        except StructuredOutputError as exc:
            raise MaterialReplyError(str(exc)) from exc
        return _read_object(data, all_asked=True)
    resp = llm_client.messages_create(
        **sampling,
        tool=tool,
        stage="extract",
        model=model_id,
        provider=provider,
        max_tokens=max_tokens,
        cache_system=True,
        system=material_system(structured=False),
        messages=messages,
    )
    return _read_object(_text_object(getattr(resp, "text", "") or ""), all_asked=False)


def build_material_reader(
    *, model_id: str, provider: Any | None = None, temperature: float | None = None
) -> MaterialReader:
    """A reader bound to one model, for `general.extract_corpus(material_reader=...)`."""

    def reader(window_text: str) -> MaterialRead:
        return read_material(window_text, model_id=model_id, provider=provider, temperature=temperature)

    return reader


def fold(merged: Sequence[dict[str, Any]], read: MaterialRead, *, passes: int) -> list[dict[str, Any]]:
    """The window's merged draw with the material answers folded in (see the module docstring)."""
    from creditpaper.ingest.consensus import _key

    out = [dict(item) for item in merged]
    by_path = {str(item.get("path")): item for item in out if item.get("value") is not None}
    for fact in read.facts:
        path, value = fact["path"], fact["value"]
        existing = by_path.get(path)
        if existing is None:
            added = {
                "path": path,
                "value": value,
                "line": fact["line"],
                "seen_in": 1,
                "passes": max(1, passes),
                "conflicting": [],
                "material": "stated",
            }
            out.append(added)
            by_path[path] = added
            continue
        existing["material"] = "stated"
        if _key(existing["value"]) == _key(value):
            continue
        conflicting = list(existing.get("conflicting") or [])
        if all(_key(c) != _key(value) for c in conflicting):
            conflicting.append(value)
        existing["conflicting"] = conflicting
    for path in sorted(read.not_stated - set(by_path)):
        out.append({"path": path, "value": None, "material": "not_stated"})
    return out

"""A schema-constrained model call that returns a validated object or raises.

Every generation stage that needs typed output calls this, never
`messages_create` directly with a hand-built `output_config`. The contract is
binary on purpose: a parsed, pydantic-validated instance of `schema`, or an
exception from the `StructuredOutputError` family. A refusal, a truncated reply,
malformed JSON or a reply that does not match the schema all raise — the
failure mode this replaces (ingestion's `parse_facts` returning `[]` on bad
JSON) made a broken reply indistinguishable from an empty one.

`structured_json` stops one step earlier and returns the canonicalised reply,
for callers that validate item by item (a specialist's contribution drops a bad
finding instead of failing the domain, spec §8).

The API's structured-output grammar does not support numeric or length bounds,
recursion, `minItems` > 1 or free-form maps, so bounds are stripped from the
schema sent (pydantic re-checks them afterwards) and a free-form map is refused
outright rather than closed to `{}` and silently emptied. Field names
`reasoning` and `thinking` are refused: they can trigger a reasoning-extraction
refusal on these models.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from creditpaper.llm import client as _client
from creditpaper.llm.client import ContextWindowExceededError, TruncatedResponseError
from creditpaper.llm.providers import LLMProvider

T = TypeVar("T", bound=BaseModel)

_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength")
_FORBIDDEN_FIELDS = frozenset({"reasoning", "thinking"})


class StructuredOutputError(RuntimeError):
    """The model's reply could not be turned into the requested schema."""


class ModelRefusalError(StructuredOutputError):
    """The model refused (`stop_reason == "refusal"`)."""


class StructuredTruncatedError(StructuredOutputError):
    """The reply was cut off (max_tokens) or the input did not fit the context window."""


def _clean_node(node: Any, path: str) -> None:
    """Reduce one schema node to what the grammar supports.

    Walks the schema's STRUCTURE — property values, `items`, combinators,
    `$defs` — and never treats a `properties` map as a schema node, so a field
    that happens to be named `minimum` or `minItems` is left alone.
    """
    if not isinstance(node, dict):
        return
    for key in _UNSUPPORTED:
        node.pop(key, None)
    min_items = node.get("minItems")
    if isinstance(min_items, int) and min_items > 1:
        node.pop("minItems")
    props = node.get("properties")
    if isinstance(props, dict) or node.get("type") == "object":
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            raise ValueError(f"{path or 'schema'} is a free-form map, which structured output cannot express")
        node["additionalProperties"] = False
        for name, sub in (props or {}).items():
            if name in _FORBIDDEN_FIELDS:
                raise ValueError(f"schema field {name!r} is not allowed: it can trigger a refusal")
            _clean_node(sub, f"{path}.{name}" if path else name)
    if isinstance(node.get("items"), dict):
        _clean_node(node["items"], f"{path}[]")
    for combinator in ("anyOf", "allOf", "oneOf"):
        for sub in node.get(combinator, []):
            _clean_node(sub, path)
    for name, sub in node.get("$defs", {}).items():
        _clean_node(sub, name)


# Anthropic refuses a schema with more union-typed parameters than this
# ("exponential compilation cost"; measured 2026-10-08 on the material pass,
# whose 38 nullable answers failed every window of a deal).
MAX_UNION_PARAMETERS = 16


def _union_parameters(node: Any) -> int:
    """Properties, anywhere in the schema, typed as a union (`anyOf` or a type array)."""
    if not isinstance(node, dict):
        return 0
    count = 0
    for sub in (node.get("properties") or {}).values():
        if isinstance(sub, dict) and ("anyOf" in sub or isinstance(sub.get("type"), list)):
            count += 1
        count += _union_parameters(sub)
    if isinstance(node.get("items"), dict):
        count += _union_parameters(node["items"])
    for combinator in ("anyOf", "allOf", "oneOf"):
        for sub in node.get(combinator, []):
            count += _union_parameters(sub)
    for sub in node.get("$defs", {}).values():
        count += _union_parameters(sub)
    return count


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema sent to the API for `model`, reduced to what the grammar supports.

    Refused here, before any call, when it has more union-typed parameters than the
    API compiles (`MAX_UNION_PARAMETERS`)."""
    schema = model.model_json_schema()
    _clean_node(schema, "")
    unions = _union_parameters(schema)
    if unions > MAX_UNION_PARAMETERS:
        raise ValueError(
            f"{model.__name__} has {unions} union-typed parameters; the API compiles at most "
            f"{MAX_UNION_PARAMETERS}. Use a fixed shape (e.g. a `stated` flag) instead of nullable fields."
        )
    return schema


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        resolved: dict[str, Any] = root["$defs"][ref.split("/")[-1]]
        return resolved
    return node


def _canonicalise(value: Any, node: dict[str, Any], root: dict[str, Any]) -> Any:
    """Fold enum strings to their declared case, guided by the schema.

    Only positions the schema declares as enums are touched, so free text that
    happens to equal an enum word is left alone.
    """
    node = _resolve(node, root)
    for branch in node.get("anyOf", []) + node.get("allOf", []):
        value = _canonicalise(value, branch, root)
    if isinstance(value, str) and "enum" in node:
        lookup = {str(member).strip().casefold(): member for member in node["enum"]}
        return lookup.get(value.strip().casefold(), value)
    if isinstance(value, dict):
        props = node.get("properties", {})
        return {k: _canonicalise(v, props[k], root) if k in props else v for k, v in value.items()}
    if isinstance(value, list) and isinstance(node.get("items"), dict):
        return [_canonicalise(v, node["items"], root) for v in value]
    return value


def structured_json(
    *,
    tool: str,
    stage: str,
    schema: type[BaseModel],
    system: list[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    model: str,
    client: Any = None,
    effort: str | None = None,
    provider: LLMProvider = LLMProvider.ANTHROPIC,
    cache_system: bool = False,
    temperature: float | None = None,
) -> Any:
    """Call the model with `schema` enforced; return the parsed, enum-canonicalised reply."""
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": json_schema_for(schema)}}
    if effort:
        output_config["effort"] = effort
    try:
        # Through the module, so a test or a wrapper that replaces `messages_create`
        # reaches structured calls too.
        sampling: dict[str, Any] = {} if temperature is None else {"temperature": temperature}
        resp = _client.messages_create(
            **sampling,
            tool=tool,
            stage=str(stage),
            provider=provider,
            client=client,
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            output_config=output_config,
            cache_system=cache_system,
        )
    except (TruncatedResponseError, ContextWindowExceededError) as exc:
        raise StructuredTruncatedError(f"{tool}: {exc}") from exc
    if resp.stop_reason == "refusal":
        raise ModelRefusalError(f"{tool}: the model refused to answer")
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"{tool}: reply was not valid JSON ({exc.msg})") from exc
    full = schema.model_json_schema()
    return _canonicalise(data, full, full)


def structured_call(
    *,
    tool: str,
    stage: str,
    schema: type[T],
    system: list[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    model: str,
    client: Any = None,
    effort: str | None = None,
    provider: LLMProvider = LLMProvider.ANTHROPIC,
    cache_system: bool = False,
) -> T:
    """Call the model with `schema` enforced; return a validated instance or raise."""
    data = structured_json(
        tool=tool,
        stage=stage,
        schema=schema,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        model=model,
        client=client,
        effort=effort,
        provider=provider,
        cache_system=cache_system,
    )
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"{tool}: reply did not match {schema.__name__} ({exc.error_count()} errors)"
        ) from exc

"""The single write path onto a DealModel (DECISIONS.md 2026-06-11).

Extractors emit ExtractionOutput; this module applies it. Nothing else writes
deal-model facts — the merge semantics that must be uniform across five
independently built extractor instances live here, once:

- path-grammar validation (malformed paths are rejected at write time)
- keyed-collection entry creation (`sop[j_smith].assets[home].value` creates
  the SopStatement and SopAsset on first write)
- the confirming-document upgrade (mode=CONFIRM): an agreeing fact moves the
  existing field `extracted` -> `confirmed` with provenance upgraded to the
  confirming document; a diverging fact moves it to `flagged`, preserving the
  original value, with both values recorded. Building contract -> lawyers'
  review, ITS -> loan docs, and valuation -> firm testing all reuse this.
- one model_version bump per apply that changes anything, so snapshotting and
  "what changed since the analyst edited" key off the version.

D4 holds at this choke point: Fact's own validator refuses extracted/confirmed
status without provenance, so an anchorless fact cannot enter the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from creditpaper.model.citations import is_valid_path
from creditpaper.model.core import Fact, FactStatus, ProseBlock
from creditpaper.model.deal import DealModel
from creditpaper.obs.timing import timed


@dataclass
class ExtractionOutput:
    """What an extractor emits: Facts keyed by dotted deal-model path, plus
    extractor-level flags. Defined here (not in extractors/) because it is the
    contract between extractors and the model's single write path."""

    fields: dict[str, Fact]
    flags: list[str]


class ApplyMode(str, Enum):
    EXTRACT = "extract"  # normal extractor output
    CONFIRM = "confirm"  # confirming document (lawyers' review, firm testing)


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)  # divergences (CONFIRM mode)
    divergences: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # path -> (held, confirming)
    rejected: dict[str, str] = field(default_factory=dict)  # path -> reason
    routed_to_review: list[str] = field(default_factory=list)  # low-confidence facts sent to review queue
    new_model_version: int | None = None

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.confirmed or self.flagged)


def _dict_value_type(parent: BaseModel, name: str) -> type | None:
    """The declared value type of a dict-typed model field, e.g.
    sop -> SopStatement, values_by_basis -> Fact[Decimal]."""
    info = type(parent).model_fields.get(name)
    if info is None or get_origin(info.annotation) is not dict:
        return None
    return get_args(info.annotation)[1]


def _is_fact_type(t: type | None) -> bool:
    return isinstance(t, type) and issubclass(t, Fact)


def _declared_value_type(ann: object) -> type | None:
    """The `T` of a declared `Fact[T]`, or None when it cannot be determined.

    `Fact[Decimal]` is a concrete pydantic subclass, not a `typing` alias, so
    `get_args` returns `()` on it — reading the parameter from there makes this
    function silently return None and the whole check a no-op that still passes
    every test. The parameter lives in `__pydantic_generic_metadata__`, the same
    place `scripts/gen_field_reference.py` reads it from.
    """
    meta = getattr(ann, "__pydantic_generic_metadata__", None)
    args = (meta or {}).get("args") or get_args(ann)
    return args[0] if args and isinstance(args[0], type) else None


def _coerce_to_declared(expected: type, value: Any) -> tuple[Any, str | None]:
    """Coerce `value` to the schema's declared type, or refuse it.

    This is the only place the schema is enforced on the way in. Without it
    `apply` accepted a `Fact(value="100")` into a `Fact[Decimal]` field and
    reported nothing rejected, after which `derive` computed `"100" + "50"` as
    `"10050"` and stored it as a citable DERIVED total — a wrong number with
    full provenance, which is the worst artefact this system can produce.

    Coercion is permitted only where it is lossless and where the alternative
    would silently discard a correct fact. The important case is `Fact[str]`
    fields: `add_fact` parses any numeric-looking string to `Decimal`, so a
    licence or title number arrives as a `Decimal` and strict rejection would
    delete it. Refusal is reserved for conversions that would change the value or
    the meaning — a float into a money field, a string into a number.
    """
    if value is None:
        return value, None
    if isinstance(expected, type) and issubclass(expected, Enum):
        if isinstance(value, expected):
            return value, None
        try:  # a bare string is what the model returns; normalise it here, once
            return expected(value), None
        except ValueError:
            pass
        # Formatting only ("Residual Stock", "residual-stock"): case, spacing and
        # hyphens are not meaning. A different meaning is still refused below.
        wording = re.sub(r"[\s-]+", "_", str(value).strip()).casefold()
        try:
            return expected(wording), None
        except ValueError:
            allowed = ", ".join(str(m.value) for m in expected)
            return value, f"{value!r} is not a valid {expected.__name__} (expected one of: {allowed})"
    if expected is bool:
        if isinstance(value, bool):
            return value, None
        return value, f"expected bool, got {type(value).__name__} ({value!r})"
    if expected is int:
        if isinstance(value, bool):  # bool is a subclass of int; never silently an int
            return value, "expected int, got bool"
        if isinstance(value, int):
            return value, None
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value), None
        return value, f"expected int, got {type(value).__name__} ({value!r})"
    if expected is Decimal:
        if isinstance(value, bool):
            return value, "expected Decimal, got bool"
        if isinstance(value, Decimal):
            return value, None
        if isinstance(value, int):  # lossless
            return Decimal(value), None
        # A float in a money field is a rounding bug waiting to happen, and a str
        # is an unparsed extraction. Both are refusals, not coercions.
        return value, f"expected Decimal, got {type(value).__name__} ({value!r})"
    if expected is str:
        if isinstance(value, str):
            return value, None
        if isinstance(value, (int, Decimal)):
            return str(value), None
        return value, f"expected str, got {type(value).__name__} ({value!r})"
    if expected is date:
        if isinstance(value, date):
            return value, None
        if isinstance(value, str):
            try:
                return date.fromisoformat(value), None
            except ValueError:
                return value, f"expected an ISO date, got {value!r}"
        return value, f"expected date, got {type(value).__name__} ({value!r})"
    # An unmodelled declared type: let pydantic be the authority rather than
    # inventing a rule here.
    return value, None


def _validated(fact: Fact, declared: object, path: str) -> tuple[Fact | None, str | None]:
    """`fact` conformed to the declared `Fact[T]`, or (None, reason)."""
    expected = _declared_value_type(declared)
    if expected is None:
        return fact, None
    value, err = _coerce_to_declared(expected, fact.value)
    if err is not None:
        return None, f"{path}: {err}"
    if value is fact.value:
        return fact, None
    return fact.model_copy(update={"value": value}), None


def _set_fact(model: DealModel, path: str, fact: Fact) -> str | None:
    """Place `fact` at `path`, creating keyed-collection entries along the way.
    Returns an error message, or None on success."""
    obj: Any = model
    parts = path.split(".")
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        name, _, bracket = part.partition("[")
        key = bracket[:-1] if bracket else None

        if isinstance(obj, dict) or not hasattr(obj, name):
            return f"unknown segment '{name}' (not in the frozen schema — escalate via DECISIONS.md)"
        if key is None:
            target = getattr(obj, name)
            if last:
                if not isinstance(target, Fact):
                    return f"'{name}' is not a Fact — a path must terminate on a Fact"
                info = type(obj).model_fields.get(name)
                checked, err = _validated(fact, info.annotation if info else None, path)
                if err is not None or checked is None:
                    return err or f"{path}: value does not match the schema"
                setattr(obj, name, checked)
                return None
            obj = target
            continue

        # keyed segment: the attribute must be a keyed collection
        coll = getattr(obj, name)
        value_type = _dict_value_type(obj, name)
        if not isinstance(coll, dict) or value_type is None:
            return f"'{name}' is not a keyed collection"
        if last:
            if not _is_fact_type(value_type):
                return f"'{name}[{key}]' is a block, not a Fact — path must go deeper"
            checked, err = _validated(fact, value_type, path)
            if err is not None or checked is None:
                return err or f"{path}: value does not match the schema"
            coll[key] = checked
            return None
        if key not in coll:
            if _is_fact_type(value_type):
                return f"'{name}[{key}]' is a Fact — nothing to descend into"
            coll[key] = value_type()
        obj = coll[key]
    return None


def _resolve_existing(model: DealModel, path: str) -> Fact | None:
    """The Fact currently at `path`, if the path resolves to one."""
    from creditpaper.model.citations import resolve_path

    target = resolve_path(model, path)
    return target if isinstance(target, Fact) else None


@timed("validation")
def apply(
    model: DealModel,
    output: ExtractionOutput,
    mode: ApplyMode = ApplyMode.EXTRACT,
    *,
    tenant_id: str | None = None,
    deal_id: str | None = None,
) -> ApplyResult:
    """Apply extractor output to the model. Per-path failures are collected in
    `rejected`, never raised — one bad path must not lose a document's other
    facts. Bumps model_version once if anything changed.

    When tenant_id and deal_id are provided, low-confidence facts are routed
    to the review queue instead of being applied directly."""
    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD
    from creditpaper.review.service import route_to_review

    result = ApplyResult()
    for path, fact in output.fields.items():
        if not is_valid_path(path):
            result.rejected[path] = "malformed path (see citation grammar, model/citations.py)"
            continue

        # Confidence routing: low-confidence facts go to review queue
        if (
            mode == ApplyMode.EXTRACT
            and tenant_id is not None
            and deal_id is not None
            and fact.confidence is not None
            and fact.confidence < EXTRACTION_CONFIDENCE_THRESHOLD
        ):
            queue_id = route_to_review(
                tenant_id=tenant_id,
                deal_id=deal_id,
                field_path=path,
                document_id=fact.provenance.document_id if fact.provenance else "unknown",
                extracted_value=fact.value,
                confidence=fact.confidence,
                page=fact.provenance.page if fact.provenance else None,
            )
            if queue_id:
                result.routed_to_review.append(path)
                continue  # skip applying to model until human reviews

        if mode == ApplyMode.CONFIRM:
            if fact.provenance is None:
                result.rejected[path] = "confirming fact must carry provenance"
                continue
            existing = _resolve_existing(model, path)
            if existing is not None and existing.status in (
                FactStatus.EXTRACTED,
                FactStatus.CONFIRMED,
            ):
                if existing.value == fact.value:
                    upgraded = existing.model_copy(
                        update={"status": FactStatus.CONFIRMED, "provenance": fact.provenance}
                    )
                    error = _set_fact(model, path, upgraded)
                    if error:
                        result.rejected[path] = error
                    else:
                        result.confirmed.append(path)
                else:
                    # Divergence: original value stands, field is flagged, both
                    # values preserved for the disclosure (check #17 picks it up).
                    flagged = existing.model_copy(update={"status": FactStatus.FLAGGED})
                    error = _set_fact(model, path, flagged)
                    if error:
                        result.rejected[path] = error
                    else:
                        result.flagged.append(path)
                        result.divergences[path] = (existing.value, fact.value)
                continue
            # nothing to confirm against: fall through and apply as written

        error = _set_fact(model, path, fact)
        if error:
            result.rejected[path] = error
        else:
            result.applied.append(path)

    if result.changed:
        model.model_version += 1
        result.new_model_version = model.model_version
    return result


def set_prose(model: DealModel, section: int, block: ProseBlock) -> None:
    """Write one generated section into the model. The only sanctioned way.

    `model.prose[9] = "some text"` succeeded. Pydantic validates a field on
    assignment, not the mutation of a dict it holds, so a bare string went in
    unchallenged — and with it went `ProseBlock`'s constructor validator, which
    is where **D8** lives: the section 9 recommendation must be human-authored,
    and the block cannot reach APPROVED while the body states one. That is an
    AFSL audit-trail decision, and it was bypassable by assignment.

    So this is not defensive typing. `isinstance` is the check that D8 cannot be
    skipped by writing prose the easy way, and `tests/test_write_path.py` fails
    CI if anything assigns into `model.prose` directly again.

    Deliberately does NOT bump `model_version`: that counts fact changes, and
    conflating prose regeneration with a change to the factual record would make
    the version useless for the thing it exists for.
    """
    if not isinstance(block, ProseBlock):
        raise TypeError(
            f"section {section}: expected a ProseBlock, got {type(block).__name__}. "
            "Constructing one is what enforces D8 (the section 9 recommendation is "
            "human-authored); assigning anything else skips that check."
        )
    model.prose[section] = block


def clear_facts_for_document(model: DealModel, document_id: str) -> int:
    """Clear all facts whose provenance.document_id matches the given document_id.

    Sets matching facts to MISSING status with value=None and provenance=None.
    Returns the number of facts cleared.

    This is called when a document is deleted so the model stays consistent with
    the documents that actually exist (D4: no anchor, no field).
    """
    count = [0]

    def _clear(obj: object) -> None:
        if isinstance(obj, Fact):
            if (
                obj.provenance is not None
                and obj.provenance.document_id == document_id
                and obj.status in (FactStatus.EXTRACTED, FactStatus.CONFIRMED, FactStatus.FLAGGED)
            ):
                obj.value = None
                obj.provenance = None
                obj.confidence = None
                obj.status = FactStatus.MISSING
                count[0] += 1
            return
        if isinstance(obj, BaseModel) and hasattr(type(obj), "model_fields"):
            for name in type(obj).model_fields:
                _clear(getattr(obj, name))
            return
        if isinstance(obj, dict):
            for val in obj.values():
                _clear(val)
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _clear(item)

    _clear(model)
    return count[0]

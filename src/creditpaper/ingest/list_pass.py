"""Targeted list passes: every item of a list, from the pages that hold it.

Measured 2026-10-08/09 on two deals: the figures a signed credit paper cites
that ingestion missed were mostly ITEMS OF LISTS - comparable sales, the
subject's unit schedule, presales, funding lines, covenants. In the open harvest
the model chooses which items to mention, so the rest are lost and which ones
changes between reads. A valuation may tabulate its comparables or narrate them
one by one, so a table reader alone cannot close it.

A list pass is per list and per document type:

- it reads only the pages of the documents filed in the list's slots that
  mention the list (`select_pages`), so it costs cents, not dollars;
- it asks for EVERY item, in a fixed shape: text fields, empty when not stated,
  plus the line the item sits on. No nullable fields (the API compiles at most
  16 union-typed parameters per schema);
- its answers become harvest-shaped facts `{path, value, line}`, so they go
  through the corpus path's verification exactly like any other draw;
- for a document it covers, it is the one source of that collection: the open
  harvest's rows for the same collection in the same document are replaced
  (`merge`), because two sources keyed differently would describe one comparable
  as two.

Same two routes as the harvest: schema-enforced on Anthropic, strict text elsewhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from creditpaper.ingest.corpus import Page
from creditpaper.model.apply import ExtractionOutput
from creditpaper.model.citations import slugify

__all__ = [
    "FIELD_SETS",
    "LISTS",
    "FieldSetSpec",
    "ListSpec",
    "field_set_harvester",
    "list_harvester",
    "merge",
    "select_pages",
]

LIST_MAX_TOKENS = 8000
# A list on one document rarely spans more pages than this; more is a cost guard, not a cap on truth.
MAX_PAGES_PER_DOCUMENT = 16


@dataclass(frozen=True)
class ListSpec:
    name: str
    noun: str  # what one item is, for the instruction
    slots: Mapping[str, str]  # slot key -> the collection path its items go under
    mentions: re.Pattern[str]  # a page qualifies when it mentions the list
    key_field: str
    # item field -> (path below the item, what the field is)
    fields: Mapping[str, tuple[str, str]]
    # Leaves that identify an item whatever a read keyed it as (a comparable's price):
    # rows of two reads with the same identity are one row (`dedupe`). Empty: never merged.
    identity: tuple[str, ...] = ()

    def collection_for(self, slot: str) -> str:
        return self.slots[slot]

    def reply_schema(self) -> type[BaseModel]:
        item_fields: dict[str, Any] = {self.key_field: (str, ...)}
        item_fields.update({name: (str, ...) for name in self.fields})
        item_fields["line"] = (int, ...)
        item = create_model(f"{self.name.title()}Item", __config__=ConfigDict(extra="forbid"), **item_fields)
        return create_model(
            f"{self.name.title()}Reply",
            __config__=ConfigDict(extra="forbid"),
            items=(list[item], ...),  # type: ignore[valid-type]
        )

    def instruction(self, *, structured: bool) -> str:
        described = "\n".join(f"- {name}: {what}" for name, (_, what) in self.fields.items())
        shape = (
            "Reply in the required format."
            if structured
            else 'Return JSON, and nothing else: {"items": [{...one object per item...}]}.'
        )
        return (
            "You read commercial property finance documents for a lender.\n\n"
            "You will be given pages from ONE document, every line numbered as `N| text`. "
            f"List EVERY {self.noun} these pages state - every one, not a selection.\n\n"
            f"For each, give:\n- {self.key_field}: how the document identifies it (address, lot, name)\n"
            f"{described}\n- line: the number of the line the {self.noun} is stated on\n\n"
            "RULES\n"
            "- Copy each value as the document states it. Do not infer, calculate or carry over knowledge.\n"
            '- A field these pages do not state for an item is "" (empty).\n'
            f"- If these pages state no {self.noun}, return no items.\n\n"
            f"{shape}"
        )


LISTS: dict[str, ListSpec] = {
    spec.name: spec
    for spec in (
        ListSpec(
            name="comparables",
            noun="comparable sale",
            slots={"valuation": "valuation.comp_set"},
            mentions=re.compile(r"comparable|sales?\s+evidence|recent\s+sales|\bsold\b|sale\s+price", re.I),
            key_field="label",
            fields={
                "sale_price": ("sale_price", "the sale price"),
                "sale_date": ("sale_date", "the sale date, as written"),
                "land_area": ("land_area", "the land or site area in sqm, for a site sale"),
                "internal_area": ("gfa_nla", "the internal or floor area in sqm, for a completed product"),
                "rate_per_sqm": ("derived_rate", "the sale price per sqm"),
            },
            identity=("sale_price",),
        ),
        ListSpec(
            name="unit_schedule",
            noun="subject unit the valuer assesses a value for",
            slots={"valuation": "valuation.unit_schedule"},
            mentions=re.compile(
                r"\b(unit|lot|townhouse|apartment)s?\b.*\$|assessed\s+value|price\s+list", re.I | re.S
            ),
            key_field="unit",
            fields={
                "unit_mix": ("unit_mix", "bedrooms and bathrooms as written"),
                "area": ("area", "the internal area in sqm"),
                "outdoor_area": ("outdoor_area", "the private open space in sqm"),
                "value": ("value", "the valuer's assessed value"),
                "rate": ("rate", "the value per sqm of internal area"),
            },
            identity=("value", "area"),
        ),
        ListSpec(
            name="presales",
            noun="presale contract",
            slots={"presales": "presales", "contracts_eois": "presales"},
            mentions=re.compile(r"purchase\s+price|deposit|purchaser|\blot\b|sunset", re.I),
            key_field="lot",
            fields={
                "purchaser": ("purchaser", "the purchaser's name"),
                "price": ("price", "the contract price"),
                "deposit": ("deposit.amount", "the deposit amount"),
                "sunset_date": ("sunset_date", "the sunset date, as written"),
            },
            identity=("price", "purchaser"),
        ),
        ListSpec(
            name="funding_lines",
            noun="line of the funding table (sources and uses)",
            slots={"signed_its": "economics.funding_table", "funding_table": "funding_table.lines"},
            mentions=re.compile(
                r"funding|sources\s+(and|&)\s+uses|total\s+(project\s+)?costs?|senior|equity", re.I
            ),
            key_field="line_item",
            fields={
                "total": ("total", "the line's total"),
                "senior": ("senior", "the senior debt funding the line"),
                "equity": ("equity", "the equity funding the line"),
            },
            identity=("total", "senior"),
        ),
        ListSpec(
            name="covenants",
            noun="financial covenant",
            slots={"signed_its": "covenants", "loan_documentation": "covenants"},
            mentions=re.compile(r"covenant|\blvr\b|loan\s+to\s+value|interest\s+cover|\bicr\b|\blcr\b", re.I),
            key_field="name",
            fields={
                "threshold": ("threshold", "the threshold as worded, e.g. 'not to exceed 65%'"),
                "threshold_value": (
                    "threshold_value",
                    "the threshold as a number (65 for 65%, 1.5 for 1.5x)",
                ),
                "basis": ("basis", "what the threshold is measured against, as worded"),
            },
        ),
    )
}


def select_pages(spec: ListSpec, pages: Iterable[Page], slot_of: Mapping[str, str]) -> list[Page]:
    """The pages of documents filed in the list's slots that mention the list, capped per document."""
    chosen: list[Page] = []
    per_document: dict[str, int] = {}
    for page in pages:
        if slot_of.get(page.document_id) not in spec.slots or not spec.mentions.search(page.text):
            continue
        if per_document.get(page.document_id, 0) >= MAX_PAGES_PER_DOCUMENT:
            continue
        per_document[page.document_id] = per_document.get(page.document_id, 0) + 1
        chosen.append(page)
    return chosen


def _facts(spec: ListSpec, collection: str, items: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get(spec.key_field) or "").strip()
        if not label:
            continue
        key = slugify(label)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:  # the same label twice is two rows: keep both, distinguishably
            key = f"{key}_{seen[key]}"
        line = item.get("line")
        for name, (leaf, _) in spec.fields.items():
            value = str(item.get(name) or "").strip()
            if value:
                out.append({"path": f"{collection}[{key}].{leaf}", "value": value, "line": line})
    return out


def list_harvester(
    spec: ListSpec, slot: str, *, model_id: str, provider: Any | None = None
) -> Callable[[str], list[dict[str, Any]]]:
    """A `general.Harvester` for one list in one slot's documents."""
    from creditpaper.ingest.harvest import HarvestReplyError

    collection = spec.collection_for(slot)
    schema = spec.reply_schema()

    def harvest(window_text: str) -> list[dict[str, Any]]:
        from creditpaper.llm import client as llm_client
        from creditpaper.llm.providers import LLMProvider
        from creditpaper.llm.structured import StructuredOutputError, structured_json

        messages = [{"role": "user", "content": window_text}]
        if llm_client.effective_provider(provider) == LLMProvider.ANTHROPIC:
            try:
                data = structured_json(
                    tool=f"ingest:list:{spec.name}",
                    stage="extract",
                    schema=schema,
                    system=spec.instruction(structured=True),
                    messages=messages,
                    max_tokens=LIST_MAX_TOKENS,
                    model=model_id,
                    provider=LLMProvider.ANTHROPIC,
                    cache_system=True,
                )
            except StructuredOutputError as exc:
                raise HarvestReplyError(str(exc)) from exc
        else:
            resp = llm_client.messages_create(
                tool=f"ingest:list:{spec.name}",
                stage="extract",
                model=model_id,
                provider=provider,
                max_tokens=LIST_MAX_TOKENS,
                cache_system=True,
                system=spec.instruction(structured=False),
                messages=messages,
            )
            text = getattr(resp, "text", "") or ""
            start, end = text.find("{"), text.rfind("}")
            try:
                data = json.loads(text[start : end + 1]) if 0 <= start < end else None
            except json.JSONDecodeError as exc:
                raise HarvestReplyError(f"the list reply is not valid JSON ({exc.msg})") from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise HarvestReplyError("the list reply has no `items` list")
        return _facts(spec, collection, items)

    return harvest


def dedupe(outputs: Sequence[ExtractionOutput], spec: ListSpec, collection: str) -> list[ExtractionOutput]:
    """One row per item: rows whose identity leaves agree are one item, whatever their keys.

    The row with the most fields stays."""
    from creditpaper.ingest.consensus import _key

    if not spec.identity:
        return list(outputs)
    prefix = collection + "["
    out: list[ExtractionOutput] = []
    for output in outputs:
        rows: dict[str, dict[str, Any]] = {}
        for path, fact in output.fields.items():
            if path.startswith(prefix):
                key, _, leaf = path[len(prefix) :].partition("].")
                rows.setdefault(key, {})[leaf] = fact.value
        keep: dict[tuple[str, ...], str] = {}
        for key in sorted(rows, key=lambda k: (-len(rows[k]), k)):
            identity = tuple(
                _key(rows[key].get(leaf)) for leaf in spec.identity if rows[key].get(leaf) is not None
            )
            if len(identity) < len(spec.identity):
                continue  # an incomplete identity proves nothing; the row stays
            keep.setdefault(identity, key)
        drop = {
            key
            for key in rows
            if (
                ident := tuple(
                    _key(rows[key].get(leaf)) for leaf in spec.identity if rows[key].get(leaf) is not None
                )
            )
            and len(ident) == len(spec.identity)
            and keep[ident] != key
        }
        fields = {
            p: f for p, f in output.fields.items() if not any(p.startswith(f"{prefix}{k}]") for k in drop)
        }
        out.append(ExtractionOutput(fields, list(output.flags)))
    return out


def _in_collection(path: str, collections: set[str]) -> bool:
    return any(path.startswith(c + "[") for c in collections)


def merge(
    general: Sequence[ExtractionOutput], listed: Sequence[ExtractionOutput], *, collections: set[str]
) -> list[ExtractionOutput]:
    """The open harvest's outputs with list passes taking over the collections they cover, per document."""

    def document_of(output: ExtractionOutput) -> str | None:
        fact = next(iter(output.fields.values()), None)
        return fact.provenance.document_id if fact is not None and fact.provenance is not None else None

    covered: dict[str, set[str]] = {}
    stated: dict[str, set[str]] = {}  # exact paths a targeted pass stated, per document
    for output in listed:
        doc = document_of(output)
        if doc is None:
            continue
        for path in output.fields:
            covered.setdefault(doc, set()).update(c for c in collections if path.startswith(c + "["))
            stated.setdefault(doc, set()).add(path)
    out: list[ExtractionOutput] = []
    for output in general:
        doc = document_of(output)
        mine = covered.get(doc or "", set())
        said = stated.get(doc or "", set())
        kept = {p: f for p, f in output.fields.items() if not _in_collection(p, mine) and p not in said}
        out.append(ExtractionOutput(kept, list(output.flags)))
    out.extend(listed)
    return out


# --- field-set passes: one document type's figures, each asked for by name ---------------


@dataclass(frozen=True)
class FieldSetSpec:
    """A fixed set of scalar fields one document type states, asked for by name.

    Figures caught only by the open harvest vary between reads (measured 2026-10-09:
    the same blind_01 valuation gave 7 fewer of the paper's figures on a second read).
    Asking for each by name, with an explicit "not stated", makes them repeatable."""

    name: str
    slots: frozenset[str]
    mentions: re.Pattern[str]
    paths: tuple[str, ...]

    def reply_schema(self) -> type[BaseModel]:
        answer = create_model(
            f"{self.name.title()}Answer",
            __config__=ConfigDict(extra="forbid"),
            stated=(bool, ...),
            value=(str, ...),
            line=(int, ...),
        )
        fields: dict[str, Any] = {f"f{i}": (answer, Field(..., alias=p)) for i, p in enumerate(self.paths)}
        return create_model(
            f"{self.name.title()}Reply",
            __config__=ConfigDict(extra="forbid", populate_by_name=True),
            **fields,
        )

    def instruction(self, *, structured: bool) -> str:
        from creditpaper.model.concepts import concept_for

        listed = "\n".join(
            f"- {p}: {c.definition if (c := concept_for(p)) is not None else p}" for p in self.paths
        )
        shape = (
            "Reply in the required format: every field above."
            if structured
            else "Return one JSON object, and nothing else, with every field above as a key."
        )
        return (
            "You read commercial property finance documents for a lender.\n\n"
            "You will be given pages from ONE document, every line numbered as `N| text`. "
            "For EACH field below, say whether these pages state it.\n\n"
            f"FIELDS\n{listed}\n\n"
            "RULES\n"
            '- Where the pages state a field: {"stated": true, "value": <as stated>, "line": <its line>}.\n'
            '- Where they do not: {"stated": false, "value": "", "line": 0}.\n'
            "- Only what the pages STATE. Do not infer, calculate or carry over knowledge.\n\n"
            f"{shape}"
        )


FIELD_SETS: dict[str, FieldSetSpec] = {
    spec.name: spec
    for spec in (
        FieldSetSpec(
            name="valuation_figures",
            slots=frozenset({"valuation"}),
            mentions=re.compile(
                r"market\s+value|as\s+is|as\s+if\s+complete|gross\s+realisation|valuation|"
                r"site\s+area|adopted|gst",
                re.I,
            ),
            paths=(
                "valuation.valuer",
                "valuation.valuation_date",
                "valuation.report_date",
                "valuation.values_by_basis[as_is]",
                "valuation.values_by_basis[as_if_complete]",
                "valuation.values_by_basis[as_if_complete_incl_gst]",
                "valuation.values_by_basis[gross_realisation]",
                "valuation.values_by_basis[as_if_complete_one_line]",
                "valuation.methodology.approach",
                "valuation.methodology.adopted_rate",
                "valuation.property.title_reference",
                "valuation.property.zoning",
                "valuation.property.site_area",
                "valuation.property.gfa_total",
                "valuation.gst_basis",
            ),
        ),
    )
}


def field_set_harvester(
    spec: FieldSetSpec, *, model_id: str, provider: Any | None = None
) -> Callable[[str], list[dict[str, Any]]]:
    """A `general.Harvester` asking one document type's pages for each of its fields."""
    from creditpaper.ingest.harvest import HarvestReplyError

    schema = spec.reply_schema()

    def harvest(window_text: str) -> list[dict[str, Any]]:
        from creditpaper.llm import client as llm_client
        from creditpaper.llm.providers import LLMProvider
        from creditpaper.llm.structured import StructuredOutputError, structured_json

        messages = [{"role": "user", "content": window_text}]
        if llm_client.effective_provider(provider) == LLMProvider.ANTHROPIC:
            try:
                data = structured_json(
                    tool=f"ingest:fields:{spec.name}",
                    stage="extract",
                    schema=schema,
                    system=spec.instruction(structured=True),
                    messages=messages,
                    max_tokens=LIST_MAX_TOKENS,
                    model=model_id,
                    provider=LLMProvider.ANTHROPIC,
                    cache_system=True,
                )
            except StructuredOutputError as exc:
                raise HarvestReplyError(str(exc)) from exc
        else:
            resp = llm_client.messages_create(
                tool=f"ingest:fields:{spec.name}",
                stage="extract",
                model=model_id,
                provider=provider,
                max_tokens=LIST_MAX_TOKENS,
                cache_system=True,
                system=spec.instruction(structured=False),
                messages=messages,
            )
            text = getattr(resp, "text", "") or ""
            start, end = text.find("{"), text.rfind("}")
            try:
                data = json.loads(text[start : end + 1]) if 0 <= start < end else None
            except json.JSONDecodeError as exc:
                raise HarvestReplyError(f"the fields reply is not valid JSON ({exc.msg})") from exc
        if not isinstance(data, dict):
            raise HarvestReplyError("the fields reply is not an object")
        out: list[dict[str, Any]] = []
        for path in spec.paths:
            answer = data.get(path)
            if isinstance(answer, dict) and answer.get("stated") and str(answer.get("value") or "").strip():
                out.append({"path": path, "value": str(answer["value"]).strip(), "line": answer.get("line")})
        return out

    return harvest

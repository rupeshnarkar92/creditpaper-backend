"""The label store: every analyst decision on a fact, kept as a label (spec 3c).

A label records what the system extracted, what the analyst decided, and the
versions and verification verdict in force at the time. Labels are append-only
(migration 0026): a later correction appends a new label, and the latest label per
(deal, concept) is the answer key. They are tenant data, read and written under
forced RLS; nothing here reads across tenants (D12).

`record_label` takes the caller's cursor so the label shares the decision's
transaction: a decision that fails writes no label, and a label cannot outlive a
rolled-back decision.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from creditpaper.db import tenant_cursor

Action = Literal["accepted", "corrected", "other_source", "set_aside"]
Source = Literal["analyst", "seed"]
ACTIONS: frozenset[str] = frozenset({"accepted", "corrected", "other_source", "set_aside"})
SOURCES: frozenset[str] = frozenset({"analyst", "seed"})

# Verdicts under which the system presented a fact as good. The ingest pipeline
# (Plan C Task 5) commits a VERIFIED fact as status `extracted` and anything else as
# `flagged`. Current assertions persist FactVerification; status remains the fallback
# for legacy assertions written before verification context was retained.
PASSED_VERDICTS: frozenset[str] = frozenset({"verified", "anchored", "extracted"})


@dataclass(frozen=True)
class Label:
    tenant_id: str
    deal_id: str
    concept_path: str
    action: str
    source: str
    extracted_value: Any = None
    final_value: Any = None
    id: str | None = None
    assertion_id: str | None = None
    document_version_id: str | None = None
    page: int | None = None
    quote: str | None = None
    reason_code: str | None = None
    verdict_at_decision: str | None = None
    verify_reasons: tuple[str, ...] = field(default_factory=tuple)
    extractor_version: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None

    @property
    def passed(self) -> bool:
        """The system presented this fact as good (verified / not flagged)."""
        return (self.verdict_at_decision or "") in PASSED_VERDICTS


_COLUMNS = (
    "id",
    "tenant_id",
    "deal_id",
    "concept_path",
    "assertion_id",
    "document_version_id",
    "page",
    "quote",
    "extracted_value",
    "final_value",
    "action",
    "reason_code",
    "verdict_at_decision",
    "verify_reasons",
    "extractor_version",
    "model_id",
    "prompt_version",
    "decided_by",
    "decided_at",
    "source",
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def record_label(
    cur: Any,
    *,
    tenant_id: str,
    deal_id: str,
    concept_path: str,
    action: str,
    source: str = "analyst",
    extracted_value: Any = None,
    final_value: Any = None,
    assertion_id: str | None = None,
    document_version_id: str | None = None,
    page: int | None = None,
    quote: str | None = None,
    reason_code: str | None = None,
    verdict_at_decision: str | None = None,
    verify_reasons: Iterable[str] = (),
    extractor_version: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    decided_by: str | None = None,
    decided_at: datetime | None = None,
) -> str:
    """Append one label on the caller's cursor (and so in its transaction). Returns its id."""
    if action not in ACTIONS:
        raise ValueError(f"unknown label action: {action!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown label source: {source!r}")
    if source == "analyst" and not decided_by:
        raise ValueError("an analyst label needs the deciding user")
    cur.execute(
        """INSERT INTO fact_labels
        (tenant_id,deal_id,concept_path,assertion_id,document_version_id,page,quote,
         extracted_value,final_value,action,reason_code,verdict_at_decision,verify_reasons,
         extractor_version,model_id,prompt_version,decided_by,decided_at,source)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
               coalesce(%s,clock_timestamp()),%s)
        RETURNING id""",
        (
            tenant_id,
            deal_id,
            concept_path,
            assertion_id,
            document_version_id,
            page,
            quote,
            _json(extracted_value),
            _json(final_value),
            action,
            reason_code,
            verdict_at_decision,
            _json(list(verify_reasons)),
            extractor_version,
            model_id,
            prompt_version,
            decided_by,
            decided_at,
            source,
        ),
    )
    return str(cur.fetchone()[0])


def label_from_assertion(fact: Mapping[str, Any]) -> dict[str, Any]:
    """The label fields a stored assertion payload (`ledger_assertions.fact`) carries."""
    prov = fact.get("provenance") or {}
    verification = fact.get("verification") or {}
    method = prov.get("method")
    return {
        "document_version_id": prov.get("document_version_id"),
        "page": prov.get("page"),
        # The anchor the extractor cited; a derived fact's location is its formula.
        "quote": None if method == "derived" else prov.get("location"),
        "verdict_at_decision": verification.get("verdict") or fact.get("status"),
        "verify_reasons": tuple(verification.get("reasons") or ()),
        "extractor_version": prov.get("extractor_version"),
        "model_id": prov.get("model_id"),
        "prompt_version": prov.get("prompt_version"),
    }


def _row(row: tuple[Any, ...]) -> Label:
    data = dict(zip(_COLUMNS, row, strict=True))
    for key in ("id", "tenant_id", "assertion_id", "document_version_id", "decided_by"):
        if data[key] is not None:
            data[key] = str(data[key])
    data["verify_reasons"] = tuple(data["verify_reasons"] or ())
    return Label(**data)


def labels_for(
    tenant_id: str,
    *,
    deal_id: str | None = None,
    concept_path: str | None = None,
    source: str | None = None,
    extractor_version: str | None = None,
    latest_only: bool = False,
) -> list[Label]:
    """This tenant's labels, oldest first. `latest_only` keeps the newest label per
    (deal, concept): a later correction supersedes an earlier decision as the answer key."""
    where, params = ["tenant_id=%s"], [tenant_id]
    for column, value in (
        ("deal_id", deal_id),
        ("concept_path", concept_path),
        ("source", source),
        ("extractor_version", extractor_version),
    ):
        if value is not None:
            where.append(f"{column}=%s")
            params.append(value)
    # Identifiers are fixed constants above, never caller input; values are parameters.
    columns, clause = ",".join(_COLUMNS), " AND ".join(where)
    if latest_only:
        sql = (
            f"SELECT DISTINCT ON (deal_id, concept_path) {columns} FROM fact_labels WHERE {clause} "
            "ORDER BY deal_id, concept_path, decided_at DESC, id DESC"
        )
    else:
        sql = f"SELECT {columns} FROM fact_labels WHERE {clause} ORDER BY decided_at, id"
    with tenant_cursor(tenant_id) as cur:
        cur.execute(sql, params)
        rows = [_row(r) for r in cur.fetchall()]
    if latest_only:
        rows.sort(key=lambda r: (str(r.decided_at), r.id or ""))
    return rows


def as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
    except (InvalidOperation, ValueError):
        return None


def values_agree(extracted: Any, final: Any) -> bool:
    """Whether the extracted value is the analyst's final value (numbers by value,
    text by normalised form). A missing side agrees only with another missing side."""
    if extracted is None or final is None:
        return extracted is None and final is None
    a, b = as_decimal(extracted), as_decimal(final)
    if a is not None and b is not None:
        return a == b
    if isinstance(extracted, (dict, list)) or isinstance(final, (dict, list)):
        return _json(extracted) == _json(final)
    return " ".join(str(extracted).split()).casefold() == " ".join(str(final).split()).casefold()

"""Load a curated ground-truth file into a DealModel as HUMAN_ENTRY facts (Phase 0).

The same paths an extractor will later fill with EXTRACTED page/clause anchors are
filled now by hand-curation, so the renderer and section agents can be built before
extraction exists (DECISIONS.md 2026-06-12, paper-first bootstrap). Everything goes
through the single write path (apply) and the derivation engine runs after loading,
so equity and aggregates are computed exactly as they will be in production.

Reads `figures_numeric` (Decimal/int facts) and a whitelist of `reference_values`
(string/date facts) from the ground-truth JSON. Paths that don't resolve against the
schema are reported, never silently dropped — surfacing ground-truth/schema drift.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from creditpaper.model import (
    DealModel,
    DealType,
    ExtractionMethod,
    ExtractionOutput,
    Fact,
    FactStatus,
    Provenance,
    apply,
    derive_economics,
    derive_metrics,
    slugify,
)

# figures whose value is an int, not a Decimal:
_INT_PATHS = ("term_months",)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# reference_values keys handled outside the generic path-apply (or deliberately
# not loaded — provider belongs in provenance, not a field; D-decision):
_REFERENCE_SKIP = {"deal.expected_funding_date", "deal.maturity_date", "screening.aml_provider"}


@dataclass
class PopulateReport:
    applied: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)
    inconsistent: list[tuple] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.inconsistent


def _human(value, source: str, location: str) -> Fact:
    """A hand-curated fact: present and citable, method HUMAN_ENTRY."""
    return Fact(
        value=value,
        status=FactStatus.EXTRACTED,
        confidence=1.0,
        provenance=Provenance(document_id=source, location=location, method=ExtractionMethod.HUMAN_ENTRY),
    )


def populate_from_ground_truth(gt_path: str | Path) -> tuple[DealModel, PopulateReport]:
    gt = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    source = gt.get("source_document", "ground_truth")
    report = PopulateReport()

    model = DealModel(tenant_id="3capital", deal_id=gt.get("deal_name", "deal"))

    # Deal identity. Routed through `apply` with everything else rather than
    # assigned directly: `apply` is the only write path (D1), and it is where the
    # declared-type validation lives. These three were assigned straight onto the
    # model, so they were the one set of facts in the system that skipped it.
    fields: dict[str, Fact] = {}
    if gt.get("deal_name"):
        fields["deal.name"] = _human(gt["deal_name"], source, "deal name")
    if gt.get("deal_type"):
        fields["deal.type"] = _human(DealType(gt["deal_type"]), source, "deal type")
    efd = gt.get("reference_values", {}).get("deal.expected_funding_date")
    if efd:
        fields["deal.expected_funding_date"] = _human(
            date.fromisoformat(efd), source, "expected funding date"
        )

    # Numeric figures.
    for path, raw in gt.get("figures_numeric", {}).items():
        try:
            value = int(raw) if path.endswith(_INT_PATHS) else Decimal(str(raw))
        except (InvalidOperation, ValueError):
            report.rejected[path] = f"not a number: {raw!r}"
            continue
        fields[path] = _human(value, source, path)

    # Reference values: attempt every key as a model path (strings, or dates when
    # the value is ISO-formatted). Paths with no model home are reported, not
    # dropped — this is the model-coverage map.
    refs = gt.get("reference_values", {})
    for path, raw in refs.items():
        if path in _REFERENCE_SKIP or path.startswith(("guarantor.", "_")) or not isinstance(raw, str):
            continue
        value: object = date.fromisoformat(raw) if _ISO_DATE.match(raw) else raw
        fields[path] = _human(value, source, path)

    # Covenants: the keyed list (DECISIONS.md 2026-06-10) — summaries from the
    # curated covenants_text block.
    for key, text in gt.get("covenants_text", {}).items():
        fields[f"covenants[{key}].summary"] = _human(str(text), source, f"covenant {key}")

    # Guarantors: convenience keys guarantor.unlimited / .limited → its.guarantors[<slug>].
    for kind in ("unlimited", "limited"):
        val = refs.get(f"guarantor.{kind}")
        if val:
            name = str(val).split("(")[0].strip()
            fields[f"its.guarantors[{slugify(name)}]"] = _human(str(val), source, f"{kind} guarantor")

    result = apply(model, ExtractionOutput(fields=fields, flags=[]))
    report.applied = result.applied
    report.rejected.update(result.rejected)

    # Derive equity + aggregates exactly as production will, then the covenant /
    # security metrics that every section cites (one canonical figure, not prose
    # recomputation).
    dr = derive_economics(model)
    mr = derive_metrics(model)
    report.derived = dr.derived + mr.derived
    report.inconsistent = dr.inconsistent + mr.inconsistent

    return model, report

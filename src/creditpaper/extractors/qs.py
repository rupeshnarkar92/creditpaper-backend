"""Quantity Surveyor extractor — Instance C, MATERIAL findings only.

The financier's QS report (initial report / cost-to-complete) is what tests the
construction side of the credit: is the cost to complete fundable from the facility,
is the contingency adequate, does the QS agree with the builder's contract sum, and
what could blow the budget. This extractor pulls those JUDGEMENTS and the headline
cost figures — not the trade-by-trade bill of quantities (D7). Those facts feed the
analysis cross-read the constitution mandates (QS cost vs the funding-table
construction line vs contingency adequacy).

Each fact carries its page + clause (D4). Output flows through the apply layer.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from creditpaper.extractors._doc import (
    ANCHOR_RULE,
    Anchored,
    add_bool_fact,
    add_fact,
    add_note_fact,
    b64,
    document_json,
    document_json_pages,
    validate_extraction,
)
from creditpaper.extractors.base import ExtractionOutput, Extractor
from creditpaper.model.citations import safe_slug
from creditpaper.model.core import Fact

__all__ = ["Anchored", "QsExtraction", "QsExtractor", "RiskOut"]


class RiskOut(BaseModel):
    summary: Anchored | None = None  # a single overrun risk / item-not-in-feasibility


class CostLineOut(BaseModel):
    # category aligns with funding-table keys where possible (construction, contingency,
    # professional_fees, statutory_fees, preliminaries, margin, …) so check #3 can pair.
    category: str = ""
    amount: Anchored | None = None
    label: Anchored | None = None  # the QS's own wording for the line
    # Which column the amount came from. Not optional in meaning, only in
    # availability: an unstated basis makes the line unpairable downstream, which
    # is the safe outcome. A guessed one produces a false divergence.
    basis: Anchored | None = None  # total_cost | cost_to_complete | costs_to_date


class QsExtraction(BaseModel):
    firm: Anchored | None = None  # the financier's QS firm
    project_cost: Anchored | None = None  # total development/construction cost
    cost_to_complete: Anchored | None = None  # remaining cost to complete
    contract_sum_view: Anchored | None = None  # the QS's view of the builder's contract sum
    agreement_appropriate: Anchored | None = None  # is the construction contract appropriate (3.11)
    cost_lines: list[CostLineOut] = []  # the cost-to-complete / dev-cost breakdown
    overrun_risks: list[RiskOut] = []  # cost/programme overrun risks the QS flags
    items_not_in_feasibility: list[RiskOut] = []  # costs the QS says are missing from the feasibility


_INSTRUCTION = (
    "You are extracting the CREDIT-MATERIAL findings from a financier's Quantity "
    "Surveyor report (initial report / cost-to-complete) for a CRE construction loan. "
    "Extract ONLY what the report states; for EVERY value record the page and a verbatim clause as its "
    "anchor; OMIT anything you cannot locate (no anchor, no "
    "field). Numbers as plain digits (no $, commas). Capture: the QS FIRM; the total "
    "project/development cost; the cost TO COMPLETE (remaining); the QS's VIEW of the "
    "builder's contract sum (is it reasonable/market, restate concisely); and whether "
    "the construction CONTRACT/agreement is appropriate (agreement_appropriate "
    "true/false). Capture the cost BREAKDOWN as cost_lines — the summary cost lines "
    "(NOT the trade-by-trade bill of quantities): for each, the amount, the QS's "
    "label, a `category` chosen from this canonical set where it applies so it can be "
    "matched to the loan's funding table (construction, contingency, professional_fees, "
    "statutory_fees, preliminaries, margin, land, interest, other), and — REQUIRED — "
    "the `basis`, meaning WHICH COLUMN the amount was read from: 'total_cost' (the "
    "full development cost of that line), 'cost_to_complete' (the remaining cost only), "
    "or 'costs_to_date' (already incurred). QS reports usually print several of these "
    "columns side by side and they are NOT interchangeable: a downstream check compares "
    "these lines against the loan's funding table, which states TOTAL cost, so a figure "
    "read from the wrong column produces a false discrepancy. Where a line shows more "
    "than one column, emit the total_cost figure. If the column is genuinely unclear, "
    "OMIT basis rather than guessing — an unstated basis is handled safely downstream, "
    "a wrong one is not. "
    "Then the QS's risk JUDGEMENTS: list the cost/programme OVERRUN RISKS the QS flags, "
    "and any items the QS says are NOT in the developer's feasibility/budget. Return ONLY "
    "a JSON object of this shape, omitting fields you cannot find:\n"
    '{"firm": {"value": "Mitchell Brandtman", "page": 1, "clause": "This report was prepared by Mitchell '
    'Brandtman for the financier named above."},\n'
    ' "project_cost": {"value": "12450000", "page": 3, "clause": "The Total Development Cost is assessed at '
    '12,450,000 inclusive of all allowances."},\n'
    ' "cost_to_complete": {"value": "9800000", "page": 3, "clause": "The Cost to Complete the remaining '
    'works is assessed at 9,800,000."},\n'
    ' "contract_sum_view": {"value": "contract sum assessed as reasonable and within '
    'market range", "page": 4, "clause": "..."},\n'
    ' "agreement_appropriate": {"value": "true", "page": 4, "clause": '
    '"The fixed-price lump-sum form of agreement is appropriate for works of this scale."},\n'
    # Round, obviously-synthetic figures. A real deal's numbers here are both tenant
    # data in source and a numeric anchor the model can echo onto an unrelated deal.
    ' "cost_lines": [{"category": "construction", "amount": {"value": "6000000", "page": 3, '
    '"clause": "Construction works are costed at 6,000,000 in the Total Cost column."}, "label": {"value": '
    '"Building works", "page": 3, "clause": "Building works form the principal construction cost line in '
    'this estimate."}, '
    '"basis": {"value": "total_cost", "page": 3, "clause": "Amounts in the Total Cost column are stated on a '
    'total_cost basis."}}, '
    '{"category": "contingency", "amount": {"value": "250000", "page": 3, "clause": "A contingency allowance '
    'of 250,000 is included within the total development cost."}, '
    '"basis": {"value": "total_cost", "page": 3, "clause": "Amounts in the Total Cost column are stated on a '
    'total_cost basis."}}],\n'
    ' "overrun_risks": [{"summary": {"value": "provisional sums for facade not yet '
    'finalised", "page": 6, "clause": "..."}}],\n'
    ' "items_not_in_feasibility": [{"summary": {"value": "no allowance for s7.11 '
    'contributions", "page": 7, "clause": "..."}}]}'
    "\n" + ANCHOR_RULE
)


class QsExtractor(Extractor):
    slot_key = "qs"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM/document boundary (override in tests) ---------------------------
    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> QsExtraction:
        if png_pages or text_context:
            return validate_extraction(
                QsExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:qs",
                ),
            )
        return validate_extraction(
            QsExtraction, document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:qs")
        )

    def extract(
        self,
        document_id: str,
        path: Path,
        *,
        page_texts: list[dict] | None = None,
    ) -> ExtractionOutput:
        from creditpaper.extractors._doc import pages_to_png
        from creditpaper.processing.page_selector import select_pages

        if page_texts:
            _idx = select_pages(page_texts, "qs")
            _pngs = pages_to_png(path, _idx)
            _ctx = "\n\n".join(
                f"[Page {pt['page']}]\n{pt.get('text', '')}"
                for pt in page_texts
                if (pt.get("page", 1) - 1) in _idx
            )
            data = self._extract_raw(png_pages=_pngs, text_context=_ctx)
        else:
            data = self._extract_raw(pdf_b64=b64(path))
        fields: dict[str, Fact] = {}
        flags: list[str] = []

        def put(p: str, anc: Anchored | None) -> None:
            add_fact(fields, flags, document_id, p, anc)

        def note(p: str, anc: Anchored | None) -> None:
            add_note_fact(fields, flags, document_id, p, anc)

        put("qs.firm", data.firm)
        put("qs.project_cost", data.project_cost)
        put("qs.cost_to_complete", data.cost_to_complete)
        note("qs.contract_sum_view", data.contract_sum_view)
        add_bool_fact(fields, flags, document_id, "qs.agreement_appropriate", data.agreement_appropriate)

        for line in data.cost_lines:
            key = (
                safe_slug(line.category)
                if line.category
                else (safe_slug(str(line.label.value)[:40]) if line.label and line.label.value else "")
            )
            if not key or line.amount is None:
                continue
            put(f"qs.cost_lines[{key}].amount", line.amount)
            put(f"qs.cost_lines[{key}].label", line.label)
            if line.basis is not None:
                put(f"qs.cost_lines[{key}].basis", line.basis)

        for r in data.overrun_risks:
            if r.summary and r.summary.value:
                note(f"qs.overrun_risks[{safe_slug(str(r.summary.value)[:48])}]", r.summary)
        for r in data.items_not_in_feasibility:
            if r.summary and r.summary.value:
                note(f"qs.items_not_in_feasibility[{safe_slug(str(r.summary.value)[:48])}]", r.summary)
        return ExtractionOutput(fields=fields, flags=flags)

"""ITS (Indicative Terms Sheet) extractor — Instance A, the foundational document.

Per D5 (DECISIONS.md 2026-06-10) the signed ITS is the source of facility
economics: the funding table, fees, covenants, parties. Reads the PDF natively,
emits each fact with PARSE-TIME PROVENANCE (D4) — the model returns each value's
page + source clause as its anchor; unlocatable values are dropped, not guessed.
Output flows through the apply layer (the only write path); the extractor never
touches the model.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from creditpaper.extractors._doc import (
    ANCHOR_RULE,
    Anchored,
    _scored,
    add_fact,
    b64,
    document_json,
    document_json_pages,
    validate_extraction,
)
from creditpaper.extractors.base import ExtractionOutput, Extractor
from creditpaper.model.citations import safe_slug
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance
from creditpaper.model.deal import DealType

__all__ = ["Anchored", "CovenantOut", "FundingLineOut", "ItsExtraction", "ItsExtractor"]


class FundingLineOut(BaseModel):
    key: str
    total: Anchored | None = None
    senior: Anchored | None = None


class CovenantOut(BaseModel):
    key: str
    threshold_value: Anchored | None = None  # numeric, e.g. 45 for 45%
    summary: str = ""


class ItsExtraction(BaseModel):
    deal_type: Anchored | None = None  # construction / bridge / residual_stock
    facility_amount: Anchored | None = None
    interest_rate: Anchored | None = None
    term_months: Anchored | None = None
    establishment_fee_pct: Anchored | None = None
    line_fee_pct: Anchored | None = None
    # The same fees in DOLLARS. The percentage alone does not give the paper
    # its funding table: a fee stated only as a rate has to be multiplied by a
    # limit that may itself be a range. Ported from the feat/ingestion line on
    # merge (2026-09-23); this line had the percentages only.
    establishment_fee_amount: Anchored | None = None
    line_fee_amount: Anchored | None = None
    borrower_entity: Anchored | None = None
    guarantors: list[Anchored] = []
    funding_table: list[FundingLineOut] = []
    covenants: list[CovenantOut] = []


_INSTRUCTION = (
    "You are extracting facility economics from a signed Indicative Terms Sheet "
    "(ITS) for an Australian CRE private-credit construction loan. Extract ONLY "
    "what the document states.\n" + ANCHOR_RULE + "\n"
    "Numbers as plain digits (no $, commas, "
    "%). The funding_table is INDIVIDUAL cost lines only (land, construction, "
    "contingency, interest, fees…) — do NOT include subtotal/total summary rows "
    "(e.g. 'Total cash cost', 'Total facility', 'Peak debt') as funding lines; the "
    "facility total is facility_amount. Classify the FACILITY TYPE as exactly one of "
    "'construction' (funds land + progressive construction drawdowns), 'bridge' "
    "(short-term, drawn at close), or 'residual_stock' (against completed unsold "
    "stock) — anchor it to the clause that establishes it (purpose, facility name, or "
    "a construction-drawdown/land line). Return ONLY a JSON object (no prose) of this "
    "shape, omitting fields you cannot find:\n"
    '{"deal_type": {"value": "construction", "page": 1, "clause": '
    '"The Facility is a Construction Facility funding the land and progressive '
    'construction drawdowns."},\n'
    ' "facility_amount": {"value": "7758697", "page": 2, "clause": '
    '"The Total Facility Limit is $7,758,697 inclusive of capitalised interest and fees."},\n'
    ' "interest_rate": {"value": "9.50", "page": 2, "clause": '
    '"Interest accrues at 9.50% per annum on the daily drawn balance of the Facility."},\n'
    ' "term_months": {"value": "14", "page": 2, "clause": '
    '"The Term is 14 months from the date of first drawdown under the Facility."},\n'
    ' "establishment_fee_amount": {"value": "190000", "page": 4, "clause": '
    '"Establishment Fee 190,000 190,000 0 of the total facility"},\n'
    ' "line_fee_amount": {"value": "142500", "page": 4, "clause": '
    '"Line Fee 142,500 142,500 0 across the facility term"},\n'
    ' "establishment_fee_pct": {"value": "1.25", "page": 2, "clause": '
    '"An establishment fee of 1.25% of the Facility Limit is payable at settlement."},\n'
    ' "line_fee_pct": {"value": "1.25", "page": 2, "clause": '
    '"A line fee of 1.25% per annum is payable monthly in arrears on the Limit."},\n'
    ' "borrower_entity": {"value": "Northvale Pty Ltd", "page": 1, "clause": '
    '"The Borrower is Northvale Pty Ltd, a company incorporated in New South Wales."},\n'
    ' "guarantors": [{"value": "Northvale Holdings Pty Ltd", "page": 1, "clause": '
    '"Northvale Holdings Pty Ltd guarantees each obligation of the Borrower under this sheet."}],\n'
    ' "funding_table": [{"key": "land", "total": {"value": "7000000", "page": 4, "clause": '
    '"Land acquisition 7,000,000 of which senior debt funds 2,000,000 at settlement."}, '
    '"senior": {"value": "2000000", "page": 4, "clause": '
    '"Land acquisition 7,000,000 of which senior debt funds 2,000,000 at settlement."}}],\n'
    ' "covenants": [{"key": "lvr_max", "threshold_value": {"value": "45", "page": 3, "clause": '
    '"The Borrower must maintain an LVR of no more than 45% of the As If Complete value."}, '
    '"summary": "Maintain LVR (ex GST) <= 45% of As If Complete"}]}'
)


class ItsExtractor(Extractor):
    slot_key = "its"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM/document boundary (override in tests) ---------------------------
    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> ItsExtraction:
        if png_pages or text_context:
            return validate_extraction(
                ItsExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:its",
                ),
            )
        # Legacy path: full PDF b64
        return validate_extraction(
            ItsExtraction, document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:its")
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
            page_indices = select_pages(page_texts, "its")
            png_pages = pages_to_png(path, page_indices)
            text_context = "\n\n".join(
                f"[Page {pt['page']}]\n{pt.get('text', '')}"
                for pt in page_texts
                if (pt.get("page", 1) - 1) in page_indices
            )
            data = self._extract_raw(png_pages=png_pages, text_context=text_context)
        else:
            data = self._extract_raw(pdf_b64=b64(path))

        fields: dict[str, Fact] = {}
        flags: list[str] = []

        def put(p: str, anc: Anchored | None, *, as_int: bool = False) -> None:
            add_fact(fields, flags, document_id, p, anc, as_int=as_int)

        # Deal type is a typed enum, not a numeric/string fact — validate against
        # DealType and emit the enum value so the derivation engine's deal-type
        # branches fire (a missing/wrong type silently degraded day-one LVR to
        # facility/as-is on the Riverton full-ingest run, 2026-06-16). D4 still applies.
        dt = data.deal_type
        if dt is not None and dt.value is not None:
            raw = str(dt.value).strip().lower().replace(" ", "_").replace("-", "_")
            if not dt.clause and dt.page is None:
                flags.append("deal.type: dropped — no anchor (D4)")
            elif raw in {m.value for m in DealType}:
                confidence, flagged = _scored(dt.value, dt, "deal.type")
                fields["deal.type"] = Fact(
                    value=DealType(raw),
                    status=FactStatus.FLAGGED if flagged else FactStatus.EXTRACTED,
                    confidence=confidence,
                    provenance=Provenance(
                        document_id=document_id,
                        page=dt.page,
                        location=dt.clause or f"page {dt.page}",
                        method=ExtractionMethod.LLM_EXTRACTION,
                    ),
                )
            else:
                flags.append(f"deal.type: '{dt.value}' not a known DealType — skipped")

        put("economics.facility_amount", data.facility_amount)
        put("economics.interest_rate", data.interest_rate)
        put("economics.term_months", data.term_months, as_int=True)
        put("economics.fees[establishment].pct", data.establishment_fee_pct)
        put("economics.fees[line].pct", data.line_fee_pct)
        put("economics.fees[establishment].amount", data.establishment_fee_amount)
        put("economics.fees[line].amount", data.line_fee_amount)
        put("its.borrower_entity", data.borrower_entity)
        for g in data.guarantors:
            if g.value:
                # Drop a trailing qualifier — "Smith Pty Ltd (limited to $500k)" keys
                # as `smith_pty_ltd` — but only when something is left. A guarantor
                # written "(ACN 123 456 789) Smith Pty Ltd" has its whole name after
                # the bracket, so taking the part before it yields an empty label.
                name = str(g.value)
                stem = name.split("(")[0].strip() or name
                put(f"its.guarantors[{safe_slug(stem)}]", g)
        for line in data.funding_table:
            base = f"economics.funding_table[{safe_slug(line.key)}]"
            put(f"{base}.total", line.total)
            put(f"{base}.senior", line.senior)
        for cov in data.covenants:
            key = safe_slug(cov.key)
            put(f"covenants[{key}].threshold_value", cov.threshold_value)
            if cov.summary and cov.threshold_value is not None:
                summary_confidence, summary_flagged = _scored(
                    cov.summary, cov.threshold_value, f"covenants[{key}].summary"
                )
                fields[f"covenants[{key}].summary"] = Fact(
                    value=cov.summary,
                    status=FactStatus.FLAGGED if summary_flagged else FactStatus.EXTRACTED,
                    confidence=summary_confidence,
                    provenance=Provenance(
                        document_id=document_id,
                        page=cov.threshold_value.page,
                        location=cov.threshold_value.clause or "covenant clause",
                        method=ExtractionMethod.LLM_EXTRACTION,
                    ),
                )
        return ExtractionOutput(fields=fields, flags=flags)

"""Sponsor / borrower extractor — Instance C, the §5 borrower-depth slot.

Section 5 turns on three questions about the people behind the deal: are they
financially sound (the sponsor's financial position and trend), have they done
this before (a TESTED track record, not a transcribed CV — 7.3 #2), and how is
the borrowing group structured (who borrows, who guarantees, who ultimately
owns it). This extractor pulls those from whatever borrower-side document the
slot is given — financial statements, an accountant's letter, a statement of
position, a development/experience schedule, or a group-structure chart.

D7 holds: headline CONCLUSIONS and the figures the document states (revenue
trend, profitability, debt levels, going concern, cash equity), NOT a
reconstruction of the full accounts, and never raw personal identity data.
Every fact carries its page + verbatim clause (D4). Output flows through the
apply layer; the multi-year keys, entity graph and track-record claims are
keyed collections the apply layer creates on first write.

KNOWN LIMITATION: routing picks one canonical document per slot, so where the
sponsor's financials, structure and track record live in separate files only
the primary document is read (the multi-doc-per-slot work is future). The
instruction is deliberately broad so a single combined memo yields all three.
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

__all__ = [
    "Anchored",
    "EntityOut",
    "FinancialYearOut",
    "NoteOut",
    "SponsorExtraction",
    "SponsorExtractor",
    "TrackRecordOut",
]


class NoteOut(BaseModel):
    summary: Anchored | None = None  # one contingent liability / related-party transaction


class FinancialYearOut(BaseModel):
    year: str = ""  # "FY2024" / "2023" -> the financials key
    revenue_trend: Anchored | None = None
    profitability: Anchored | None = None  # NPAT / net result (digits, may be negative)
    debt_levels: Anchored | None = None  # total / interest-bearing debt
    going_concern: Anchored | None = None  # "true"/"false"
    contingent_liabilities: list[NoteOut] = []
    related_party_transactions: list[NoteOut] = []


class EntityOut(BaseModel):
    name: Anchored | None = None  # raw, no normalisation (check #12)
    entity_type: Anchored | None = None  # company / trust / individual / partnership
    role: Anchored | None = None  # borrower / guarantor / trustee / holdco ...


class TrackRecordOut(BaseModel):
    claim: Anchored | None = None  # the discrete, testable assertion
    project: Anchored | None = None
    asset_type: Anchored | None = None
    scale: Anchored | None = None  # GDV / units / value, as stated
    year: Anchored | None = None  # completion year (int)
    verifiable_detail: Anchored | None = None  # what makes it checkable (address, partner, ref)


class SponsorExtraction(BaseModel):
    cash_equity_to_date: Anchored | None = None  # cumulative cash equity contributed
    liquidity_cushion: Anchored | None = None  # unused cash buffer in the borrowing entity
    financial_years: list[FinancialYearOut] = []
    entities: list[EntityOut] = []
    trusts: list[EntityOut] = []
    ubos: list[Anchored] = []  # ultimate beneficial owners, as identified
    track_record: list[TrackRecordOut] = []


_INSTRUCTION = (
    "You are extracting SPONSOR / BORROWER facts for a CRE loan credit paper "
    "(Section 5: borrower financial standing, track record, and group structure), "
    "from a borrower-side document — financial statements, an accountant's letter, "
    "a statement of financial position, a development/experience schedule, or a "
    "group-structure chart. Extract ONLY what the document states; anchor EVERY "
    "value to a page + verbatim clause; OMIT what you cannot locate (no "
    "anchor, no field). Numbers as plain digits (no $, commas; a loss is negative). "
    "Capture HEADLINE CONCLUSIONS and figures, NOT a reconstruction of the full "
    "accounts, and NEVER raw personal identity data (dates of birth, document "
    "numbers, addresses of individuals).\n"
    "Capture: cumulative CASH EQUITY contributed to date; any unused cash LIQUIDITY "
    "cushion in the borrowing entity. For EACH financial year disclosed (most recent "
    "first), the year label, the REVENUE TREND (a short phrase), PROFITABILITY (net "
    "result figure), DEBT LEVELS (total/interest-bearing debt figure), whether GOING "
    "CONCERN is confirmed (true/false), and list any CONTINGENT LIABILITIES and "
    "RELATED-PARTY TRANSACTIONS the notes disclose (surface each, do not summarise "
    "them away). The group STRUCTURE: each entity (name, type company/trust/"
    "individual/partnership, and role borrower/guarantor/trustee/holdco), the trusts "
    "explicitly, and the ultimate beneficial owners. The TRACK RECORD as discrete, "
    "TESTABLE claims (not a transcribed CV): for each prior project the claim, the "
    "project, asset type, scale (GDV/units/value), completion year, and a verifiable "
    "detail that makes it checkable. Return ONLY a JSON object of this shape, "
    "omitting fields you cannot find:\n"
    '{"cash_equity_to_date": {"value": "1850000", "page": 2, "clause": "Equity contributed to date by the '
    'sponsor totals 1,850,000 across the project."},\n'
    ' "liquidity_cushion": {"value": "420000", "page": 2, "clause": "Cash at bank at balance date was '
    '420,000 held in operating accounts."},\n'
    ' "financial_years": [{"year": "FY2024",\n'
    '   "revenue_trend": {"value": "revenue up 12% on prior year", "page": 3, "clause": "..."},\n'
    '   "profitability": {"value": "640000", "page": 3, "clause": "Net profit after tax for the year was '
    '640,000 as reported."},\n'
    '   "debt_levels": {"value": "3200000", "page": 4, "clause": "Interest-bearing liabilities at balance '
    'date amounted to 3,200,000 in total."},\n'
    '   "going_concern": {"value": "true", "page": 5, "clause": "These financial statements have been '
    'prepared on a going concern basis."},\n'
    '   "contingent_liabilities": [{"summary": {"value": "guarantee of $500k given to a related entity", '
    '"page": 6, "clause": "Note 14 discloses a guarantee given in favour of a related entity."}}],\n'
    '   "related_party_transactions": [{"summary": {"value": "loan of $250k from a director outstanding at '
    'balance date", '
    '"page": 6, "clause": "Note 15 discloses a loan from a director outstanding at balance date."}}]}],\n'
    ' "entities": [{"name": {"value": "Northvale Pty Ltd", "page": 1, "clause": "The Borrower under the '
    'facility is Northvale Pty Ltd, incorporated in New South Wales."},\n'
    '   "entity_type": {"value": "company", "page": 1, "clause": "Northvale Pty Ltd is a proprietary company '
    'limited by shares."},\n'
    '   "role": {"value": "borrower", "page": 1, "clause": "Northvale Pty Ltd is named as borrower under the '
    'facility documents."}}],\n'
    ' "trusts": [{"name": {"value": "Northvale Family Trust", "page": 1, "clause": "Northvale Pty Ltd acts '
    'as trustee for the Northvale Family Trust."},\n'
    '   "entity_type": {"value": "trust", "page": 1, "clause": "The Northvale Family Trust is a '
    'discretionary trust established by deed."},\n'
    '   "role": {"value": "trustee structure", "page": 1, "clause": "The borrower holds the land in a '
    'trustee structure for the family trust."}}],\n'
    ' "ubos": [{"value": "Guarantor A", "page": 1, "clause": "The ultimate beneficial owner of the trust is '
    'Guarantor A."}],\n'
    ' "track_record": [{"claim": {"value": "delivered 18-unit townhouse project", "page": 7, '
    '"clause": "..."},\n'
    '   "project": {"value": "12 Smith St, Riverton", "page": 7, "clause": "The sponsor previously delivered '
    'the project at 12 Smith St, Riverton."},\n'
    '   "asset_type": {"value": "residential townhouses", "page": 7, "clause": "That project comprised '
    'residential townhouses delivered over two stages."},\n'
    '   "scale": {"value": "18", "page": 7, "clause": "The completed scheme comprised 18 units with a gross '
    'development value of 14,000,000."},\n'
    '   "year": {"value": "2022", "page": 7, "clause": "The scheme reached practical completion during the '
    '2022 calendar year."},\n'
    '   "verifiable_detail": {"value": "sold through Ray White, settled Q3 2022", "page": 7, '
    '"clause": "..."}}]}'
    "\n" + ANCHOR_RULE
)


class SponsorExtractor(Extractor):
    slot_key = "sponsor"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM/document boundary (override in tests) ---------------------------
    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> SponsorExtraction:
        if png_pages or text_context:
            return validate_extraction(
                SponsorExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    max_tokens=12000,
                    tool="extract:sponsor",
                ),
            )
        return validate_extraction(
            SponsorExtraction,
            document_json(
                pdf_b64 or "", _INSTRUCTION, self._model_name, max_tokens=12000, tool="extract:sponsor"
            ),
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
            _idx = select_pages(page_texts, self.slot_key)
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

        put("sponsor.cash_equity_to_date", data.cash_equity_to_date)
        put("sponsor.liquidity_cushion", data.liquidity_cushion)

        for fy in data.financial_years:
            key = safe_slug(fy.year) if fy.year else ""
            if not key:
                continue
            base = f"sponsor.financials[{key}]"
            note(f"{base}.revenue_trend", fy.revenue_trend)
            put(f"{base}.profitability", fy.profitability)
            put(f"{base}.debt_levels", fy.debt_levels)
            add_bool_fact(fields, flags, document_id, f"{base}.going_concern", fy.going_concern)
            for n in fy.contingent_liabilities:
                if n.summary and n.summary.value:
                    note(f"{base}.contingent_liabilities[{safe_slug(str(n.summary.value)[:48])}]", n.summary)
            for n in fy.related_party_transactions:
                if n.summary and n.summary.value:
                    put(
                        f"{base}.related_party_transactions[{safe_slug(str(n.summary.value)[:48])}]",
                        n.summary,
                    )

        for e in data.entities:
            if not (e.name and e.name.value):
                continue
            k = safe_slug(str(e.name.value)[:48])
            put(f"sponsor.structure.entities[{k}].name", e.name)
            put(f"sponsor.structure.entities[{k}].entity_type", e.entity_type)
            put(f"sponsor.structure.entities[{k}].role", e.role)
        for t in data.trusts:
            if not (t.name and t.name.value):
                continue
            k = safe_slug(str(t.name.value)[:48])
            put(f"sponsor.structure.trusts[{k}].name", t.name)
            put(f"sponsor.structure.trusts[{k}].entity_type", t.entity_type)
            put(f"sponsor.structure.trusts[{k}].role", t.role)
        for u in data.ubos:
            if u and u.value:
                put(f"sponsor.structure.ubos[{safe_slug(str(u.value)[:48])}]", u)

        for tr in data.track_record:
            primary = tr.claim or tr.project
            if not (primary and primary.value):
                continue
            k = safe_slug(str(primary.value)[:48])
            put(f"sponsor.claims[{k}].claim", tr.claim)
            put(f"sponsor.claims[{k}].project", tr.project)
            put(f"sponsor.claims[{k}].asset_type", tr.asset_type)
            put(f"sponsor.claims[{k}].scale", tr.scale)
            add_fact(fields, flags, document_id, f"sponsor.claims[{k}].year", tr.year, as_int=True)
            put(f"sponsor.claims[{k}].verifiable_detail", tr.verifiable_detail)
        return ExtractionOutput(fields=fields, flags=flags)

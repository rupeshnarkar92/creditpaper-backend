"""Valuation extractor — Instance B, the second-most-material document.

The valuation is the security anchor: values by basis (the LVR denominator), the
property, the methodology. Reads the report natively and emits each fact with its
page + clause anchor (D4). Values are normalised to canonical basis keys so the
metrics/materiality engine can consume them regardless of how the report words
them (as-is land vs in-one-line vs gross realisation). Comps are deferred to a
targeted second pass (Section 6's analytical layer); this pass takes the headline
facts the paper depends on.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from creditpaper.concurrency import pool
from creditpaper.extractors._doc import (
    ANCHOR_RULE,
    Anchored,
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
    "CompOut",
    "CompsExtraction",
    "MarketSiteExtraction",
    "ValuationExtraction",
    "ValuationExtractor",
    "ValueOut",
]

# Canonical basis keys the model must map each value onto (the rest of the
# pipeline keys on these — see model/derive.py, model/materiality.py).
_CANONICAL_BASES = (
    "as_is",
    "as_if_complete",
    "as_if_complete_incl_gst",
    "gross_realisation",
    "as_if_complete_one_line",
)


class ValueOut(BaseModel):
    basis: str  # one of _CANONICAL_BASES
    amount: Anchored | None = None


class ValuationExtraction(BaseModel):
    valuer: Anchored | None = None
    valuation_date: Anchored | None = None  # ISO YYYY-MM-DD
    approach: Anchored | None = None
    values: list[ValueOut] = []
    property_address: Anchored | None = None
    title_reference: Anchored | None = None
    zoning: Anchored | None = None
    site_area: Anchored | None = None  # sqm
    gfa_total: Anchored | None = None  # sqm


class CompOut(BaseModel):
    address: str  # comp address (becomes the key + location)
    sale_price: Anchored | None = None
    sale_date: Anchored | None = None  # ISO
    derived_rate: Anchored | None = None  # $/sqm of the relevant measure
    land_area: Anchored | None = None
    gfa_nla: Anchored | None = None
    adjustment: Anchored | None = None  # the valuer's adjustment, e.g. "+5%"
    adjustment_reason: Anchored | None = None
    condition: Anchored | None = None


class CompsExtraction(BaseModel):
    comps: list[CompOut] = []


class MarketSiteExtraction(BaseModel):
    market_conditions: Anchored | None = None
    supply_demand: Anchored | None = None
    locality_outlook: Anchored | None = None
    market_risks: list[Anchored] = []  # each a restated market-risk statement
    planning_controls: Anchored | None = None  # height / FSR / overlays
    site_characteristics: Anchored | None = None  # frontage / shape / access / aspect / topography
    property_risks: list[Anchored] = []  # flood / contamination / easement / heritage


_INSTRUCTION = (
    "You are extracting headline facts from a property valuation report supporting "
    "an Australian CRE private-credit loan. Extract ONLY what the report states. For "
    "EVERY value record the page and a verbatim clause as its anchor; OMIT "
    "anything you cannot locate to a clause (no anchor, no field). Numbers as plain "
    "digits (no $, commas). Dates as ISO YYYY-MM-DD. Map each value onto ONE of these "
    "canonical bases: as_is, as_if_complete (ex GST), as_if_complete_incl_gst, "
    "gross_realisation, as_if_complete_one_line. Return ONLY a JSON object (no prose) "
    "of this shape, omitting fields you cannot find:\n"
    '{"valuer": {"value": "Hartley Valuers", "page": 1, "clause": "This valuation was prepared by Hartley '
    'Valuers for mortgage purposes."},\n'
    ' "valuation_date": {"value": "2024-11-29", "page": 1, "clause": "The date of valuation is 29 November '
    '2024 and the inspection occurred earlier."},\n'
    ' "approach": {"value": "Direct Comparison", "page": 3, "clause": "We have adopted the Direct Comparison '
    'approach supported by settled local evidence."},\n'
    ' "values": [{"basis": "as_is", "amount": {"value": "7000000", "page": 2, "clause": "The market value of '
    'the property in its as is condition is 7,000,000."}},\n'
    '            {"basis": "as_if_complete", "amount": {"value": "17270000", "page": 2, "clause": "The as if '
    'complete market value of the proposed development is 17,270,000."}}],\n'
    ' "property_address": {"value": "14 Example Street, Suburb", "page": 1, "clause": "The subject property '
    'is situated at 14 Example Street, Suburb in New South Wales."},\n'
    ' "title_reference": {"value": "Lot 11 DP 10439", "page": 1, "clause": "The land is described as Lot 11 '
    'DP 10439 in the certificate of title."},\n'
    ' "zoning": {"value": "R2 Low Density Residential", "page": 4, "clause": "The land is zoned R2 Low '
    'Density Residential under the local environmental plan."},\n'
    ' "site_area": {"value": "689.2", "page": 4, "clause": "The site has a surveyed area of 689.2 square '
    'metres with street frontage."},\n'
    ' "gfa_total": {"value": "500.5", "page": 4, "clause": "The proposed gross floor area totals 500.5 '
    'square metres across both levels."}}'
    "\n" + ANCHOR_RULE
)

_COMPS_INSTRUCTION = (
    "Extract the COMPARABLE SALES the valuer relied on (the comparable-sales / "
    "market-evidence section). For each comparable record its address and, where "
    "shown, sale price, sale date (ISO YYYY-MM-DD), the derived rate ($/sqm of the "
    "relevant measure), land area and GFA/NLA (sqm), the valuer's adjustment to the "
    "subject (e.g. '+5%') and its reason, and condition. Numbers as plain digits. "
    "Anchor EVERY value (page + verbatim clause); omit a comp, or any field, you "
    "cannot locate. Return ONLY JSON:\n"
    '{"comps": [{"address": "12 Example St, Riverton", "sale_price": {"value": "8200000", '
    '"page": 41, "clause": "The property sold for 8,200,000 in an arms length transaction."}, '
    '"sale_date": {"value": "2024-08-15", "page": 41, "clause": '
    '"The sale settled on 15 August 2024 according to the records searched."}, '
    '"derived_rate": {"value": "16400", "page": 41, "clause": '
    '"The analysed rate for this comparable sale is 16,400 per square metre."}, '
    '"land_area": {"value": "650", "page": 41, "clause": '
    '"The comparable occupies a site area of 650 square metres with similar frontage."}, '
    '"gfa_nla": {"value": "500", "page": 41, "clause": '
    '"The improvements provide a gross floor area of 500 square metres."}, '
    '"adjustment": {"value": "+5%", "page": 42, '
    '"clause": "..."}, "adjustment_reason": {"value": "superior aspect", "page": 42, '
    '"clause": "..."}, "condition": {"value": "as new", "page": 41, "clause": "..."}}]}'
)

_MARKETSITE_INSTRUCTION = (
    "Extract the MARKET COMMENTARY and SITE/LOCATION detail, restating the valuer's "
    "own words concisely. Anchor every item (page + verbatim clause); omit what you "
    "cannot locate. Cover: overall market conditions; supply/demand for this product; "
    "the locality outlook; named market risks; the planning controls (height, FSR, "
    "any overlays); the physical site characteristics (frontage, shape, access, "
    "aspect, topography); and any valuer-identified property risks (flood/coastal "
    "hazard, contamination, easements, heritage). Return ONLY JSON:\n"
    '{"market_conditions": {"value": "...", "page": 8, "clause": "..."},\n'
    ' "supply_demand": {"value": "...", "page": 8, "clause": "..."},\n'
    ' "locality_outlook": {"value": "...", "page": 9, "clause": "..."},\n'
    ' "market_risks": [{"value": "buyer-pool concentration at this price point", '
    '"page": 9, "clause": "..."}],\n'
    ' "planning_controls": {"value": "9.5m height limit; FSR 0.6:1; no overlays", '
    '"page": 4, "clause": "..."},\n'
    ' "site_characteristics": {"value": "18m frontage, regular shape, level, street '
    'access east", "page": 4, "clause": "..."},\n'
    ' "property_risks": [{"value": "no flood affectation noted", "page": 4, "clause": "..."}]}'
)


class ValuationExtractor(Extractor):
    slot_key = "valuation"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- topic-targeted passes over the (cached) document; override in tests -----
    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> ValuationExtraction:
        if png_pages or text_context:
            return validate_extraction(
                ValuationExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:valuation/headline",
                ),
            )
        return validate_extraction(
            ValuationExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:valuation/headline"),
        )

    def _extract_comps(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> CompsExtraction:
        # A full comp schedule is many comparables × nine anchored fields each;
        # give it room so the JSON isn't truncated mid-array (the default 4k cut off
        # the Riverton report's comp set).
        if png_pages or text_context:
            return validate_extraction(
                CompsExtraction,
                document_json_pages(
                    png_pages,
                    _COMPS_INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    max_tokens=24000,
                    tool="extract:valuation/comps",
                ),
            )
        return validate_extraction(
            CompsExtraction,
            document_json(
                pdf_b64 or "",
                _COMPS_INSTRUCTION,
                self._model_name,
                max_tokens=24000,
                tool="extract:valuation/comps",
            ),
        )

    def _extract_market_site(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> MarketSiteExtraction:
        if png_pages or text_context:
            return validate_extraction(
                MarketSiteExtraction,
                document_json_pages(
                    png_pages,
                    _MARKETSITE_INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:valuation/market_site",
                ),
            )
        return validate_extraction(
            MarketSiteExtraction,
            document_json(
                pdf_b64 or "", _MARKETSITE_INSTRUCTION, self._model_name, tool="extract:valuation/market_site"
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
            _idx = select_pages(page_texts, "valuation")
            _pngs = pages_to_png(path, _idx)
            _ctx = "\n\n".join(
                f"[Page {pt['page']}]\n{pt.get('text', '')}"
                for pt in page_texts
                if (pt.get("page", 1) - 1) in _idx
            )
            # All three passes share the same selected pages — the headline values,
            # comps, and market sections are co-located in valuation reports.
            data = self._extract_raw(png_pages=_pngs, text_context=_ctx)
            with pool(2, thread_name_prefix="valuation") as ex:
                f_comps = ex.submit(self._extract_comps, png_pages=_pngs, text_context=_ctx)
                f_ms = ex.submit(self._extract_market_site, png_pages=_pngs, text_context=_ctx)
                comps, ms = f_comps.result(), f_ms.result()
        else:
            pdf_b64 = b64(path)
            # Headline pass first — it cache-WRITES the document. Comps and market/site
            # then both cache-READ it, so they run in parallel for free (no extra cache
            # write): wall-clock drops to headline + max(comps, market), not their sum.
            data = self._extract_raw(pdf_b64)
            with pool(2, thread_name_prefix="valuation") as ex:
                f_comps = ex.submit(self._extract_comps, pdf_b64)
                f_ms = ex.submit(self._extract_market_site, pdf_b64)
                comps, ms = f_comps.result(), f_ms.result()
        fields: dict[str, Fact] = {}
        flags: list[str] = []

        def put(p: str, anc: Anchored | None) -> None:
            add_fact(fields, flags, document_id, p, anc)

        def note(p: str, anc: Anchored | None) -> None:
            add_note_fact(fields, flags, document_id, p, anc)

        # Headline (values + property basics + methodology).
        put("valuation.valuer", data.valuer)
        put("valuation.valuation_date", data.valuation_date)
        note("valuation.methodology.approach", data.approach)
        put("valuation.property.address", data.property_address)
        put("valuation.property.title_reference", data.title_reference)
        put("valuation.property.zoning", data.zoning)
        put("valuation.property.site_area", data.site_area)
        put("valuation.property.gfa_total", data.gfa_total)
        for v in data.values:
            key = safe_slug(v.basis)
            if key not in _CANONICAL_BASES:
                flags.append(f"value basis '{v.basis}' not canonical — skipped")
                continue
            put(f"valuation.values_by_basis[{key}]", v.amount)

        # Comparable sales -> comp_set[<address>].* (Section 6's comp-stress input).
        for c in comps.comps:
            if not c.address:
                continue
            base = f"valuation.comp_set[{safe_slug(c.address)}]"
            put(
                f"{base}.location",
                Anchored(
                    value=c.address,
                    page=(c.sale_price.page if c.sale_price else None),
                    clause=(c.sale_price.clause if c.sale_price else "comparable"),
                ),
            )
            put(f"{base}.sale_price", c.sale_price)
            put(f"{base}.sale_date", c.sale_date)
            put(f"{base}.derived_rate", c.derived_rate)
            put(f"{base}.land_area", c.land_area)
            put(f"{base}.gfa_nla", c.gfa_nla)
            put(f"{base}.adjustment", c.adjustment)
            put(f"{base}.adjustment_reason", c.adjustment_reason)
            put(f"{base}.condition", c.condition)

        # Market commentary + site/location (Sections 4 and 6).
        note("valuation.market.conditions", ms.market_conditions)
        note("valuation.market.supply_demand", ms.supply_demand)
        note("valuation.market.locality_outlook", ms.locality_outlook)
        for r in ms.market_risks:
            if r.value:
                note(f"valuation.market.market_risks[{safe_slug(str(r.value)[:40])}]", r)
        put("valuation.property.planning_controls", ms.planning_controls)
        put("valuation.subject_attributes.location", ms.site_characteristics)
        for r in ms.property_risks:
            if r.value:
                note(f"valuation.property_risks[{safe_slug(str(r.value)[:40])}]", r)
        return ExtractionOutput(fields=fields, flags=flags)

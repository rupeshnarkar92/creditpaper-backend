"""Builder extractor — builder capacity (check #14), from the builder's licence and
financials. On construction the builder is a primary risk: a replacement builder on a
part-built project is costly and rarely takes warranty risk, and a builder funds trades
in advance, so their cash position matters. Captures the licence (number, currency,
category), the cash position and any defaults, and the site manager — conclusions and
headline figures only (D7), not full financial statements.

Two source documents commonly feed this slot (licence, financials); the extractor reads
whichever it is given and emits what that document supports. Each fact anchored (D4).
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

__all__ = ["Anchored", "BuilderExtraction", "BuilderExtractor", "DefaultOut"]


class DefaultOut(BaseModel):
    summary: Anchored | None = None


class BuilderExtraction(BaseModel):
    licence_number: Anchored | None = None
    licence_current: Anchored | None = None  # "true"/"false": current/in force
    licence_category: Anchored | None = None  # the class of work licensed (vs project scope)
    cash_position: Anchored | None = None  # cash / net assets headline
    site_manager: Anchored | None = None
    defaults: list[DefaultOut] = []  # judgments / defaults / insolvency events


_INSTRUCTION = (
    "You are extracting BUILDER-CAPACITY facts for a CRE construction loan, from a "
    "builder's licence record and/or the builder's financial statements/ATO/credit "
    "material. Extract ONLY what the document states; anchor EVERY value to a page + "
    "verbatim clause; OMIT what you cannot locate (no anchor, no field). Numbers "
    "as plain digits. Capture: the builder's LICENCE number; whether the licence is "
    "CURRENT/in force (licence_current true/false); the licence CATEGORY/class of work; "
    "the builder's CASH POSITION or net assets (headline figure the document states — "
    "NOT a full reconstruction of the accounts); the SITE MANAGER / nominated "
    "supervisor; and any DEFAULTS, judgments, payment defaults or insolvency events on "
    "record. Return ONLY a JSON object of this shape, omitting fields you cannot find:\n"
    '{"licence_number": {"value": "228447C", "page": 1, "clause": '
    '"The holder of Licence No. 228447C is authorised to carry out residential building work."},\n'
    ' "licence_current": {"value": "true", "page": 1, "clause": '
    '"The licence is current and in force as at the date of this record."},\n'
    ' "licence_category": {"value": "Building (open)", "page": 1, "clause": '
    '"The licence category recorded is Building (open) with no conditions presently imposed."},\n'
    ' "cash_position": {"value": "850000", "page": 3, "clause": '
    '"Cash at bank at 30 June 2025 was 850,000 across all operating accounts."},\n'
    ' "site_manager": {"value": "Mark Casey", "page": 1, "clause": '
    '"The nominated supervisor for the works is Mark Casey of Casey Bros Building."},\n'
    ' "defaults": [{"summary": {"value": "no adverse judgments recorded", "page": 2, "clause": '
    '"The search returned no adverse judgments recorded against the builder in the period."}}]}'
    "\n" + ANCHOR_RULE
)


class BuilderExtractor(Extractor):
    slot_key = "builder"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> BuilderExtraction:
        if png_pages or text_context:
            return validate_extraction(
                BuilderExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:builder",
                ),
            )
        return validate_extraction(
            BuilderExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:builder"),
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

        put("builder.licence.number", data.licence_number)
        put("builder.licence.category", data.licence_category)
        put("builder.financials.cash_position", data.cash_position)
        put("builder.site_manager", data.site_manager)
        add_bool_fact(fields, flags, document_id, "builder.licence.current", data.licence_current)
        for d in data.defaults:
            if d.summary and d.summary.value:
                note(f"builder.financials.defaults[{safe_slug(str(d.summary.value)[:48])}]", d.summary)
        return ExtractionOutput(fields=fields, flags=flags)

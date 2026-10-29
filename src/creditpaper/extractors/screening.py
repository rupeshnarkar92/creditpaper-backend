"""KYC / screening extractor — CONCLUSIONS ONLY (D7, Instance E).

Data minimisation is a hard rule: the KYC slot takes the screening CONCLUSIONS, never
raw identity documents. This reads a credit/AML screening output (VEDA/Equifax,
sanctions/PEP/adverse-media screen) and records only the verdicts a credit paper needs —
any adverse credit findings, the AML flags (sanctions / PEP / adverse media), and whether
identity was verified. It does NOT extract dates of birth, document numbers, or any raw
identity data. Each fact anchored (D4); the screening conclusions feed §5/§8 and checks
#10/#12.
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

__all__ = ["Anchored", "FindingOut", "ScreeningExtraction", "ScreeningExtractor"]


class FindingOut(BaseModel):
    summary: Anchored | None = None  # an adverse credit finding (conclusion, not raw data)


class ScreeningExtraction(BaseModel):
    provider: Anchored | None = None  # the screening provider (e.g. Equifax, FirstAML)
    sanctions: Anchored | None = None  # "true"/"false": a sanctions hit
    pep: Anchored | None = None  # "true"/"false": politically-exposed person
    adverse_media: Anchored | None = None  # "true"/"false": adverse media
    identity_verified: Anchored | None = None  # "true"/"false"
    credit_findings: list[FindingOut] = []  # adverse credit findings (defaults, judgments)


_INSTRUCTION = (
    "You are extracting KYC / SCREENING CONCLUSIONS for a CRE construction loan, from a "
    "credit check (VEDA/Equifax) and/or an AML/sanctions screening output. CRITICAL DATA "
    "MINIMISATION RULE: extract ONLY the screening CONCLUSIONS/verdicts — NEVER any raw "
    "identity data (no dates of birth, no document/passport/licence numbers, no "
    "addresses). Anchor EVERY value to a page + verbatim clause; OMIT what you "
    "cannot locate. Capture: the screening PROVIDER; whether there is a SANCTIONS hit "
    "(true/false); whether the party is a PEP (true/false); whether there is ADVERSE "
    "MEDIA (true/false); whether IDENTITY was verified (true/false); and any ADVERSE "
    "CREDIT findings (defaults, judgments, insolvencies) as short conclusions. Where a "
    "screen is clear, say so (e.g. sanctions false). Return ONLY a JSON object of this "
    "shape, omitting fields you cannot find:\n"
    '{"provider": {"value": "Equifax", "page": 1, "clause": '
    '"This report was prepared by Equifax at the request of the subscribing member."},\n'
    ' "sanctions": {"value": "false", "page": 2, "clause": '
    '"The sanctions screen returned no matches against any listed party."},\n'
    ' "pep": {"value": "false", "page": 2, "clause": '
    '"The subject was not identified as a politically exposed person on screening."},\n'
    ' "adverse_media": {"value": "false", "page": 2, "clause": '
    '"No adverse media results were returned for the subject of this screen."},\n'
    ' "identity_verified": {"value": "true", "page": 1, "clause": '
    '"Identity was verified to the required standard using two acceptable sources."},\n'
    ' "credit_findings": [{"summary": {"value": "no adverse credit findings recorded", '
    '"page": 1, "clause": '
    '"There were no adverse credit findings recorded against the subject in this search."}}]}'
    "\n" + ANCHOR_RULE
)


class ScreeningExtractor(Extractor):
    slot_key = "kyc"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> ScreeningExtraction:
        if png_pages or text_context:
            return validate_extraction(
                ScreeningExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:kyc",
                ),
            )
        return validate_extraction(
            ScreeningExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:kyc"),
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
            _idx = select_pages(page_texts, "sponsor_kyc")
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

        put("screening.aml.provider", data.provider)
        add_bool_fact(fields, flags, document_id, "screening.aml.sanctions", data.sanctions)
        add_bool_fact(fields, flags, document_id, "screening.aml.pep", data.pep)
        add_bool_fact(fields, flags, document_id, "screening.aml.adverse_media", data.adverse_media)
        add_bool_fact(fields, flags, document_id, "screening.identity.verified", data.identity_verified)
        for f in data.credit_findings:
            if f.summary and f.summary.value:
                note(f"screening.credit.adverse_findings[{safe_slug(str(f.summary.value)[:48])}]", f.summary)
        return ExtractionOutput(fields=fields, flags=flags)

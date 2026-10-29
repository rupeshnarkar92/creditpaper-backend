"""Insurance extractor — certificates of currency (check #7). Confirms cover is in
force across the facility term and that the financier / SPV is noted as an interested
party. Captures the cover type, sum insured, the named parties, and the period — the
conclusions a credit paper needs (D7), not the full policy wording. Multiple
certificates land keyed (contract works, public liability, …). Each fact anchored (D4).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from creditpaper.extractors._doc import (
    ANCHOR_RULE,
    Anchored,
    add_fact,
    b64,
    document_json,
    document_json_pages,
    validate_extraction,
)
from creditpaper.extractors.base import ExtractionOutput, Extractor
from creditpaper.model.citations import safe_slug
from creditpaper.model.core import Fact

__all__ = ["Anchored", "CertificateOut", "InsuranceExtraction", "InsuranceExtractor"]


class CertificateOut(BaseModel):
    cover_type: Anchored | None = None  # contract works / public liability / professional indemnity
    sum_insured: Anchored | None = None
    period_start: Anchored | None = None  # ISO
    period_end: Anchored | None = None  # ISO
    parties: list[Anchored] = []  # named insureds / interested parties


class InsuranceExtraction(BaseModel):
    certificates: list[CertificateOut] = []


_INSTRUCTION = (
    "You are extracting INSURANCE facts from one or more certificates of currency for a "
    "CRE construction loan. Extract ONLY what the certificate states; anchor EVERY value "
    "to a page + verbatim clause; OMIT what you cannot locate (no anchor, no "
    "field). Numbers as plain digits; dates ISO YYYY-MM-DD. For EACH certificate capture: "
    "the COVER TYPE (contract works / public liability / professional indemnity / etc.); "
    "the SUM INSURED; the PERIOD (start and end); and the named INSUREDS / interested "
    "parties (so it can be checked that the financier and the SPV borrower are noted). "
    "Return ONLY a JSON object of this shape, omitting fields you cannot find:\n"
    '{"certificates": [{"cover_type": {"value": "Contract Works", "page": 1, "clause": '
    '"This certificate evidences Contract Works cover for the project described below."},\n'
    '   "sum_insured": {"value": "9000000", "page": 1, "clause": '
    '"The Sum Insured under this policy is 9,000,000 for the period shown."},\n'
    '   "period_start": {"value": "2025-02-01", "page": 1, "clause": '
    '"The period of insurance commences on 01/02/2025 and continues until expiry."},\n'
    '   "period_end": {"value": "2026-05-01", "page": 1, "clause": '
    '"The period of insurance expires at 4pm on 01/05/2026 unless renewed earlier."},\n'
    '   "parties": [{"value": "Northvale Pty Ltd", "page": 1, "clause": '
    '"The Insured is Northvale Pty Ltd as trustee for the Northvale Family Trust."},\n'
    '               {"value": "Lender Nominees Pty Ltd", "page": 1, "clause": '
    '"Lender Nominees Pty Ltd is noted as mortgagee and interested party on this policy."}]}]}'
    "\n" + ANCHOR_RULE
)


class InsuranceExtractor(Extractor):
    slot_key = "insurance"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> InsuranceExtraction:
        if png_pages or text_context:
            return validate_extraction(
                InsuranceExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:insurance",
                ),
            )
        return validate_extraction(
            InsuranceExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:insurance"),
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
            _idx = select_pages(page_texts, "insurance")
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

        for cert in data.certificates:
            if cert.cover_type is None or not str(cert.cover_type.value or "").strip():
                continue
            key = safe_slug(str(cert.cover_type.value)[:40])
            base = f"insurance[{key}]"
            put(f"{base}.cover_type", cert.cover_type)
            put(f"{base}.sum", cert.sum_insured)
            put(f"{base}.period_start", cert.period_start)
            put(f"{base}.period_end", cert.period_end)
            for p in cert.parties:
                if p.value:
                    put(f"{base}.parties_noted[{safe_slug(str(p.value)[:40])}]", p)
        return ExtractionOutput(fields=fields, flags=flags)

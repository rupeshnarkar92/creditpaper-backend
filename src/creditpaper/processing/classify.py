"""Keyword-based document classifier.

8 kinds: its, valuation, da, qs, building_contract, sponsor_kyc,
insurance, other (fallback). No LLM. S3 (Fact Extraction) can
override.

S2 only writes `deal_documents.document_kind` when confidence >= 0.5
or the slot is currently empty. User edits in the UI win.
"""

from __future__ import annotations

KINDS: dict[str, list[str]] = {
    "its": [
        "indicative term sheet",
        "term sheet",
        "facility",
        "tranche",
        "interest rate",
        "maturity",
        "commitment fee",
    ],
    "valuation": [
        "valuation",
        "market value",
        "as is",
        "as if complete",
        "gross realisation",
        "net realisation",
        "valuer",
    ],
    "da": [
        "development application",
        "consent authority",
        "lapsing date",
        "section 4.55",
        "conditions of consent",
    ],
    "qs": [
        "quantity surveyor",
        "cost report",
        "construction cost",
        "gfa",
        "lettable area",
        "lockup",
    ],
    "building_contract": [
        "building contract",
        "lump sum",
        "cost plus",
        "practical completion",
        "defects liability",
        "retention",
    ],
    "sponsor_kyc": [
        "know your customer",
        "kyc",
        "aml",
        "beneficial owner",
        "director",
        "sponsor track record",
    ],
    "insurance": [
        "policy schedule",
        "insured value",
        "premium",
        "excess",
        "construction all risk",
        "public liability",
    ],
    "other": [],
}


def classify_document(
    text: str,
    filename: str,
    *,
    page_texts: list[dict] | None = None,
) -> dict:
    low = text.lower()
    fn_low = filename.lower()
    scores: dict[str, int] = {}
    for kind, kws in KINDS.items():
        if not kws:
            continue
        scores[kind] = sum(1 for k in kws if k in low or k in fn_low)

    if not scores or all(v == 0 for v in scores.values()):
        return {
            "kind": "other",
            "confidence": 0.0,
            "signals": [],
            "scores": {},
        }

    kind = max(scores, key=scores.get)
    score = scores[kind]
    confidence = min(0.95, score / 5.0 + 0.1) if score else 0.0
    return {
        "kind": kind,
        "confidence": round(confidence, 2),
        "signals": [k for k, v in scores.items() if v > 0],
        "scores": scores,
    }

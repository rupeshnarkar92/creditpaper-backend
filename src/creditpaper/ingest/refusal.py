"""D7 refusal gate: decide what we will not read, before we read it.

Data minimisation is a refusal at the door, not a filter afterwards. Once a
passport has been OCR'd and indexed, refusing it later is theatre. So this runs
on the filename and the first page's text only - the cheapest signals available
- and it runs before parsing, caching or extraction.

A refusal is a visible outcome the analyst sees, never a silent drop: a document
that vanishes without explanation gets re-uploaded, and the second attempt
usually succeeds by accident.

The class keys mirror `document_refused_classes` in the database and
`refused_classes` in the slot taxonomy; a test asserts the two stay in step.
Matching lives here rather than in a database lookup because the gate sits on
the hot path of every uploaded file.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

REFUSED_CLASSES: tuple[str, ...] = ("raw_identity", "pricing_model")

_IDENTITY_FILENAME = re.compile(
    r"\b(passport|drivers?[\s_-]?licen[cs]e|licence[\s_-]?card|medicare|birth[\s_-]?certificate"
    r"|citizenship[\s_-]?certificate|photo[\s_-]?id|identity[\s_-]?document)\b",
    re.I,
)
_IDENTITY_TEXT = re.compile(
    r"\b(australian passport|passport no\.?|driver licence|driver's licence|licence no\.?"
    r"|medicare card|date of birth|place of birth|card number)\b",
    re.I,
)
_PRICING_FILENAME = re.compile(
    r"\b(pricing[\s_-]?model|pricing[\s_-]?tool|rate[\s_-]?card[\s_-]?model|margin[\s_-]?model"
    r"|deal[\s_-]?calculator)\b",
    re.I,
)
_PRICING_TEXT = re.compile(r"\b(pricing model|margin grid|internal pricing|rate build[\s-]?up)\b", re.I)

_REASONS = {
    "raw_identity": (
        "Raw identity documents are refused (D7). Upload the KYC screening conclusions instead; "
        "the paper cites the screening outcome, not the identity document."
    ),
    "pricing_model": (
        "Pricing models are refused (D5). Facility economics come from the signed ITS, where pricing "
        "is fully disclosed."
    ),
}


_SEPARATORS = re.compile(r"[._\-]+")


def _separated(text: str) -> str:
    """Turn filename separators into spaces so word boundaries mean something.

    `\b` does not match between `passport` and `_`, because underscore is a
    word character. Without this, `passport_scan_copy.pdf` - the usual shape
    of the exact thing D7 refuses - walks straight through the gate.
    """
    return _SEPARATORS.sub(" ", text)


class RefusalOutcome(BaseModel):
    refused: bool
    reason: str | None = None
    refused_class: str | None = None


def check_refusal(filename: str, first_page_text: str) -> RefusalOutcome:
    name = _separated(filename or "")
    head = _separated((first_page_text or "")[:4000])

    if _IDENTITY_FILENAME.search(name) or _IDENTITY_TEXT.search(head):
        return RefusalOutcome(refused=True, reason=_REASONS["raw_identity"], refused_class="raw_identity")
    if _PRICING_FILENAME.search(name) or _PRICING_TEXT.search(head):
        return RefusalOutcome(refused=True, reason=_REASONS["pricing_model"], refused_class="pricing_model")
    return RefusalOutcome(refused=False)

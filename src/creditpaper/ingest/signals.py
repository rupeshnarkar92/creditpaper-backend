"""Cheap, deterministic evidence for what a document is.

These signals do not decide the slot - measured on real deal folders they settle
51% of documents at 82% accuracy, which is not a filing system. They do three
other jobs well: they corroborate the model's proposal (agreement is worth more
than either alone), they cost nothing, and they give the analyst a reason to
read ("folder 04 QS, cover page 'Cost Report No. 3'") instead of a bare verdict.

`qs_report` carries its own signatures because it was the worst-performing slot
in the corpus at 14% recall, losing to neighbouring construction documents,
while `valuation` sat at 69%. Both have a dedicated regression test.

The neighbour signal is the learning loop: it compares against documents this
tenant has already confirmed, so a firm's own naming conventions and its panel
valuers' templates raise accuracy over time. The labelled corpus is passed in
rather than fetched so this module stays pure and testable without a database.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

PARTY_SENSITIVE_SLOTS: frozenset[str] = frozenset(
    {"borrower_financials", "builder_financials", "ato_statements", "builder_ato"}
)

_SHINGLE = 4
_FILENAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"contract\s*works|cwi\b", "contract_works_insurance"),
    (r"public\s*liability|\bpli\b", "public_liability_insurance"),
    (r"certificate\s*of\s*currency|cert\s*of\s*currency", "contract_works_insurance"),
    (r"quantity\s*surveyor|\bqs\b|cost\s*report|cost\s*to\s*complete", "qs_report"),
    (r"valuation|valuer", "valuation"),
    (r"building\s*contract|as\s*4000|abic", "building_contract"),
    (
        r"development\s*application|\bda\b\s*(approval|consent)|notice\s*of\s*determination",
        "development_approval",
    ),
    (r"construction\s*certificate|\bcc\b\s*approval", "construction_certificate"),
    (r"tripartite|side\s*deed", "tripartite"),
    (r"presale|pre-sale", "presales"),
    (r"price\s*list", "price_list"),
    (r"term\s*sheet|\bits\b|indicative\s*terms", "signed_its"),
    (r"trust\s*deed|constitution|share\s*certificate", "company_trust_documents"),
    (r"builder.*licen[cs]e|licen[cs]e.*builder", "builder_licence"),
    (r"land\s*tax", "land_tax_clearance"),
    (r"water\s*rates", "water_rates"),
    (r"council\s*rates", "council_rates"),
    (r"lease", "leases"),
    (r"purchase\s*contract|contract\s*of\s*sale", "purchase_contract"),
    (r"payout\s*figure", "payout_figure"),
    (r"deed\s*of\s*priority", "deed_of_priority"),
    (r"occupancy\s*certificate|\boc\b\s*issued", "occupancy_certificate"),
    (r"schedule\s*of\s*finishes", "schedule_of_finishes"),
    (r"kyc|screening", "kyc_screening"),
)

# Ordered: the first match wins, so the more specific pattern is listed first.
_SIGNATURE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"quantity\s*surveyor|cost\s*to\s*complete|progress\s*claim\s*certif"
        r"|initial\s*cost\s*report|cost\s*report\s*no",
        "qs_report",
    ),
    (
        r"market\s*value\s*as\s*at|certified\s*practising\s*valuer|valuation\s*report|api\s*member",
        "valuation",
    ),
    (
        r"certificate\s*of\s*currency|policy\s*number|insured\s*amount|period\s*of\s*insurance",
        "contract_works_insurance",
    ),
    (
        r"notice\s*of\s*determination|development\s*consent|conditions\s*of\s*consent",
        "development_approval",
    ),
    (r"as\s*4000|abic\s*sw|lump\s*sum\s*contract|contract\s*sum", "building_contract"),
    (r"indicative\s*terms\s*sheet|facility\s*limit|line\s*fee|establishment\s*fee", "signed_its"),
    (
        r"running\s*balance\s*account|integrated\s*client\s*account|australian\s*taxation\s*office",
        "ato_statements",
    ),
    (
        r"statement\s*of\s*financial\s*position|profit\s*and\s*loss|balance\s*sheet",
        "borrower_financials",
    ),
    (r"contract\s*for\s*the\s*sale\s*of\s*land|vendor\s*and\s*purchaser", "purchase_contract"),
    (r"builder\s*licence\s*no|licen[cs]e\s*class|nsw\s*fair\s*trading", "builder_licence"),
)
_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^development\s*application", "development_approval"),
    (r"^valuation\s*report", "valuation"),
    (r"^quantity\s*surveyor", "qs_report"),
    (r"^certificate\s*of\s*currency", "contract_works_insurance"),
    (r"^indicative\s*terms\s*sheet", "signed_its"),
    (r"^construction\s*certificate", "construction_certificate"),
    (r"^building\s*contract", "building_contract"),
)


class Signal(BaseModel):
    signal: str
    slot_key: str | None
    score: float
    detail: str


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "")[:20000].lower())
    if len(words) < _SHINGLE:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)}


def filename_signal(filename: str, path: str) -> Signal:
    haystack = f"{path} {filename}".lower()
    for pattern, slot in _FILENAME_PATTERNS:
        if re.search(pattern, haystack):
            return Signal(
                signal="filename",
                slot_key=slot,
                score=0.6,
                detail=f"filename or folder matched {pattern!r}",
            )
    return Signal(signal="filename", slot_key=None, score=0.0, detail="no filename or folder match")


def fingerprint_signal(first_pages: dict[int, str]) -> Signal:
    if not first_pages:
        return Signal(signal="fingerprint", slot_key=None, score=0.0, detail="no page text")
    first = first_pages[sorted(first_pages)[0]]
    lines = [line.strip() for line in first.splitlines() if line.strip()]
    head = " ".join(lines[:3]).lower()
    for pattern, slot in _TITLE_PATTERNS:
        if re.search(pattern, head):
            return Signal(
                signal="fingerprint",
                slot_key=slot,
                score=0.7,
                detail=f"title block matched {pattern!r}",
            )
    return Signal(signal="fingerprint", slot_key=None, score=0.0, detail="title block not recognised")


def signature_signal(first_pages: dict[int, str]) -> Signal:
    body = " ".join(first_pages[n] for n in sorted(first_pages))[:20000].lower()
    if not body.strip():
        return Signal(signal="signature", slot_key=None, score=0.0, detail="no page text")
    for pattern, slot in _SIGNATURE_PATTERNS:
        if re.search(pattern, body):
            return Signal(
                signal="signature",
                slot_key=slot,
                score=0.65,
                detail=f"document signature matched {pattern!r}",
            )
    return Signal(signal="signature", slot_key=None, score=0.0, detail="no document signature matched")


def neighbour_signal(text: str, labelled: list[tuple[str, str]]) -> Signal:
    if not labelled:
        return Signal(signal="neighbour", slot_key=None, score=0.0, detail="no confirmed documents yet")
    target = _shingles(text)
    best_slot: str | None = None
    best_score = 0.0
    for other_text, slot in labelled:
        other = _shingles(other_text)
        if not target or not other:
            continue
        score = len(target & other) / len(target | other)
        if score > best_score:
            best_slot, best_score = slot, score
    if best_slot is None or best_score < 0.05:
        return Signal(signal="neighbour", slot_key=None, score=0.0, detail="no similar confirmed document")
    return Signal(
        signal="neighbour",
        slot_key=best_slot,
        score=round(min(best_score * 2, 0.8), 3),
        detail=f"closest confirmed document in this firm scored {best_score:.2f}",
    )


def all_signals(
    *,
    filename: str,
    path: str,
    first_pages: dict[int, str],
    labelled: list[tuple[str, str]],
) -> list[Signal]:
    body = " ".join(first_pages[n] for n in sorted(first_pages))
    return [
        filename_signal(filename, path),
        fingerprint_signal(first_pages),
        signature_signal(first_pages),
        neighbour_signal(body, labelled),
    ]

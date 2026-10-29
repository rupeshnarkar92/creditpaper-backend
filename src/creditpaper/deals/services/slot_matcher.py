"""Deterministic slot matcher: filenames (+ the S2 document_kind) to proposed slots.

No LLM, no content peek (Flow E defers that to Phase 2). The matcher only PROPOSES (D3):
its output is a plan the board applies as `proposed` slots and `candidate` rows, never a
confirmation.

Keywords come from two places:
  1. `harness/route.py` `_SLOT_KEYWORDS`, the routing the extractors already use. Each
     routing key is mapped onto the taxonomy slot(s) it feeds (`route_keyword_targets`),
     so extending route.py extends the board without an edit here.
  2. `EXTRA_KEYWORDS` below, for taxonomy slots route.py cannot place yet.

Scoring, per file and per applicable slot: a slot matches when any of its keywords
occurs in the normalised filename (word-boundary match). The best slot is one matched on
wording no other matched slot shares, then the longest matched keyword (more specific
wording wins; ties keep taxonomy order). Confidence starts at 0.6, +0.1 for a matched
keyword of 12+ characters, +0.1 when the S2 classifier's document_kind agrees, +0.05 per
additional distinct keyword, minus 0.15 when another slot also matched; capped to
[0.3, 0.95]. A file that matches no keyword but whose document_kind maps to an applicable
slot is proposed at 0.35.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from creditpaper.deals.services.slot_taxonomy import RefusedClass, SlotType
from creditpaper.harness.route import _SLOT_KEYWORDS, _canonical_score

# Keywords for taxonomy slots route.py cannot place. Generic document wording only:
# no lender, provider, deal or party names.
EXTRA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "company_trust_documents": (
        "trust deed",
        "deed of trust",
        "unit trust",
        "share certificate",
        "share register",
        "constitution",
        "company extract",
        "company search",
        "asic extract",
        "certificate of registration",
    ),
    "borrower_profile": ("company profile", "borrower profile", "developer profile", "sponsor profile"),
    "funding_table": ("funding table", "funding schedule", "sources and uses", "source and application"),
    "statement_of_position": ("statements of position", "sop"),
    "ato_statements": ("ato", "ato portal", "ato statement", "integrated client account", "running balance"),
    "land_tax_clearance": ("land tax",),
    "water_rates": ("water rates", "water certificate", "water clearance", "water account"),
    "council_rates": ("council rates", "rates notice", "rates receipt", "rate notice"),
    "equity_evidence": ("equity invoice", "equity invoices", "equity evidence", "invoices", "tax invoice"),
    "purchase_contract": ("purchase contract", "contract for sale", "contract of sale", "sale of land"),
    "leases": ("lease", "leases", "tenancy agreement", "tenancy schedule"),
    "public_liability_insurance": ("public liability",),
    "contract_works_insurance": (
        "contract works",
        "workers compensation",
        "workers comp",
        "construction all risk",
    ),
    "building_insurance": (
        "building insurance",
        "landlord insurance",
        "replacement value",
        "strata insurance",
    ),
    "valuation": ("val",),
    "development_approval": ("stamped plans", "development consent", "planning permit"),
    "da_submission_pack": (
        "da submission",
        "statement of environmental effects",
        "geotechnical",
        "geotech",
        "arborist",
        "acoustic report",
        "traffic report",
        "stormwater",
        "landscape plan",
        "heritage report",
        "contamination",
        "environmental site assessment",
        "bca report",
    ),
    "authority_contributions": (
        "contributions",
        "infrastructure charges",
        "planning agreement",
        "authority fees",
        "section 7.11",
        "section 7.12",
    ),
    "occupancy_certificate": (
        "occupation certificate",
        "occupancy certificate",
        "occupancy permit",
        "certificate of occupancy",
    ),
    "sales_contract_template": (
        "pro forma contract",
        "proforma contract",
        "pro forma",
        "proforma",
        "sales contract template",
        "off the plan contract",
        "standard contract",
    ),
    "presales": ("presale", "presales", "pre sale", "pre sales", "exchanged contract", "exchanged contracts"),
    "price_list": ("price list", "pricelist", "sales price list"),
    "contracts_eois": (
        "expression of interest",
        "expressions of interest",
        "eoi",
        "eois",
        "exchanged contract",
        "exchanged contracts",
        "sales register",
    ),
    "deposits_in_trust": ("deposits held", "deposit trust", "trust account", "deposits in trust"),
    "marketing": ("marketing", "sales agent", "agency agreement", "brochure"),
    "consultant_profiles": ("consultant profile", "consultant profiles", "capability statement"),
    "building_contract": ("building contract",),
    "construction_certificate": ("construction certificate", "building permit"),
    "builder_financials": ("builder financials", "builder accounts"),
    "builder_licence": ("contractor licence", "builder license", "builders license"),
    "builder_profile": ("builder profile", "builder references", "builder capability"),
    "site_personnel": ("site manager", "site personnel", "site supervisor", "key personnel"),
    "builder_ato": ("builder ato", "ato builder", "builder ato statement", "builder ato statements"),
    "tripartite": ("tripartite", "side deed", "step in deed"),
    "schedule_of_finishes": ("schedule of finishes", "finishes schedule"),
    "legal_review": (
        "title search",
        "survey plan",
        "survey plans",
        "legal review",
        "certificate of title",
        "deposited plan",
        "strata plan",
    ),
    "loan_documentation": ("loan documentation", "loan documents", "mortgage deed", "general security deed"),
    "payout_figure": ("payout", "payout figure", "payout letter", "discharge figure"),
    "deed_of_priority": ("deed of priority", "priority deed"),
    "independent_legal_advice": (
        "independent legal advice",
        "ila",
        "solicitors certificate",
        "legal advice certificate",
    ),
    "rfi_sheet": ("rfi", "request for information", "information request"),
}

_INSURANCE_SLOTS = ("public_liability_insurance", "contract_works_insurance", "building_insurance")
_SOP_WORDS = ("statement of position", "assets and liabilities", "asset and liability", "net asset position")

# S2 classifier kinds (processing/classify.py) and the slots they support.
KIND_TARGETS: dict[str, tuple[str, ...]] = {
    "its": ("signed_its",),
    "valuation": ("valuation",),
    "da": ("development_approval",),
    "qs": ("qs_report",),
    "building_contract": ("building_contract",),
    "sponsor_kyc": ("kyc_screening",),
    "insurance": _INSURANCE_SLOTS,
}


def route_keyword_targets(route_key: str, keyword: str) -> tuple[str, ...]:
    """The taxonomy slot(s) a route.py keyword feeds."""
    if route_key == "its":
        return ("signed_its",)
    if route_key == "valuation":
        return ("valuation",)
    if route_key == "qs":
        return ("qs_report",)
    if route_key == "building_contract":
        return ("building_contract",)
    if route_key == "da":
        return ("development_approval",)
    if route_key == "kyc":
        return ("kyc_screening",)
    if route_key == "sponsor_structure":
        return ("corporate_structure",)
    if route_key == "sponsor_track_record":
        return ("borrower_profile",)
    if route_key == "builder":
        if "licen" in keyword:
            return ("builder_licence",)
        if "ato" in keyword.split():
            return ("builder_ato",)
        return ("builder_financials",)
    if route_key == "sponsor_financials":
        if keyword in _SOP_WORDS:
            return ("statement_of_position",)
        return ("borrower_financials",)
    if route_key == "insurance":
        if "contract works" in keyword:
            return ("contract_works_insurance",)
        if "public liability" in keyword:
            return ("public_liability_insurance",)
        return _INSURANCE_SLOTS
    return ()


def routing_keys_for(taxonomy_key: str) -> tuple[str, ...]:
    """The routing key(s) whose extractor reads this taxonomy slot.

    The inverse of `route_keyword_targets`, and it has to be built by walking
    every keyword rather than probing each route key once: `builder` maps to
    `builder_licence`, `builder_ato` or `builder_financials` depending on the
    KEYWORD, so a single probe would silently miss two of the three.

    Empty is a valid answer, not a failure. Most taxonomy slots have no
    extractor at all — that gap is the disclosure list Flow E describes ("the
    board is not the extractor registry").
    """
    from creditpaper.harness.route import _SLOT_KEYWORDS

    out: list[str] = []
    for route_key, keywords in _SLOT_KEYWORDS:
        if any(taxonomy_key in route_keyword_targets(route_key, kw) for kw in keywords):
            out.append(route_key)
    return tuple(dict.fromkeys(out))


def keyword_table() -> dict[str, tuple[str, ...]]:
    """slot key -> every keyword that places a file in it."""
    table: dict[str, list[str]] = {}
    for route_key, keywords in _SLOT_KEYWORDS:
        for kw in keywords:
            for slot_key in route_keyword_targets(route_key, kw):
                table.setdefault(slot_key, []).append(kw)
    for slot_key, keywords in EXTRA_KEYWORDS.items():
        table.setdefault(slot_key, []).extend(keywords)
    return {k: tuple(dict.fromkeys(v)) for k, v in table.items()}


def normalise(text: str) -> str:
    low = text.lower().replace("\\", "/")
    low = re.sub(r"\.(pdf|docx?|xlsx?|csv|txt|png|jpe?g)$", "", low)
    low = low.replace("'", "").replace("’", "")
    low = re.sub(r"[_\-]+", " ", low)
    return re.sub(r"\s+", " ", low)


def _hit(keyword: str, haystacks: Iterable[str]) -> bool:
    raw = keyword.lower()
    if raw.startswith(("/", "\\")):
        # A folder-segment keyword ("/its/") matches only a path segment, as in route.py.
        needle = re.sub(r"[_\-]+", " ", raw.replace("\\", "/"))
        return any(needle in h for h in haystacks)
    kw = normalise(keyword.strip())
    if not kw:
        return False
    pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
    return any(pattern.search(h) for h in haystacks)


def refused_match(names: Iterable[str], refused: list[RefusedClass]) -> RefusedClass | None:
    haystacks = [normalise(n) for n in names if n]
    for cls in refused:
        if any(_hit(term, haystacks) for term in cls.match):
            return cls
    return None


@dataclass
class FileMatch:
    slot_key: str | None
    confidence: float | None
    reason: str | None
    candidates: list[tuple[str, float, str]] = field(default_factory=list)  # (slot_key, confidence, reason)


def _humanise_kind(kind: str) -> str:
    return {"its": "a term sheet", "qs": "a QS report", "da": "a development approval"}.get(
        kind, "a " + kind.replace("_", " ")
    )


CONTENT_ONLY_CEILING = 0.55


def _content_match(
    first_pages: dict[int, str],
    labelled: list[tuple[str, str]] | None,
    slot_by_key: dict[str, SlotType],
) -> tuple[str, float, str] | None:
    """The best slot the PAGE TEXT points at, when the filename points nowhere.

    Flow E defers *content-based proposal* and it stays deferred: the matcher
    still only proposes and the analyst still decides one file at a time. What
    changes is the evidence behind a proposal that the filename cannot make at
    all — `Scan_0042.pdf` carries no filename signal, and today it lands in
    Unassigned even when page one says Indicative Terms Sheet across the top.

    Capped below the filename floor of 0.6 so a content-only proposal always
    sorts above a filename one in the "ordered by doubt" list Flow E specifies.
    """
    from creditpaper.ingest.signals import fingerprint_signal, neighbour_signal, signature_signal

    body = " ".join(first_pages[n] for n in sorted(first_pages))
    signals = [
        fingerprint_signal(first_pages),
        signature_signal(first_pages),
        neighbour_signal(body, list(labelled or [])),
    ]
    usable = [s for s in signals if s.slot_key and s.slot_key in slot_by_key]
    if not usable:
        return None
    best = max(usable, key=lambda s: s.score)
    assert best.slot_key is not None
    return (
        best.slot_key,
        round(min(best.score, CONTENT_ONLY_CEILING), 2),
        f"No filename match. Read from page {min(first_pages)}: {best.detail}.",
    )


def match_file(
    names: Iterable[str],
    document_kind: str | None,
    slots: list[SlotType],
    table: dict[str, tuple[str, ...]] | None = None,
    *,
    first_pages: dict[int, str] | None = None,
    labelled: list[tuple[str, str]] | None = None,
) -> FileMatch:
    """The best slot for one file among `slots` (already resolved for the deal type).

    `first_pages` and `labelled` are optional evidence. With neither, the result
    is byte-for-byte what it was before they existed: the filename decides, and
    the page text only ever speaks where the filename is silent.
    """
    table = table if table is not None else keyword_table()
    haystacks = ["/" + normalise(n) for n in names if n]
    slot_by_key = {s.key: s for s in slots}
    order = {s.key: i for i, s in enumerate(slots)}

    hits: dict[str, list[str]] = {}
    for slot in slots:
        matched = [kw for kw in table.get(slot.key, ()) if _hit(kw, haystacks)]
        if matched:
            hits[slot.key] = sorted(matched, key=len, reverse=True)

    kind_slots = [k for k in KIND_TARGETS.get(document_kind or "", ()) if k in slot_by_key]

    # A keyword several matched slots share (a generic "insurance") says less than one
    # only this slot matched ("contract works"): rank on the slot's own wording first.
    seen: dict[str, int] = {}
    for matched in hits.values():
        for kw in matched:
            seen[kw] = seen.get(kw, 0) + 1
    own = {key: [kw for kw in matched if seen[kw] == 1] for key, matched in hits.items()}

    scored: list[tuple[str, float, str]] = []
    for key, matched in hits.items():
        top_raw = (own[key] or matched)[0]
        top = normalise(top_raw.strip("/\\ ")) or top_raw
        conf = 0.6
        if len(top) >= 12:
            conf += 0.1
        conf += 0.05 * min(len(matched) - 1, 2)
        reason = f'Matched on "{top}" in the filename.'
        if key in kind_slots:
            conf += 0.1
            reason += f" The document text also reads as {_humanise_kind(document_kind or '')}."
        if len(hits) > 1:
            conf -= 0.15
        scored.append((key, round(max(0.3, min(conf, 0.95)), 2), reason))

    if not scored and kind_slots:
        reason = f"No filename match. The document text reads as {_humanise_kind(document_kind or '')}."
        scored = [(k, 0.35, reason) for k in kind_slots]

    if not scored and first_pages:
        # Last resort only: the filename said nothing and the classifier's kind
        # placed nothing either. A confident filename match is never overridden.
        content = _content_match(first_pages, labelled, slot_by_key)
        if content is not None:
            scored = [content]

    if not scored:
        return FileMatch(slot_key=None, confidence=None, reason=None)

    def rank(item: tuple[str, float, str]) -> tuple[int, int, float, int]:
        key = item[0]
        mine = own.get(key) or []
        longest = len((mine or hits.get(key) or [""])[0])
        return (0 if mine else 1, -longest, -item[1], order[key])

    scored.sort(key=rank)
    best, rest = scored[0], scored[1:]
    if rest:
        others = ", ".join(slot_by_key[k].name for k, _, _ in rest)
        reason = f"{best[2]} Also matched {others}."
    else:
        reason = best[2]
    return FileMatch(slot_key=best[0], confidence=best[1], reason=reason, candidates=list(rest))


def canonical_score(filename: str) -> int:
    """route.py's canonical preference (executed/final over draft/copy)."""
    from pathlib import Path

    return _canonical_score(Path(filename))

"""Document routing — map a deal's document pile to slots and the right extractor.

A deal arrives as ~40 mixed files (not one blob). Routing classifies each to its
slot and picks the CANONICAL document per slot (the executed/final/updated copy,
not a draft/appendix/engagement letter), so only the materially-relevant documents
are deep-read (D7). Mappings are PROPOSED here; intake confirms them (D3: no
classifier silently in the critical path). New extractors register by slot_key.
"""

from __future__ import annotations

from pathlib import Path

from creditpaper.extractors.base import Extractor
from creditpaper.extractors.builder import BuilderExtractor
from creditpaper.extractors.building_contract import BuildingContractExtractor
from creditpaper.extractors.da import DaExtractor
from creditpaper.extractors.insurance import InsuranceExtractor
from creditpaper.extractors.its import ItsExtractor
from creditpaper.extractors.qs import QsExtractor
from creditpaper.extractors.screening import ScreeningExtractor
from creditpaper.extractors.sponsor import SponsorExtractor
from creditpaper.extractors.valuation import ValuationExtractor

# slot_key -> extractor. Add a slot by registering its extractor here.
EXTRACTOR_REGISTRY: dict[str, type[Extractor]] = {
    "its": ItsExtractor,
    "valuation": ValuationExtractor,
    "da": DaExtractor,
    "qs": QsExtractor,
    "building_contract": BuildingContractExtractor,
    "builder": BuilderExtractor,
    "insurance": InsuranceExtractor,
    "kyc": ScreeningExtractor,
    # Sponsor depth is split by DOCUMENT TYPE into three slots (DECISIONS.md 2026-06-18)
    # so routing reads the financials, the structure chart, AND the track record, not
    # one canonical doc. One shared extractor emits to the same sponsor.* model paths;
    # the apply layer merges. (Focused per-slot instructions are a GL-03 accuracy refinement.)
    "sponsor_financials": SponsorExtractor,
    "sponsor_structure": SponsorExtractor,
    "sponsor_track_record": SponsorExtractor,
}

# (slot, keywords) — first slot whose any keyword is in the path wins. Ordered so
# more specific slots are tried before generic ones.
_SLOT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # ITS naming varies by lender ("Executed ITS", "Term Sheet - Signed", "Letter of
    # Offer", "Facility Agreement") — match the facility-terms document generically,
    # not one deal's filename (the Wattlefield blind deal uses "Term Sheet", not "ITS").
    (
        "its",
        (
            "executed its",
            "indicative term",
            "/its/",
            "\\its\\",
            "term sheet",
            "terms sheet",
            "termsheet",
            "/terms sheets",
            "letter of offer",
            "facility agreement",
            "loan agreement",
        ),
    ),
    ("valuation", ("finalrpt", "final rpt", "valuation", "valuer", "val report", "val page")),
    (
        "qs",
        (
            "qs report",
            "quantity surveyor",
            "cost report",
            "qs eport",
            "cost to complete",
            "quantity surveying",
            "dd-ir",
            "initial report",
        ),
    ),
    (
        "building_contract",
        ("building contract", "construction contract", "hia ", "abic ", "contract of works", "head contract"),
    ),
    (
        "builder",
        (
            "builder's licence",
            "builders licence",
            "builder licence",
            "building licence",
            "builder's financ",
            "builder financ",
            "builder's statement",
            "ato builder",
        ),
    ),
    # Sponsor / borrower depth (Section 5), split by document TYPE into three slots so
    # the financials, the structure chart and the track record each route separately.
    # Placed AFTER 'builder' so the builder's own financials (matched above) don't fall
    # here. Screening OUTPUTS stay with 'kyc'; never raw identity documents (D7).
    (
        "sponsor_financials",
        (
            "financial statement",
            "financial statements",
            "annual accounts",
            "annual report",
            "statement of financial position",
            "statement of position",
            "assets and liabilities",
            "asset and liability",
            "net asset position",
            "balance sheet",
            "profit and loss",
            "accountant",
            "accountants letter",
            "borrower financ",
            "guarantor financ",
        ),
    ),
    (
        "sponsor_structure",
        ("group structure", "corporate structure", "ownership structure", "structure chart", "org chart"),
    ),
    (
        "sponsor_track_record",
        ("track record", "development schedule", "experience schedule", "completed projects"),
    ),
    (
        "insurance",
        (
            "certificate of currency",
            "insurance",
            "contract works",
            "public liability",
            "professional indemnity",
            "cert of currency",
        ),
    ),
    # KYC slot takes screening CONCLUSIONS only (D7) — match the screening/credit-check
    # OUTPUTS, never raw identity documents (trust deeds, constitutions, IDs, minutes).
    # Do NOT match the bare "kyc" folder name, which holds those raw documents.
    (
        "kyc",
        (
            "veda",
            "equifax",
            "credit check",
            "credit report",
            "aml screen",
            "sanctions screen",
            "screening report",
            "pep screen",
        ),
    ),
    (
        "da",
        (
            "development approval",
            "notice of determination",
            "da-",
            "consent",
            "endorsed plan",
            "endorsed development",
            "approved plans",
        ),
    ),
]

# Per-slot preference: higher score = more canonical. Negative terms demote a file
# (a draft, an appendix, a duplicate "copy", an engagement letter). "determination"
# promotes the operative Notice of Determination over the rest of a DA pack (plans,
# arborist, geotech) that also carries the DA number; the consultant reports demote
# so the DA slot lands on the consent, not a stamped plan set (D7).
_PREFER = ("updated", "executed", "final", "signed", "determination")
_DEMOTE = (
    "draft",
    "appendix",
    "appendices",
    "engagement",
    "copy",
    "superseded",
    "old",
    "arborist",
    "geotechnical",
    "geotech",
    "landscape",
    "stormwater",
    "architectural",
    "structural",
    "dilapidation",
)

# Slots where the SAME document type legitimately recurs N times, so the slot ingests
# every classified doc (apply merges by key — insurance[<cover>], the screening
# outputs) rather than picking one canonical doc. Distinct from finer slots, which
# split DIFFERENT document types (DECISIONS.md 2026-06-18). Extend by adding a slot
# (e.g. sponsor_financials / guarantor financials once the sponsor split lands).
#
# The SOURCE OF TRUTH is the `multi` flag on each taxonomy slot. This set
# mirrors it for the routing keys that have an extractor, because `route_deal`
# is a pure function of file paths — `scripts/ingest_coverage.py` and
# `harness_run` both call it with no database — and reading the taxonomy here
# would make routing require one. `tests/ingest/test_multi_doc_slots_match_
# taxonomy.py` fails if the two drift.
#
# `builder`, `building_contract` and `valuation` were missing until 2026-09-21,
# so a deal with two valuations, or a building contract plus its variation, had
# the extra documents dropped before any extractor saw them — no tray, no flag,
# nothing in the report.
MULTI_DOC_SLOTS: set[str] = {
    "builder",
    "building_contract",
    "insurance",
    "kyc",
    "sponsor_financials",
    "valuation",
}


def classify(path: str | Path) -> str | None:
    """The slot a single file belongs to, or None if it isn't a known slot."""
    low = str(path).lower()
    # Normalize underscores/hyphens to spaces so keyword matching works
    # when _safe_filename() converts spaces to underscores.
    low_normalized = low.replace("_", " ").replace("-", " ")
    for slot, keywords in _SLOT_KEYWORDS:
        if any(k in low or k in low_normalized for k in keywords):
            return slot
    return None


def _canonical_score(path: Path) -> int:
    low = path.name.lower()
    return sum(2 for k in _PREFER if k in low) - sum(3 for k in _DEMOTE if k in low)


def route_deal(files: list[str | Path]) -> dict[str, list[Path]]:
    """Proposed {slot: [docs]} for the slots we can extract. A single-doc slot gets the
    one canonical pick (best preference score; ties break on the longest, most-specific
    path); a MULTI_DOC_SLOT gets EVERY classified doc, best-first, so a slot fed by
    several documents (insurance certs by cover, VEDA + AML screening) ingests them all
    and the apply layer merges them by key. Always a list, so callers handle one shape."""
    buckets: dict[str, list[Path]] = {}
    for f in files:
        slot = classify(f)
        if slot in EXTRACTOR_REGISTRY:
            buckets.setdefault(slot, []).append(Path(f))
    out: dict[str, list[Path]] = {}
    for slot, paths in buckets.items():
        ranked = sorted(paths, key=lambda p: (_canonical_score(p), len(str(p))), reverse=True)
        out[slot] = ranked if slot in MULTI_DOC_SLOTS else [ranked[0]]
    return out

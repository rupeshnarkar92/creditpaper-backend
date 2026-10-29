"""Check registry (Build Plan §6). Named, finite, added by name (D6).

Each check: trigger (which deal types / slots), logic (a callable over the deal
model), message template. Deterministic where possible. Logic lands in Wave 2;
the registry of names is fixed now so nothing open-ended creeps in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from creditpaper.model.deal import DealModel, DealType


@dataclass(frozen=True)
class CheckSpec:
    id: int
    key: str
    name: str
    deal_types: tuple[DealType, ...] | None = None  # None = all
    deterministic: bool = True
    logic: Callable[[DealModel], CheckResult | None] | None = None  # Wave 2


@dataclass
class CheckResult:
    check_key: str
    passed: bool
    message: str
    anchored_section: int | None = None  # margin annotation target
    details: dict = field(default_factory=dict)


_CONSTRUCTION = (DealType.CONSTRUCTION,)


def same_concept_agreement(model: DealModel) -> CheckResult:
    """Check #18 (spec 3a, D6): no open contradiction on a material concept.

    Reads the deal's contradiction register (`ledger/contradictions.py`), where the
    ledger records every same-concept disagreement at commit. Resolved rows, noted
    (informational) rows and open contradictions on immaterial concepts pass; the
    check fails while any material contradiction is open, and names the concepts.
    """
    from creditpaper.ledger.contradictions import list_contradictions

    rows = list_contradictions(model.tenant_id, model.deal_id, status="open")
    blocking = sorted({r.concept_path for r in rows if r.blocking})
    if blocking:
        return CheckResult(
            "same_concept_agreement",
            False,
            f"{len(blocking)} material concept(s) have an open contradiction between sources",
            details={"concepts": blocking, "register_ids": sorted(r.id for r in rows if r.blocking)},
        )
    return CheckResult("same_concept_agreement", True, "No open contradiction on a material concept")


REGISTRY: list[CheckSpec] = [
    CheckSpec(1, "equity_reconciliation", "Claimed equity vs invoice totals", _CONSTRUCTION),
    CheckSpec(2, "funding_reconciliation", "Borrower funding table vs signed ITS, to the dollar"),
    CheckSpec(3, "qs_vs_feasibility", "QS contract sum and cost-to-complete vs feasibility", _CONSTRUCTION),
    CheckSpec(4, "qs_vs_developer", "QS report vs developer-claimed costs", _CONSTRUCTION),
    CheckSpec(5, "valuation_currency", "Valuation under three months at expected funding date"),
    CheckSpec(6, "relying_party", "Valuation assigned to the firm"),
    CheckSpec(7, "insurance_sufficiency", "Sums vs facility and value; SPV noted; dates cover term"),
    CheckSpec(8, "sop_arithmetic", "SOP internal addition; encumbrance classification consistent"),
    CheckSpec(9, "presale_coverage", "Qualifying presales vs covenant threshold", _CONSTRUCTION),
    CheckSpec(10, "ato_position", "No outstanding debts/arrangements/disputes, or flagged"),
    CheckSpec(11, "rates_currency_identity", "Clearances under three months; property and entity match"),
    CheckSpec(12, "entity_consistency", "Entities consistent across ITS, structure, financials, SOPs, title"),
    CheckSpec(13, "da_blockers", "Unsatisfied/lapsed DA conditions; plans match feasibility", _CONSTRUCTION),
    CheckSpec(
        14, "builder_capacity", "Licence current and category-matched; cash vs project scale", _CONSTRUCTION
    ),
    CheckSpec(15, "payout_match", "Existing mortgagee payout vs funding table"),
    CheckSpec(16, "its_to_paper", "Every paper term reconciles to signed ITS or flagged as moved"),
    # DECISIONS.md 2026-06-11: flagged facts are citable as disclosures; the
    # inverse is enforced here — a flag the paper never discloses blocks approval.
    CheckSpec(17, "flagged_disclosure", "Every flagged field cited as a disclosure in Section 7 or 8"),
    # DECISIONS.md 2026-09-18 ("Canonical concepts"): assertions of the *same* concept
    # agree, or their disagreement is resolved. Bounded by the concept registry (D6).
    CheckSpec(
        18,
        "same_concept_agreement",
        "Sources stating the same material concept agree, or the contradiction is resolved",
        logic=same_concept_agreement,
    ),
]

BY_KEY: dict[str, CheckSpec] = {c.key: c for c in REGISTRY}


def checks_for(deal_type: DealType) -> list[CheckSpec]:
    return [c for c in REGISTRY if c.deal_types is None or deal_type in c.deal_types]

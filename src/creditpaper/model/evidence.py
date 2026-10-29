"""Evidence-authority tier: how far a fact can be trusted by who asserted it (spec §3.8, schema 0.11).

A covenant read from the executed term sheet and the same figure in the
borrower's funding table are not the same evidence. The specialists weigh a
finding by it and the claim verifier treats a borrower's own number as a claim
to corroborate, not a fact to restate.

The tier is DERIVED, never asked: from how the fact was made (an analyst's
entry, a computation), else from the slot its document was filed in. A fact
whose document has no assigned slot gets no tier rather than a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from creditpaper.model.core import EvidenceTier, ExtractionMethod, Fact

__all__ = ["SLOT_TIERS", "annotate_tiers", "model_facts", "tier_for"]

_S = EvidenceTier.SIGNED_LEGAL
_I = EvidenceTier.INDEPENDENT
_B = EvidenceTier.BORROWER_ASSERTED
_A = EvidenceTier.ANALYST_CONFIRMED

# Every slot in docs/specs/slot-taxonomy/slot_taxonomy.json; a test fails if one is missing.
SLOT_TIERS: Mapping[str, EvidenceTier] = MappingProxyType(
    {
        # Executed, legal or issued-by-authority instruments.
        "signed_its": _S,
        "company_trust_documents": _S,
        "purchase_contract": _S,
        "leases": _S,
        "development_approval": _S,
        "authority_contributions": _S,
        "occupancy_certificate": _S,
        "presales": _S,
        "contracts_eois": _S,
        "building_contract": _S,
        "construction_certificate": _S,
        "tripartite": _S,
        "legal_review": _S,
        "loan_documentation": _S,
        "deed_of_priority": _S,
        "independent_legal_advice": _S,
        # A third party with no stake in the answer.
        "kyc_screening": _I,
        "ato_statements": _I,
        "land_tax_clearance": _I,
        "water_rates": _I,
        "council_rates": _I,
        "valuation": _I,
        "public_liability_insurance": _I,
        "contract_works_insurance": _I,
        "building_insurance": _I,
        "qs_report": _I,
        "da_submission_pack": _I,
        "deposits_in_trust": _I,
        "payout_figure": _I,
        "builder_licence": _I,
        "builder_ato": _I,
        # The borrower's (or its builder's) own statements and schedules.
        "corporate_structure": _B,
        "borrower_profile": _B,
        "funding_table": _B,
        "statement_of_position": _B,
        "borrower_financials": _B,
        "equity_evidence": _B,
        "sales_contract_template": _B,
        "price_list": _B,
        "marketing": _B,
        "consultant_profiles": _B,
        "builder_financials": _B,
        "builder_profile": _B,
        "site_personnel": _B,
        "schedule_of_finishes": _B,
        # Maintained by the lender's own team.
        "rfi_sheet": _A,
    }
)


def tier_for(*, slot_key: str | None, method: ExtractionMethod) -> EvidenceTier | None:
    """The tier for a fact made by `method` from a document filed in `slot_key`; None when unknown."""
    if method is ExtractionMethod.HUMAN_ENTRY:
        return EvidenceTier.ANALYST_CONFIRMED
    if method is ExtractionMethod.DERIVED:
        return EvidenceTier.COMPUTED
    if slot_key is None:
        return None
    return SLOT_TIERS.get(slot_key)


_STRENGTH = (
    EvidenceTier.SIGNED_LEGAL,
    EvidenceTier.INDEPENDENT,
    EvidenceTier.BORROWER_ASSERTED,
    EvidenceTier.ANALYST_CONFIRMED,
)


def _strongest(tiers: Iterable[EvidenceTier | None]) -> EvidenceTier | None:
    present = {t for t in tiers if t is not None}
    return next((t for t in _STRENGTH if t in present), None)


def model_facts(node: object) -> Iterator[Fact[Any]]:
    """Every `Fact` in a model, depth first, through nested blocks and keyed collections."""
    if isinstance(node, Fact):
        yield node
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from model_facts(getattr(node, name))
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from model_facts(value)


def annotate_tiers(model: BaseModel, slots_of: Mapping[str, Iterable[str]]) -> None:
    """Set each fact's tier, in place, from its document's current slots (`document_id -> slot keys`).

    A document filed in two slots takes the stronger tier; one in none takes no tier."""
    for fact in model_facts(model):
        prov = fact.provenance
        if prov is None:
            continue
        slots = list(slots_of.get(prov.document_id, ()))
        if prov.method in (ExtractionMethod.HUMAN_ENTRY, ExtractionMethod.DERIVED):
            prov.evidence_tier = tier_for(slot_key=None, method=prov.method)
        else:
            prov.evidence_tier = _strongest(tier_for(slot_key=s, method=prov.method) for s in slots)

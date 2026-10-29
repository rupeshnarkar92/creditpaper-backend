"""Materiality engine — computes the CRUX of a deal, deal-agnostically.

The objective: get to what DECIDES the credit, not everything. A great paper goes
deep only where materiality is high and is crisp everywhere else (selective depth =
concise AND insightful). This module ranks the material items from the model's
derived signals + facts + gaps, using GENERAL credit logic (proximity to binding
constraints, coverage near 1, unquantified recourse, missing-but-decisive facts) —
so it works for construction, bridge, or residual stock alike, never tuned to one
deal.

It is a NAMED-RULE REGISTRY (the D6 philosophy): new materiality dimensions are
added by name, not by an open-ended engine. Deterministic — the crux is computed,
not left for an LLM to stumble onto. Feeds the thesis (its dominant risks must
reflect the top items) and the thesis completeness guard.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from creditpaper.model.core import Fact
from creditpaper.model.deal import DealModel, deal_type_str

HIGH, MEDIUM, LOW = 75, 45, 25


class MaterialCategory(str, Enum):
    COVENANT = "covenant"
    SECURITY = "security"
    EXIT = "exit"
    EXECUTION = "execution"
    COUNTERPARTY = "counterparty"
    VALUATION = "valuation"
    COST = "cost"
    GAP = "gap"


@dataclass
class MaterialItem:
    key: str
    category: MaterialCategory
    label: str  # short, grounded in this deal
    score: int  # 0-100 materiality
    basis: list[str] = field(default_factory=list)  # model paths / signals
    keywords: set[str] = field(default_factory=set)  # for the thesis completeness check

    @property
    def tier(self) -> str:
        return "HIGH" if self.score >= HIGH else "MEDIUM" if self.score >= MEDIUM else "LOW"


_RULES: list[Callable[[DealModel], list[MaterialItem]]] = []


def materiality_rule(fn: Callable[[DealModel], list[MaterialItem]]):
    _RULES.append(fn)
    return fn


def _val(fact: Fact | None):
    return fact.value if fact is not None and fact.value is not None else None


# ---------------------------------------------------------------------------
# Rules (general credit logic; each returns 0+ items)
# ---------------------------------------------------------------------------


@materiality_rule
def _covenant_headroom(model: DealModel) -> list[MaterialItem]:
    items: list[MaterialItem] = []
    m = model.metrics
    drop = _val(m.lvr_breach_drop_pct)
    if drop is not None:
        d = Decimal(drop)
        score = HIGH + 15 if d < 1 else MEDIUM if d < 5 else LOW
        items.append(
            MaterialItem(
                "lvr_headroom",
                MaterialCategory.COVENANT,
                f"LVR covenant headroom — value can fall ~{d}% before breach",
                min(score, 95),
                ["metrics.lvr_breach_drop_pct"],
                {"lvr", "covenant", "headroom", "breach"},
            )
        )
    head, tc = _val(m.lcr_headroom_amount), _val(model.economics.total_cost)
    if head is not None and tc:
        pct = Decimal(head) / Decimal(tc) * 100
        score = HIGH + 10 if pct < 1 else MEDIUM if pct < 5 else LOW
        items.append(
            MaterialItem(
                "lcr_headroom",
                MaterialCategory.COVENANT,
                f"LCR covenant headroom — ~{pct:.1f}% of cost before breach",
                min(score, 90),
                ["metrics.lcr_headroom_amount", "economics.total_cost"],
                {"lcr", "covenant", "headroom", "cost"},
            )
        )
    return items


@materiality_rule
def _security_position(model: DealModel) -> list[MaterialItem]:
    """Day-one position against the standing security value — scored by LVR level,
    deal-agnostic (the standing basis is as-is land for construction, in-one-line
    value for residual stock; chosen in derive.py)."""
    lvr = _val(model.metrics.asis_lvr_pct)
    if lvr is None:
        return []
    lvr = Decimal(lvr)
    kw = {"security", "lvr", "as-is", "as is", "one-line", "standing", "day-one", "day one"}
    gap = _val(model.metrics.asis_security_gap)
    if gap is not None and Decimal(gap) > 0:
        return [
            MaterialItem(
                "security_overadvance",
                MaterialCategory.SECURITY,
                "Day-one advance exceeds standing security — over-advanced at close",
                HIGH + 5,
                ["metrics.asis_security_gap", "metrics.asis_lvr_pct"],
                kw | {"shortfall", "over-advanced", "advance"},
            )
        ]
    if lvr >= 80:
        return [
            MaterialItem(
                "standing_security",
                MaterialCategory.SECURITY,
                f"Thin standing-security headroom — day-one LVR ~{lvr}% of the standing value",
                HIGH,
                ["metrics.asis_lvr_pct"],
                kw,
            )
        ]
    if lvr >= 65:
        return [
            MaterialItem(
                "standing_security",
                MaterialCategory.SECURITY,
                f"Moderate standing-security headroom — day-one LVR ~{lvr}%",
                MEDIUM,
                ["metrics.asis_lvr_pct"],
                kw,
            )
        ]
    return []


@materiality_rule
def _exit_fragility(model: DealModel) -> list[MaterialItem]:
    if not _val(model.deal.exit_strategy):
        return []
    dt = deal_type_str(model)
    score = HIGH if not model.presales else MEDIUM
    if dt == "residual_stock":
        detail = "presales reduce absorption risk" if model.presales else "no presales — full absorption risk"
        label = f"Repayment by progressive stock sell-down — {detail}"
    else:
        detail = "uncontracted (no presales on file)" if not model.presales else "presales on file"
        label = f"Repayment rests on the exit — {detail}"
    return [
        MaterialItem(
            "exit",
            MaterialCategory.EXIT,
            label,
            score,
            ["deal.exit_strategy"],
            {"exit", "sale", "sell", "repay", "presale", "sell-down", "absorption"},
        )
    ]


@materiality_rule
def _construction_execution(model: DealModel) -> list[MaterialItem]:
    if deal_type_str(model) != "construction":
        return []
    items: list[MaterialItem] = []
    if _val(model.builder.financials.cash_position) is None:
        items.append(
            MaterialItem(
                "builder_capacity",
                MaterialCategory.EXECUTION,
                "Builder financial capacity not evidenced on file",
                HIGH,
                ["builder.financials.cash_position"],
                {"builder", "construction", "completion", "build"},
            )
        )
    if _val(model.building_contract.contract_type) is None:
        items.append(
            MaterialItem(
                "building_contract",
                MaterialCategory.EXECUTION,
                "No building-contract terms (fixed-price / sum) on file",
                MEDIUM + 10,
                ["building_contract.contract_type"],
                {"contract", "fixed-price", "builder"},
            )
        )
    return items


@materiality_rule
def _recourse(model: DealModel) -> list[MaterialItem]:
    if model.its.guarantors and not model.sop:
        return [
            MaterialItem(
                "recourse",
                MaterialCategory.COUNTERPARTY,
                "Guarantee recovery value unquantified (no statements of position)",
                MEDIUM,
                ["its.guarantors", "sop"],
                {"guarantee", "guarantor", "recourse", "statement of position"},
            )
        ]
    return []


@materiality_rule
def _cost_alignment(model: DealModel) -> list[MaterialItem]:
    qs = _val(model.qs.project_cost)
    line = model.economics.funding_table.get("construction")
    funded = _val(line.total) if line else None
    if qs is not None and funded is not None and Decimal(qs) > Decimal(funded):
        return [
            MaterialItem(
                "cost_alignment",
                MaterialCategory.COST,
                "QS cost exceeds the funded construction line",
                MEDIUM,
                ["qs.project_cost", "economics.funding_table[construction].total"],
                {"qs", "cost", "contingency", "overrun", "construction"},
            )
        ]
    return []


@materiality_rule
def _valuation_currency(model: DealModel) -> list[MaterialItem]:
    vd = _val(model.valuation.valuation_date)
    fd = _val(model.deal.expected_funding_date)
    if vd is not None and fd is not None and (fd - vd).days > 75:
        return [
            MaterialItem(
                "valuation_currency",
                MaterialCategory.VALUATION,
                f"Valuation aging toward its currency limit at funding (~{(fd - vd).days} days)",
                MEDIUM,
                ["valuation.valuation_date", "deal.expected_funding_date"],
                {"valuation", "currency", "current", "dated", "revaluation"},
            )
        ]
    return []


@materiality_rule
def _material_gaps(model: DealModel) -> list[MaterialItem]:
    # A gap is only material once there is a real deal to assess.
    if _val(model.economics.facility_amount) is None:
        return []
    items: list[MaterialItem] = []
    if not model.insurance:
        dt = deal_type_str(model)
        label = (
            "No insurance position on file (contract works / public liability — "
            "material on a construction facility)"
            if dt == "construction"
            else "No insurance position on file (building / strata cover — material on completed stock)"
        )
        items.append(
            MaterialItem(
                "insurance_gap",
                MaterialCategory.GAP,
                label,
                MEDIUM,
                ["insurance"],
                {"insurance", "cover", "contract works", "strata"},
            )
        )
    if _val(model.security.title_status) is None and not model.security.encumbrances:
        items.append(
            MaterialItem(
                "title_gap",
                MaterialCategory.GAP,
                "Title / security ranking not confirmed on file",
                MEDIUM,
                ["security.title_status"],
                {"title", "security", "ranking", "mortgage"},
            )
        )
    return items


def assess_materiality(model: DealModel) -> list[MaterialItem]:
    """All material items, ranked most-material first. The top few are the crux."""
    items: list[MaterialItem] = []
    for rule in _RULES:
        items.extend(rule(model))
    items.sort(key=lambda i: i.score, reverse=True)
    return items

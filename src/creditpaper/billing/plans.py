"""The published tiers and the usage arithmetic. Pure: no database, no Stripe.

Flow C (signed off 2026-08-26, prices in AUD per DECISIONS 2026-09-15): a subscription banded
on papers a month, unlimited users at every tier, annual saves 10%. A paper is never blocked at
the band; papers over it are overage at the tier's own effective rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

Tier = Literal["starter", "standard", "enterprise"]
Interval = Literal["monthly", "annual"]


@dataclass(frozen=True)
class Plan:
    tier: Tier
    papers_included: int | None  # None: unlimited (Enterprise)
    monthly: Decimal | None  # price a month when billed monthly
    annual: Decimal | None  # price a month when billed annually
    overage_rate: Decimal | None  # per paper over the band
    support: str


PLANS: dict[str, Plan] = {
    "starter": Plan("starter", 3, Decimal("499"), Decimal("449"), Decimal("166"), "Help centre"),
    "standard": Plan("standard", 8, Decimal("999"), Decimal("899"), Decimal("125"), "Email support"),
    "enterprise": Plan("enterprise", None, None, None, None, "Named account manager"),
}

CURRENCY = "AUD"


def price_per_month(tier: str, interval: str) -> Decimal | None:
    plan = PLANS[tier]
    return plan.annual if interval == "annual" else plan.monthly


def calendar_month(now: datetime) -> tuple[datetime, datetime]:
    """The billing period when no Stripe period is known yet: the calendar month, in UTC."""
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@dataclass(frozen=True)
class Usage:
    papers_approved: int
    papers_included: int | None
    overage_papers: int
    overage_rate: Decimal | None
    overage_amount: Decimal


def usage_for(tier: str, papers_approved: int) -> Usage:
    """Plan and overage kept apart, so an invoice reconciles against what the team remembers doing."""
    plan = PLANS[tier]
    if plan.papers_included is None or plan.overage_rate is None:
        return Usage(papers_approved, None, 0, None, Decimal("0"))
    over = max(0, papers_approved - plan.papers_included)
    return Usage(papers_approved, plan.papers_included, over, plan.overage_rate, plan.overage_rate * over)

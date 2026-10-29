"""Billing for one firm: what Settings shows, plan changes, card collection and Stripe events."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from creditpaper.billing import store, stripe_client
from creditpaper.billing.plans import CURRENCY, PLANS, calendar_month, price_per_month, usage_for

log = logging.getLogger(__name__)

# Stripe subscription status -> ours. Anything owed and unpaid is past due: generating and
# approving pause, reading and exporting do not (flow C, soft lock).
_STATUS = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "incomplete_expired": "canceled",
    "canceled": "canceled",
    "paused": "past_due",
}


class EnterpriseByArrangement(ValueError):
    """Enterprise is quoted and set up with the firm, not chosen in Settings."""


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _ts(epoch: int | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch else None


def billing_summary(tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    account = store.get_or_create_account(tenant_id)
    plan = PLANS[account["tier"]]
    start, end = account["current_period_start"], account["current_period_end"]
    if not (start and end):
        start, end = calendar_month(now or datetime.now(UTC))
    usage = usage_for(account["tier"], store.count_billable_events(tenant_id, start, end))
    card = (
        {
            "brand": account["card_brand"],
            "last4": account["card_last4"],
            "exp_month": account["card_exp_month"],
            "exp_year": account["card_exp_year"],
        }
        if account["card_last4"]
        else None
    )
    return {
        "plan": {
            "tier": account["tier"],
            "interval": account["billing_interval"],
            "status": account["status"],
            "price_per_month": _money(price_per_month(account["tier"], account["billing_interval"])),
            "papers_included": plan.papers_included,
            "renews_at": account["current_period_end"].isoformat() if account["current_period_end"] else None,
            "started_at": account["created_at"].isoformat() if account["created_at"] else None,
            "currency": CURRENCY,
        },
        "usage": {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "papers_approved": usage.papers_approved,
            "papers_included": usage.papers_included,
            "overage_papers": usage.overage_papers,
            "overage_rate": _money(usage.overage_rate),
            "overage_amount": float(usage.overage_amount),
        },
        "payment_method": card,
        "invoices": [
            {
                "id": inv["id"],
                "number": inv["number"],
                "issued_at": inv["issued_at"].isoformat(),
                "amount": float(inv["amount"]),
                "currency": inv["currency"],
                "status": inv["status"],
                "description": inv["description"],
                "pdf_url": inv["pdf_url"],
            }
            for inv in store.list_invoices(tenant_id)
        ],
        "stripe_connected": stripe_client.is_configured(),
    }


def change_plan(tenant_id: str, tier: str, interval: str) -> dict[str, Any]:
    """Change tier or billing interval. Stripe is changed first when a subscription exists, so
    the firm is never shown a plan it is not being billed for."""
    if tier == "enterprise":
        raise EnterpriseByArrangement("Enterprise plans are arranged with our team.")
    account = store.get_or_create_account(tenant_id)
    if account["stripe_subscription_id"]:
        stripe_client.change_subscription_price(
            account["stripe_subscription_id"], stripe_client.price_id(tier, interval)
        )
    store.update_account(tenant_id, tier=tier, billing_interval=interval)
    return billing_summary(tenant_id)


def card_session(tenant_id: str, email: str, company: str | None) -> dict[str, str]:
    """Open Stripe's page to add or replace the firm's card. Nothing is charged there."""
    account = store.get_or_create_account(tenant_id)
    customer_id = account["stripe_customer_id"]
    if not customer_id:
        customer_id = stripe_client.create_customer(email, tenant_id, company)["id"]
        store.update_account(tenant_id, stripe_customer_id=customer_id)
    frontend = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    session = stripe_client.create_setup_checkout(
        customer_id,
        tenant_id,
        success_url=f"{frontend}/settings/billing?card=updated",
        cancel_url=f"{frontend}/settings/billing",
    )
    return {"url": session["url"]}


def record_paper_approved(tenant_id: str, deal_id: str) -> bool:
    """Call when a paper first reaches an approved render. The only thing usage is counted from."""
    return store.record_billable_event(tenant_id, deal_id)


# ── Stripe events ─────────────────────────────────────────────────────────────


def _tenant_for(obj: dict[str, Any]) -> str | None:
    tenant = (obj.get("metadata") or {}).get("tenant_id")
    if tenant:
        return str(tenant)
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer:
        account = store.find_account_by_customer(str(customer))
        return account["tenant_id"] if account else None
    return None


def _invoice_status(invoice: dict[str, Any], event_type: str) -> str:
    if event_type == "invoice.payment_failed" or invoice.get("status") in ("uncollectible", "void"):
        return "failed"
    return "paid" if invoice.get("status") == "paid" else "open"


def handle_event(event: dict[str, Any]) -> str:
    """Apply one verified Stripe event. Returns what was done, for the log."""
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    tenant_id = _tenant_for(obj)
    if not tenant_id:
        return f"ignored {event_type}: no tenant"

    if event_type == "checkout.session.completed" and obj.get("mode") == "setup":
        setup_intent = stripe_client.retrieve(
            f"/setup_intents/{obj['setup_intent']}", expand=["payment_method"]
        )
        method = setup_intent.get("payment_method") or {}
        card = method.get("card") or {}
        if method.get("id") and obj.get("customer"):
            stripe_client.set_default_payment_method(str(obj["customer"]), method["id"])
        store.update_account(
            tenant_id,
            card_brand=(card.get("brand") or "").title() or None,
            card_last4=card.get("last4"),
            card_exp_month=card.get("exp_month"),
            card_exp_year=card.get("exp_year"),
        )
        return "card saved"

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        store.update_account(
            tenant_id,
            stripe_subscription_id=obj.get("id"),
            status=_STATUS.get(obj.get("status", ""), "past_due"),
            current_period_start=_ts(obj.get("current_period_start")),
            current_period_end=_ts(obj.get("current_period_end")),
        )
        return f"subscription {obj.get('status')}"

    if event_type in ("invoice.finalized", "invoice.paid", "invoice.payment_failed", "invoice.voided"):
        lines = ((obj.get("lines") or {}).get("data")) or []
        description = (
            next((line.get("description") for line in lines if line.get("description")), "") or "Subscription"
        )
        status = _invoice_status(obj, event_type)
        store.upsert_invoice(
            tenant_id,
            stripe_invoice_id=obj["id"],
            number=obj.get("number") or obj["id"],
            description=description,
            amount=Decimal(obj.get("amount_due") or obj.get("total") or 0) / 100,
            currency=(obj.get("currency") or "aud").upper(),
            status=status,
            pdf_url=obj.get("invoice_pdf"),
            issued_at=_ts(obj.get("created")) or datetime.now(UTC),
        )
        if event_type == "invoice.payment_failed":
            store.update_account(tenant_id, status="past_due")
        elif event_type == "invoice.paid":
            store.update_account(tenant_id, status="active")
        return f"invoice {status}"

    return f"ignored {event_type}"

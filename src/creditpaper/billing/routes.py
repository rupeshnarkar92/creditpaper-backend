"""Billing API under /api/billing.

Admin only, and the tenant is the signed-in admin's own firm (never a header or parameter).
The webhook is the one unauthenticated route: it is authenticated by Stripe's signature.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from creditpaper.auth.database import get_db
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.auth.models.domain import Domain
from creditpaper.billing import service, stripe_client

router = APIRouter(prefix="/billing", tags=["billing"])
log = logging.getLogger(__name__)


class PlanChangeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    tier: Literal["starter", "standard", "enterprise"]
    interval: Literal["monthly", "annual"]


def _firm(user: CurrentUser) -> str:
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="Your account is not part of a firm.")
    return user.tenant_id


@router.get("")
async def get_billing(
    user: CurrentUser = Depends(require_permission("billing.read")),  # noqa: B008
):
    return await run_in_threadpool(service.billing_summary, _firm(user))


@router.post("/plan")
async def change_plan(
    req: PlanChangeRequest,
    user: CurrentUser = Depends(require_permission("billing.manage")),  # noqa: B008
):
    try:
        return await run_in_threadpool(service.change_plan, _firm(user), req.tier, req.interval)
    except service.EnterpriseByArrangement as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except stripe_client.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except stripe_client.StripeError as exc:
        log.warning("plan change failed at Stripe: %s", exc)
        raise HTTPException(
            status_code=502, detail="The plan could not be changed at Stripe. Nothing was changed."
        ) from None


@router.post("/payment-method/session")
async def payment_method_session(
    user: CurrentUser = Depends(require_permission("billing.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    tenant_id = _firm(user)
    company = (await db.execute(select(Domain.company).where(Domain.id == tenant_id))).scalar_one_or_none()
    try:
        return await run_in_threadpool(service.card_session, tenant_id, user.email, company)
    except stripe_client.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except stripe_client.StripeError as exc:
        log.warning("card session failed at Stripe: %s", exc)
        raise HTTPException(
            status_code=502, detail="Stripe could not open the card form. Try again."
        ) from None


@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        event = stripe_client.verify_webhook(
            payload, request.headers.get("stripe-signature"), os.getenv("STRIPE_WEBHOOK_SECRET", "")
        )
    except stripe_client.InvalidSignature as exc:
        log.warning("rejected Stripe webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid signature.") from None
    try:
        outcome = await run_in_threadpool(service.handle_event, event)
    except stripe_client.StripeError as exc:
        # Stripe retries non-2xx responses, which is what a transient failure needs.
        log.error("Stripe webhook %s failed: %s", event.get("type"), exc)
        raise HTTPException(status_code=502, detail="Could not process the event.") from None
    log.info("Stripe webhook %s: %s", event.get("type"), outcome)
    return {"received": True}

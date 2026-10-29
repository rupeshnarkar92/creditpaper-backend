"""A minimal Stripe client over its REST API, plus webhook signature verification.

Uses httpx (already a dependency) rather than the Stripe SDK. Card details never pass through
Creditpaper: a card is collected on Stripe's own page, and only brand and last four come back.
Billing is off until STRIPE_SECRET_KEY is set; every call then raises BillingNotConfigured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

import httpx

API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_SECONDS = 300


class BillingNotConfigured(RuntimeError):
    """Stripe keys are not set in this environment."""


class StripeError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InvalidSignature(ValueError):
    """The webhook did not come from Stripe, or was replayed outside the tolerance."""


def _secret_key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise BillingNotConfigured("Card payments are not connected in this environment.")
    return key


def is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip())


def price_id(tier: str, interval: str) -> str:
    """Stripe price IDs come from the environment, e.g. STRIPE_PRICE_STANDARD_ANNUAL."""
    name = f"STRIPE_PRICE_{tier.upper()}_{interval.upper()}"
    value = os.getenv(name, "").strip()
    if not value:
        raise BillingNotConfigured(f"No Stripe price is set for {tier} billed {interval} ({name}).")
    return value


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested dicts as a[b], lists as a[0]."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif value is not None:
            out.append((name, "true" if value is True else "false" if value is False else str(value)))
    return out


def _request(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    key = _secret_key()
    try:
        response = httpx.request(
            method,
            f"{API}{path}",
            auth=(key, ""),
            data=_flatten(data) if data and method != "GET" else None,
            params=_flatten(data) if data and method == "GET" else None,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise StripeError(f"Could not reach Stripe: {exc.__class__.__name__}") from exc
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("error") or {}).get("message") or f"Stripe returned {response.status_code}"
        raise StripeError(message, response.status_code)
    return body


def create_customer(email: str, tenant_id: str, name: str | None = None) -> dict[str, Any]:
    return _request(
        "POST", "/customers", {"email": email, "name": name, "metadata": {"tenant_id": tenant_id}}
    )


def create_setup_checkout(
    customer_id: str, tenant_id: str, success_url: str, cancel_url: str
) -> dict[str, Any]:
    """A Stripe-hosted page that saves a card for later charges and takes no payment now."""
    return _request(
        "POST",
        "/checkout/sessions",
        {
            "mode": "setup",
            "currency": "aud",
            "customer": customer_id,
            "payment_method_types": ["card"],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"tenant_id": tenant_id},
        },
    )


def retrieve(path: str, expand: list[str] | None = None) -> dict[str, Any]:
    return _request("GET", path, {"expand": expand} if expand else None)


def set_default_payment_method(customer_id: str, payment_method_id: str) -> dict[str, Any]:
    return _request(
        "POST",
        f"/customers/{customer_id}",
        {"invoice_settings": {"default_payment_method": payment_method_id}},
    )


def change_subscription_price(subscription_id: str, new_price_id: str) -> dict[str, Any]:
    """Move the subscription's single item to a new price, prorated from today."""
    subscription = retrieve(f"/subscriptions/{subscription_id}")
    items = (subscription.get("items") or {}).get("data") or []
    if not items:
        raise StripeError("The subscription has no items to change.")
    return _request(
        "POST",
        f"/subscriptions/{subscription_id}",
        {
            "items": [{"id": items[0]["id"], "price": new_price_id}],
            "proration_behavior": "create_prorations",
        },
    )


def verify_webhook(
    payload: bytes, signature_header: str | None, secret: str, now: float | None = None
) -> dict[str, Any]:
    """Check a webhook's Stripe-Signature header and return the parsed event.

    Signed content is "{timestamp}.{raw body}" with HMAC-SHA256 under the endpoint secret. Any
    v1 signature may match (Stripe sends more than one while a secret is being rolled). An old
    timestamp is refused so a captured request cannot be replayed later.
    """
    if not secret:
        raise InvalidSignature("No webhook secret is configured.")
    if not signature_header:
        raise InvalidSignature("Missing Stripe-Signature header.")
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise InvalidSignature("Malformed Stripe-Signature header.") from None
    if abs((now if now is not None else time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise InvalidSignature("Webhook timestamp is outside the tolerance.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise InvalidSignature("Webhook signature does not match.")
    try:
        return json.loads(payload)
    except ValueError:
        raise InvalidSignature("Webhook body is not JSON.") from None

"""Thin wrapper around the Stripe SDK.

We intentionally keep this small — just the operations we need for the
subscription routes and webhook handler. Stripe calls are synchronous so we
offload them to a thread when called from async code (Stripe SDK does not
have an official async client).
"""
from __future__ import annotations

from typing import Any

import stripe
from loguru import logger

from app.core.config import settings

# Initialize once at import time.
if settings.STRIPE_SECRET_KEY:
    stripe.api_key = settings.STRIPE_SECRET_KEY


def stripe_configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def create_customer(email: str, name: str | None = None, user_id: str | None = None) -> str:
    """Create a Stripe Customer and return its id."""
    metadata = {"user_id": user_id} if user_id else None
    customer = stripe.Customer.create(email=email, name=name, metadata=metadata)
    logger.info(f"Stripe customer created: {customer.id} for {email}")
    return customer.id


def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
    trial_days: int | None = None,
) -> str:
    """Return a hosted Checkout URL for the user to complete payment."""
    params: dict[str, Any] = {
        "customer": customer_id,
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
    }
    if trial_days and trial_days > 0:
        params["subscription_data"] = {"trial_period_days": trial_days}

    session = stripe.checkout.Session.create(**params)
    return session.url


def create_portal_session(*, customer_id: str, return_url: str) -> str:
    """Return a Customer Portal URL for self-service subscription management."""
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return session.url


def parse_webhook_event(payload: bytes, signature: str) -> dict:
    """Validate the Stripe signature and return the event dict.

    Raises stripe.error.SignatureVerificationError on bad signature.
    """
    return stripe.Webhook.construct_event(
        payload, signature, settings.STRIPE_WEBHOOK_SECRET
    )

"""Stripe implementation of the PaymentProvider interface.

This used to live in `stripe_service.py` as bare functions; we keep it here
as a class so the route handlers can be provider-agnostic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import stripe
from loguru import logger

from app.core.config import settings
from app.db.models.subscription import SubscriptionPlan

from .base import (
    CheckoutResult,
    NormalizedEvent,
    NormalizedInvoice,
    NormalizedSubscription,
    PaymentProvider,
    PortalResult,
    ProviderError,
)


# Configure the SDK once.
if settings.STRIPE_SECRET_KEY:
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _to_utc(ts: int | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class StripeProvider(PaymentProvider):
    name = "stripe"

    # -- configuration ------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(settings.STRIPE_SECRET_KEY)

    def price_id_for(self, plan: SubscriptionPlan) -> str | None:
        if plan == SubscriptionPlan.PRO:
            return settings.STRIPE_PRICE_PRO or None
        if plan == SubscriptionPlan.ENTERPRISE:
            return settings.STRIPE_PRICE_ENTERPRISE or None
        return None

    # -- checkout / portal --------------------------------------------------

    async def create_checkout(
        self,
        *,
        user_id: str,
        user_email: str,
        user_name: str | None,
        plan: SubscriptionPlan,
        success_url: str,
        cancel_url: str,
        trial_days: int | None = None,
        existing_customer_id: str | None = None,
    ) -> CheckoutResult:
        if not self.is_configured():
            raise ProviderError("Stripe is not configured on this server")
        price_id = self.price_id_for(plan)
        if not price_id:
            raise ProviderError(f"No Stripe price configured for plan: {plan.value}")

        # Stripe SDK is sync — offload to a thread to avoid blocking the loop.
        def _do() -> CheckoutResult:
            customer_id = existing_customer_id
            if not customer_id:
                customer = stripe.Customer.create(
                    email=user_email,
                    name=user_name,
                    metadata={"user_id": user_id},
                )
                customer_id = customer.id

            params: dict[str, Any] = {
                "customer": customer_id,
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "allow_promotion_codes": True,
                # Mirror metadata onto the subscription too — webhooks for
                # invoice.* events don't always include the customer metadata
                # but DO include the subscription metadata.
                "subscription_data": {"metadata": {"user_id": user_id}},
            }
            if trial_days and trial_days > 0:
                params["subscription_data"]["trial_period_days"] = trial_days

            try:
                session = stripe.checkout.Session.create(**params)
            except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
                logger.exception("Stripe checkout creation failed")
                raise ProviderError(
                    f"Stripe error: {getattr(exc, 'user_message', None) or str(exc)}"
                ) from exc

            return CheckoutResult(
                checkout_url=session.url,
                provider=self.name,
                customer_id=customer_id,
            )

        return await asyncio.to_thread(_do)

    async def open_portal(self, *, customer_id: str, return_url: str) -> PortalResult:
        if not self.is_configured():
            raise ProviderError("Stripe is not configured on this server")

        def _do() -> PortalResult:
            try:
                session = stripe.billing_portal.Session.create(
                    customer=customer_id, return_url=return_url
                )
            except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
                raise ProviderError(
                    f"Stripe error: {getattr(exc, 'user_message', None) or str(exc)}"
                ) from exc
            return PortalResult(portal_url=session.url)

        return await asyncio.to_thread(_do)

    # -- webhook ------------------------------------------------------------

    def verify_and_parse_webhook(
        self, *, body: bytes, headers: dict[str, str]
    ) -> NormalizedEvent:
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise ProviderError("Stripe webhook secret not configured")
        signature = headers.get("stripe-signature") or headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(
                body, signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except (ValueError, stripe.error.SignatureVerificationError) as exc:  # type: ignore[attr-defined]
            raise ProviderError(f"Bad Stripe signature: {exc}") from exc

        event_type: str = event["type"]
        event_id: str = event.get("id", "")
        data: dict = event["data"]["object"]
        logger.info(f"Stripe webhook: {event_type} (id={event_id})")

        # Map to NormalizedEvent. checkout.session.completed is an alias for
        # subscription.created on our side — both mean "user just paid".
        if event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "checkout.session.completed",
        }:
            sub_data = data
            if event_type == "checkout.session.completed":
                sub_id = data.get("subscription")
                if not sub_id:
                    return NormalizedEvent(kind="unknown", event_id=event_id, raw=event)
                try:
                    sub_data = dict(stripe.Subscription.retrieve(sub_id))
                except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
                    raise ProviderError(f"Could not retrieve subscription: {exc}") from exc
            return NormalizedEvent(
                kind=(
                    "subscription.created"
                    if event_type == "customer.subscription.created"
                    else "subscription.updated"
                ),
                event_id=event_id,
                subscription=_normalize_stripe_subscription(sub_data),
                raw=event,
            )

        if event_type == "customer.subscription.deleted":
            return NormalizedEvent(
                kind="subscription.canceled",
                event_id=event_id,
                subscription=_normalize_stripe_subscription(data),
                raw=event,
            )

        if event_type in {"invoice.paid", "invoice.payment_succeeded"}:
            return NormalizedEvent(
                kind="invoice.paid",
                event_id=event_id,
                invoice=_normalize_stripe_invoice(data, status="paid"),
                raw=event,
            )
        if event_type == "invoice.payment_failed":
            return NormalizedEvent(
                kind="invoice.failed",
                event_id=event_id,
                invoice=_normalize_stripe_invoice(data, status="failed"),
                raw=event,
            )

        return NormalizedEvent(kind="unknown", event_id=event_id, raw=event)


def _normalize_stripe_subscription(data: dict) -> NormalizedSubscription:
    """Translate a raw Stripe subscription dict → our NormalizedSubscription."""
    # Plan inference from the price ID. We trust env mappings.
    plan: SubscriptionPlan | None = None
    items = (data.get("items") or {}).get("data") or []
    if items:
        price_id = (items[0].get("price") or {}).get("id")
        if price_id == settings.STRIPE_PRICE_PRO:
            plan = SubscriptionPlan.PRO
        elif price_id == settings.STRIPE_PRICE_ENTERPRISE:
            plan = SubscriptionPlan.ENTERPRISE

    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")

    return NormalizedSubscription(
        provider="stripe",
        provider_subscription_id=data.get("id", ""),
        provider_customer_id=data.get("customer"),
        plan=plan,
        status=data.get("status", ""),
        current_period_start=_to_utc(data.get("current_period_start")),
        current_period_end=_to_utc(data.get("current_period_end")),
        trial_ends_at=_to_utc(data.get("trial_end")),
        cancel_at_period_end=bool(data.get("cancel_at_period_end")),
        user_id=user_id,
    )


def _normalize_stripe_invoice(data: dict, *, status: str) -> NormalizedInvoice:
    # Stripe amounts are integer minor units (cents).
    amount_minor = data.get("amount_paid") if status == "paid" else data.get("amount_due")
    amount_minor = amount_minor or 0
    currency = (data.get("currency") or "usd").upper()
    amount = Decimal(amount_minor) / Decimal(100)

    paid_at = _to_utc(data.get("status_transitions", {}).get("paid_at")) if status == "paid" else None
    lines = (data.get("lines") or {}).get("data") or []
    description = lines[0].get("description") if lines else (data.get("description") or None)

    # Try to surface a user_id — Stripe invoices include the subscription
    # metadata via subscription_details on newer API versions, or we can pull
    # from customer metadata.
    user_id = ((data.get("subscription_details") or {}).get("metadata") or {}).get("user_id")

    return NormalizedInvoice(
        provider="stripe",
        provider_invoice_id=data.get("id", ""),
        amount=amount,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        description=description,
        hosted_url=data.get("hosted_invoice_url"),
        pdf_url=data.get("invoice_pdf"),
        paid_at=paid_at,
        metadata={
            "subscription_id": data.get("subscription"),
            "customer_id": data.get("customer"),
        },
        user_id=user_id,
    )

"""Paystack implementation.

Paystack's recurring model:
  1. Create a Plan in dashboard (or via API). Plan codes look like PLN_xxx.
  2. Initialize a Transaction with `plan=PLN_xxx` and `customer email` — the
     hosted page handles auth + first charge + sub setup.
  3. Subsequent renewals are auto-charged via Subauth, and Paystack fires
     `subscription.create` and `invoice.create`/`charge.success` webhooks.

Webhooks are verified by HMAC-SHA512 of the raw body using the secret key.

This is a functional MVP. Customer-portal-style self-service isn't a thing
Paystack hosts; we instead expose a `manage` URL that points to the
provider's account page.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
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


PAYSTACK_BASE = "https://api.paystack.co"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Paystack returns ISO 8601 — datetime.fromisoformat handles Z on 3.11+
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class PaystackProvider(PaymentProvider):
    name = "paystack"

    def is_configured(self) -> bool:
        return bool(settings.PAYSTACK_SECRET_KEY)

    def price_id_for(self, plan: SubscriptionPlan) -> str | None:
        if plan == SubscriptionPlan.PRO:
            return settings.PAYSTACK_PLAN_PRO or None
        if plan == SubscriptionPlan.ENTERPRISE:
            return settings.PAYSTACK_PLAN_ENTERPRISE or None
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
            raise ProviderError("Paystack is not configured on this server")
        plan_code = self.price_id_for(plan)
        if not plan_code:
            raise ProviderError(f"No Paystack plan configured for: {plan.value}")

        # Note: Paystack doesn't support per-checkout trial_days. Trials are
        # configured at the plan level. Documented for clarity.
        if trial_days:
            logger.info(
                "paystack: per-checkout trial_days isn't supported — "
                "configure the trial on the Plan in the dashboard"
            )

        # Initialize a hosted transaction. `metadata.user_id` rides into all
        # subsequent webhook events so we can resolve our local user later.
        payload = {
            "email": user_email,
            "plan": plan_code,
            "callback_url": success_url,
            "metadata": {
                "user_id": user_id,
                "cancel_url": cancel_url,
                "user_name": user_name,
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.post(
                    f"{PAYSTACK_BASE}/transaction/initialize",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"Paystack network error: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(f"Paystack error {r.status_code}: {r.text[:300]}")
        body = r.json()
        if not body.get("status"):
            raise ProviderError(f"Paystack error: {body.get('message')}")
        data = body.get("data") or {}
        return CheckoutResult(
            checkout_url=data.get("authorization_url", ""),
            provider=self.name,
            # The Paystack customer object is created on the FIRST charge, so
            # we don't have an id here. The webhook will give us one and we
            # backfill on Subscription then.
            customer_id=None,
        )

    async def open_portal(self, *, customer_id: str, return_url: str) -> PortalResult:
        # Paystack doesn't host a customer portal. Best we can do is point
        # them at their dashboard. Routes should handle ProviderError and
        # render a friendly message.
        raise ProviderError(
            "Paystack does not provide a hosted customer portal. "
            "Users manage payment methods directly through their bank."
        )

    # -- webhook ------------------------------------------------------------

    def verify_and_parse_webhook(
        self, *, body: bytes, headers: dict[str, str]
    ) -> NormalizedEvent:
        if not settings.PAYSTACK_SECRET_KEY:
            raise ProviderError("Paystack secret not configured")
        signature = headers.get("x-paystack-signature") or headers.get(
            "X-Paystack-Signature", ""
        )
        digest = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
            body,
            hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(digest, signature):
            raise ProviderError("Bad Paystack signature")

        try:
            event = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Paystack webhook decode failed: {exc}") from exc

        event_type: str = event.get("event", "")
        data: dict[str, Any] = event.get("data") or {}
        # Paystack uses idempotency_key/id on the data object; fall back to a
        # composite key so re-runs don't double-process.
        event_id = data.get("id") or data.get("reference") or ""
        event_id = f"paystack:{event_type}:{event_id}"
        logger.info(f"Paystack webhook: {event_type} (id={event_id})")

        if event_type == "subscription.create":
            return NormalizedEvent(
                kind="subscription.created",
                event_id=event_id,
                subscription=_normalize_paystack_sub(data),
                raw=event,
            )
        if event_type == "subscription.not_renew" or event_type == "subscription.disable":
            return NormalizedEvent(
                kind="subscription.canceled",
                event_id=event_id,
                subscription=_normalize_paystack_sub(data),
                raw=event,
            )
        if event_type in {"charge.success", "invoice.payment_failed", "invoice.create"}:
            status = "paid" if event_type == "charge.success" else (
                "failed" if event_type == "invoice.payment_failed" else "pending"
            )
            return NormalizedEvent(
                kind="invoice.paid" if status == "paid" else "invoice.failed",
                event_id=event_id,
                invoice=_normalize_paystack_invoice(data, status=status),
                raw=event,
            )
        return NormalizedEvent(kind="unknown", event_id=event_id, raw=event)


def _normalize_paystack_sub(data: dict) -> NormalizedSubscription:
    plan_obj = data.get("plan") or {}
    plan_code = plan_obj.get("plan_code") or plan_obj.get("code")
    plan: SubscriptionPlan | None = None
    if plan_code == settings.PAYSTACK_PLAN_PRO:
        plan = SubscriptionPlan.PRO
    elif plan_code == settings.PAYSTACK_PLAN_ENTERPRISE:
        plan = SubscriptionPlan.ENTERPRISE

    customer = data.get("customer") or {}
    user_metadata = (data.get("metadata") or {})
    if isinstance(user_metadata, str):
        try:
            user_metadata = json.loads(user_metadata)
        except Exception:
            user_metadata = {}

    return NormalizedSubscription(
        provider="paystack",
        provider_subscription_id=data.get("subscription_code") or data.get("id", ""),
        provider_customer_id=customer.get("customer_code"),
        plan=plan,
        status=data.get("status", "active"),
        current_period_start=_parse_iso(data.get("createdAt")),
        current_period_end=_parse_iso(data.get("next_payment_date")),
        trial_ends_at=None,  # Paystack trials only configurable at plan level
        cancel_at_period_end=False,
        user_id=user_metadata.get("user_id") if isinstance(user_metadata, dict) else None,
    )


def _normalize_paystack_invoice(data: dict, *, status: str) -> NormalizedInvoice:
    # Paystack amounts in kobo (NGN minor units, 100 = 1 NGN). For USD plans
    # they're cents.
    amount_minor = data.get("amount") or 0
    currency = (data.get("currency") or "NGN").upper()
    amount = Decimal(amount_minor) / Decimal(100)

    user_metadata = data.get("metadata") or {}
    if isinstance(user_metadata, str):
        try:
            user_metadata = json.loads(user_metadata)
        except Exception:
            user_metadata = {}

    return NormalizedInvoice(
        provider="paystack",
        provider_invoice_id=str(data.get("id") or data.get("reference") or ""),
        amount=amount,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        description=(data.get("plan") or {}).get("name"),
        hosted_url=None,
        pdf_url=None,
        paid_at=_parse_iso(data.get("paid_at")) if status == "paid" else None,
        metadata={"reference": data.get("reference")},
        user_id=user_metadata.get("user_id") if isinstance(user_metadata, dict) else None,
    )

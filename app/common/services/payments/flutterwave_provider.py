"""Flutterwave implementation.

Flutterwave's v3 API:
  - Payment Plans (https://developer.flutterwave.com/reference/create-payment-plan)
    have numeric IDs. We configure them via env (FLUTTERWAVE_PLAN_PRO etc.).
  - For recurring billing, initialize a Payment with `payment_plan=<id>` and
    a `tx_ref` we generate. The hosted checkout handles auth + first charge.
  - Webhooks ship the `verif-hash` header that must match
    `FLUTTERWAVE_WEBHOOK_HASH` (the value you set in the dashboard).
"""
from __future__ import annotations

import json
import uuid
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


FLW_BASE = "https://api.flutterwave.com/v3"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class FlutterwaveProvider(PaymentProvider):
    name = "flutterwave"

    def is_configured(self) -> bool:
        return bool(settings.FLUTTERWAVE_SECRET_KEY)

    def price_id_for(self, plan: SubscriptionPlan) -> str | None:
        if plan == SubscriptionPlan.PRO:
            return settings.FLUTTERWAVE_PLAN_PRO or None
        if plan == SubscriptionPlan.ENTERPRISE:
            return settings.FLUTTERWAVE_PLAN_ENTERPRISE or None
        return None

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
            raise ProviderError("Flutterwave is not configured on this server")
        plan_id = self.price_id_for(plan)
        if not plan_id:
            raise ProviderError(f"No Flutterwave plan configured for: {plan.value}")
        if trial_days:
            logger.info(
                "flutterwave: trials are managed at the Plan level — "
                "ignoring per-checkout trial_days"
            )

        # Flutterwave wants a tx_ref WE generate — used to match webhooks.
        tx_ref = f"affixai_{user_id}_{uuid.uuid4().hex[:12]}"
        # Amount must be sent — pulled from the Plan via API. Charging the
        # Plan amount is implicit on the hosted page, but the standard
        # endpoint we use here (`/payments`) requires an amount field;
        # use a tiny placeholder and let the plan override.
        payload = {
            "tx_ref": tx_ref,
            "amount": "0",  # plan amount takes precedence
            "currency": settings.BILLING_CURRENCY or "USD",
            "redirect_url": success_url,
            "payment_plan": plan_id,
            "customer": {
                "email": user_email,
                "name": user_name or user_email,
            },
            "meta": {
                "user_id": user_id,
                "cancel_url": cancel_url,
            },
            "customizations": {
                "title": "AffixAI Subscription",
                "description": f"Upgrade to {plan.value}",
            },
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.post(
                    f"{FLW_BASE}/payments",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"Flutterwave network error: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(f"Flutterwave error {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("status") != "success":
            raise ProviderError(f"Flutterwave error: {body.get('message')}")
        data = body.get("data") or {}
        return CheckoutResult(
            checkout_url=data.get("link", ""),
            provider=self.name,
            customer_id=None,
        )

    async def open_portal(self, *, customer_id: str, return_url: str) -> PortalResult:
        raise ProviderError(
            "Flutterwave does not provide a hosted customer portal."
        )

    def verify_and_parse_webhook(
        self, *, body: bytes, headers: dict[str, str]
    ) -> NormalizedEvent:
        if not settings.FLUTTERWAVE_WEBHOOK_HASH:
            raise ProviderError("Flutterwave webhook hash not configured")
        sent = headers.get("verif-hash") or headers.get("Verif-Hash", "")
        if sent != settings.FLUTTERWAVE_WEBHOOK_HASH:
            raise ProviderError("Bad Flutterwave verif-hash")

        try:
            event = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Flutterwave webhook decode failed: {exc}") from exc

        event_type: str = event.get("event") or event.get("event.type") or ""
        data: dict[str, Any] = event.get("data") or event
        event_id = f"flw:{event_type}:{data.get('id') or data.get('tx_ref') or ''}"
        logger.info(f"Flutterwave webhook: {event_type} (id={event_id})")

        meta = data.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        user_id = meta.get("user_id") if isinstance(meta, dict) else None

        if event_type in {"charge.completed", "Charge Completed"}:
            status = "paid" if data.get("status") == "successful" else "failed"
            return NormalizedEvent(
                kind="invoice.paid" if status == "paid" else "invoice.failed",
                event_id=event_id,
                invoice=_normalize_flw_invoice(data, status=status, user_id=user_id),
                raw=event,
            )
        if event_type in {"subscription.cancelled", "Subscription Cancelled"}:
            return NormalizedEvent(
                kind="subscription.canceled",
                event_id=event_id,
                subscription=_normalize_flw_sub(data, user_id=user_id),
                raw=event,
            )
        return NormalizedEvent(kind="unknown", event_id=event_id, raw=event)


def _normalize_flw_invoice(data: dict, *, status: str, user_id: str | None) -> NormalizedInvoice:
    amount = Decimal(str(data.get("amount") or 0))
    currency = (data.get("currency") or "USD").upper()
    return NormalizedInvoice(
        provider="flutterwave",
        provider_invoice_id=str(data.get("id") or data.get("tx_ref") or ""),
        amount=amount,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        description=(data.get("payment_plan") or {}).get("name")
            if isinstance(data.get("payment_plan"), dict) else None,
        hosted_url=None,
        pdf_url=None,
        paid_at=_parse_iso(data.get("created_at")) if status == "paid" else None,
        metadata={"tx_ref": data.get("tx_ref")},
        user_id=user_id,
    )


def _normalize_flw_sub(data: dict, *, user_id: str | None) -> NormalizedSubscription:
    plan_id_raw = data.get("plan") or data.get("payment_plan")
    plan_id = str(plan_id_raw) if plan_id_raw is not None else None
    plan: SubscriptionPlan | None = None
    if plan_id == settings.FLUTTERWAVE_PLAN_PRO:
        plan = SubscriptionPlan.PRO
    elif plan_id == settings.FLUTTERWAVE_PLAN_ENTERPRISE:
        plan = SubscriptionPlan.ENTERPRISE
    return NormalizedSubscription(
        provider="flutterwave",
        provider_subscription_id=str(data.get("id", "")),
        provider_customer_id=(data.get("customer") or {}).get("id") if isinstance(data.get("customer"), dict) else None,
        plan=plan,
        status=data.get("status", "active"),
        user_id=user_id,
    )

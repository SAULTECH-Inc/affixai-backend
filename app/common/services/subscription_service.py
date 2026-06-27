"""Subscription helpers: trial provisioning, Stripe-sync, access policy.

Two entry points matter elsewhere in the app:

  - `ensure_subscription(user)` — called from auth.register so a new user
    immediately has a trial row.
  - `user_can_access_paid_features(user)` — used by the
    `require_active_subscription` dependency to gate paid endpoints.

Both are deliberately tolerant of missing Stripe configuration: when
STRIPE_SECRET_KEY is empty (dev), we operate in trial-only mode without ever
calling Stripe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from loguru import logger

from app.core.config import settings
from app.db.models.invoice import Invoice, InvoiceStatus
from app.db.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.db.models.user import User


# Map Stripe subscription status strings → our local enum.
_STRIPE_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.EXPIRED,
    "unpaid": SubscriptionStatus.PAST_DUE,
}


async def ensure_subscription(user: User) -> Subscription:
    """Get-or-create the user's subscription row. New users start as TRIALING."""
    sub = await Subscription.get_or_none(user_id=user.id)
    if sub:
        return sub

    sub = await Subscription.create(
        user_id=user.id,
        plan=SubscriptionPlan.TRIAL,
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=settings.FREE_TRIAL_DAYS),
    )
    logger.info(f"Trial subscription created for user {user.id}")
    return sub


def _is_within_period(end: datetime | None) -> bool:
    if end is None:
        return False
    now = datetime.now(timezone.utc)
    # Tolerate naive datetimes coming from DB drivers in some configurations.
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end > now


def user_can_access_paid_features(sub: Subscription) -> bool:
    """Policy: does this subscription allow paid feature access right now?

    Allowed:
      - ACTIVE with current_period_end in the future
      - TRIALING with trial_ends_at in the future
      - PAST_DUE (Stripe grace period) — let them in so they can update payment
      - ANY trial-expired state if FREE_TIER_ENABLED is True
    """
    if sub.status == SubscriptionStatus.ACTIVE and _is_within_period(sub.current_period_end):
        return True

    if sub.status == SubscriptionStatus.TRIALING:
        if _is_within_period(sub.trial_ends_at):
            return True
        # Trial expired but never paid — fall through to the global toggle.

    if sub.status == SubscriptionStatus.PAST_DUE:
        return True  # short grace period; Stripe will retry payment

    if settings.FREE_TIER_ENABLED:
        return True

    return False


def days_left(sub: Subscription) -> int | None:
    """How many days until access ends, or None if access has already lapsed."""
    end: datetime | None = None
    if sub.status == SubscriptionStatus.ACTIVE:
        end = sub.current_period_end
    elif sub.status == SubscriptionStatus.TRIALING:
        end = sub.trial_ends_at

    if not end:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - datetime.now(timezone.utc)
    return max(delta.days, 0) if delta.total_seconds() > 0 else None


# ---- Webhook sync helpers ----------------------------------------------------


async def sync_subscription_from_stripe(stripe_sub: dict[str, Any]) -> None:
    """Reconcile our Subscription row with a Stripe subscription payload."""
    customer_id = stripe_sub.get("customer")
    if not customer_id:
        logger.warning("sync_subscription_from_stripe: missing customer id")
        return

    record = await Subscription.get_or_none(stripe_customer_id=customer_id)
    if not record:
        logger.warning(
            f"sync_subscription_from_stripe: no local record for customer {customer_id}"
        )
        return

    stripe_status = stripe_sub.get("status", "")
    record.status = _STRIPE_STATUS_MAP.get(stripe_status, record.status)
    record.stripe_subscription_id = stripe_sub.get("id") or record.stripe_subscription_id

    items = (stripe_sub.get("items", {}) or {}).get("data") or []
    if items:
        price = (items[0].get("price") or {})
        record.stripe_price_id = price.get("id") or record.stripe_price_id

    if cps := stripe_sub.get("current_period_start"):
        record.current_period_start = datetime.fromtimestamp(cps, tz=timezone.utc)
    if cpe := stripe_sub.get("current_period_end"):
        record.current_period_end = datetime.fromtimestamp(cpe, tz=timezone.utc)
    if trial_end := stripe_sub.get("trial_end"):
        record.trial_ends_at = datetime.fromtimestamp(trial_end, tz=timezone.utc)

    record.cancel_at_period_end = bool(stripe_sub.get("cancel_at_period_end"))
    if canceled := stripe_sub.get("canceled_at"):
        record.canceled_at = datetime.fromtimestamp(canceled, tz=timezone.utc)

    # If the Stripe sub is active and using the PRO price, set our plan accordingly.
    if record.status == SubscriptionStatus.ACTIVE:
        record.plan = SubscriptionPlan.PRO

    await record.save()
    logger.info(f"Subscription synced for customer {customer_id}: status={record.status}")


async def mark_canceled_from_stripe(stripe_sub: dict[str, Any]) -> None:
    customer_id = stripe_sub.get("customer")
    record = await Subscription.get_or_none(stripe_customer_id=customer_id)
    if not record:
        return
    record.status = SubscriptionStatus.CANCELED
    record.canceled_at = datetime.now(timezone.utc)
    await record.save()


# ---- Provider-agnostic apply helpers ----------------------------------------
#
# The payments/ abstraction emits NormalizedSubscription / NormalizedInvoice
# regardless of which gateway sent the webhook. These two functions resolve
# the local Subscription row and update it accordingly.


# Loose status map covering all three providers.
_PROVIDER_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trialing": SubscriptionStatus.TRIALING,
    "trial": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "attention": SubscriptionStatus.PAST_DUE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "cancelled": SubscriptionStatus.CANCELED,
    "complete": SubscriptionStatus.ACTIVE,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.EXPIRED,
    "non-renewing": SubscriptionStatus.CANCELED,
}


async def apply_normalized_subscription(norm: Any) -> Subscription | None:
    """Merge a NormalizedSubscription into the local Subscription row.

    Resolution order for finding the row:
      1. user_id from metadata (provider attaches our user_id to checkouts)
      2. provider_customer_id match
      3. stripe_customer_id match (legacy Stripe-only rows)
    """
    from uuid import UUID
    record: Subscription | None = None
    if norm.user_id:
        try:
            record = await Subscription.get_or_none(user_id=UUID(norm.user_id))
        except (ValueError, TypeError):
            record = None
    if not record and norm.provider_customer_id:
        record = await Subscription.get_or_none(provider_customer_id=norm.provider_customer_id)
    if not record and norm.provider == "stripe" and norm.provider_customer_id:
        record = await Subscription.get_or_none(stripe_customer_id=norm.provider_customer_id)
    if not record:
        logger.warning(
            f"apply_normalized_subscription: no local row for "
            f"provider={norm.provider} user={norm.user_id} "
            f"customer={norm.provider_customer_id}"
        )
        return None

    record.provider = norm.provider
    if norm.plan:
        record.plan = norm.plan
    mapped = _PROVIDER_STATUS_MAP.get((norm.status or "").lower())
    if mapped:
        record.status = mapped
    if norm.provider_customer_id:
        record.provider_customer_id = norm.provider_customer_id
        if norm.provider == "stripe":
            record.stripe_customer_id = norm.provider_customer_id
    if norm.provider_subscription_id:
        record.provider_subscription_id = norm.provider_subscription_id
        if norm.provider == "stripe":
            record.stripe_subscription_id = norm.provider_subscription_id
    if norm.current_period_start:
        record.current_period_start = norm.current_period_start
    if norm.current_period_end:
        record.current_period_end = norm.current_period_end
    if norm.trial_ends_at:
        record.trial_ends_at = norm.trial_ends_at
    record.cancel_at_period_end = norm.cancel_at_period_end

    await record.save()
    logger.info(
        f"subscription synced: provider={record.provider} "
        f"plan={record.plan} status={record.status} user={record.user_id}"
    )
    return record


async def apply_normalized_invoice(norm: Any) -> None:
    """UPSERT an Invoice row from a NormalizedInvoice.

    If we can't resolve the user_id, log a warning and bail — orphan invoices
    aren't useful to surface in the UI.
    """
    user_id: UUID | None = None
    if norm.user_id:
        try:
            user_id = UUID(norm.user_id)
        except (ValueError, TypeError):
            user_id = None
    if not user_id and norm.provider == "stripe":
        # Fall back to resolving via subscription's stripe_customer_id.
        customer_id = (norm.metadata or {}).get("customer_id")
        if customer_id:
            sub = await Subscription.get_or_none(stripe_customer_id=customer_id)
            if sub:
                user_id = sub.user_id

    if not user_id:
        logger.warning(
            f"apply_normalized_invoice: could not resolve user for "
            f"{norm.provider}:{norm.provider_invoice_id}"
        )
        return

    status_map = {
        "paid": InvoiceStatus.PAID,
        "failed": InvoiceStatus.FAILED,
        "refunded": InvoiceStatus.REFUNDED,
        "pending": InvoiceStatus.PENDING,
    }
    status = status_map.get(norm.status, InvoiceStatus.PENDING)

    existing = await Invoice.get_or_none(
        provider=norm.provider, provider_invoice_id=norm.provider_invoice_id
    )
    if existing:
        was_paid = existing.status == InvoiceStatus.PAID
        was_failed = existing.status == InvoiceStatus.FAILED
        existing.status = status
        existing.amount = norm.amount
        existing.currency = norm.currency
        existing.description = norm.description or existing.description
        existing.hosted_url = norm.hosted_url or existing.hosted_url
        existing.pdf_url = norm.pdf_url or existing.pdf_url
        existing.paid_at = norm.paid_at or existing.paid_at
        existing.metadata = {**(existing.metadata or {}), **(norm.metadata or {})}
        await existing.save()
        logger.info(
            f"invoice updated: {norm.provider}:{norm.provider_invoice_id} → {status}"
        )
        if status == InvoiceStatus.PAID and not was_paid:
            await _accrue_referral_commission(existing)
            await _send_invoice_email(user_id, existing, norm, kind="paid")
        elif status == InvoiceStatus.FAILED and not was_failed:
            await _send_invoice_email(user_id, existing, norm, kind="failed")
        return

    created = await Invoice.create(
        user_id=user_id,
        provider=norm.provider,
        provider_invoice_id=norm.provider_invoice_id,
        amount=norm.amount,
        currency=norm.currency,
        status=status,
        description=norm.description,
        hosted_url=norm.hosted_url,
        pdf_url=norm.pdf_url,
        paid_at=norm.paid_at,
        metadata=norm.metadata or {},
    )
    logger.info(
        f"invoice created: {norm.provider}:{norm.provider_invoice_id} "
        f"user={user_id} amount={norm.amount} {norm.currency} → {status}"
    )
    if status == InvoiceStatus.PAID:
        await _accrue_referral_commission(created)
        await _send_invoice_email(user_id, created, norm, kind="paid")
    elif status == InvoiceStatus.FAILED:
        await _send_invoice_email(user_id, created, norm, kind="failed")


async def _send_invoice_email(
    user_id: UUID,
    invoice: Invoice,
    norm: Any,
    *,
    kind: str,  # "paid" | "failed"
) -> None:
    """Best-effort billing email — never raises so invoice processing is safe."""
    try:
        from app.common.services.email_service import (
            send_subscription_activated_email,
            send_payment_failed_email,
        )

        user = await User.get_or_none(id=user_id, deleted_at=None)
        if not user:
            return

        sub = await Subscription.get_or_none(user_id=user_id)
        plan_name = sub.plan.value.title() if sub else "Pro"
        amount = norm.amount or invoice.amount
        currency = (norm.currency or invoice.currency or "USD").upper()
        invoice_url = norm.hosted_url or norm.pdf_url or invoice.hosted_url or invoice.pdf_url

        if kind == "paid":
            await send_subscription_activated_email(
                user.email,
                plan_name=plan_name,
                amount=amount,
                currency=currency,
                billing_period_end=sub.current_period_end if sub else None,
                invoice_url=invoice_url,
            )
        elif kind == "failed":
            billing_url = f"{settings.FRONTEND_URL}/billing"
            await send_payment_failed_email(
                user.email,
                plan_name=plan_name,
                amount=amount,
                currency=currency,
                update_url=billing_url,
                invoice_url=invoice_url,
            )
    except Exception as exc:
        logger.warning(f"billing email ({kind}) failed for user {user_id}: {exc}")


async def _accrue_referral_commission(invoice: Invoice) -> None:
    """Best-effort: failures here should NEVER prevent the invoice itself
    from being persisted, so we swallow + log."""
    try:
        from app.common.services.referral_service import record_commission_event

        await record_commission_event(invoice)
    except Exception as exc:
        logger.warning(
            f"referral commission accrual failed for invoice {invoice.id}: {exc}"
        )

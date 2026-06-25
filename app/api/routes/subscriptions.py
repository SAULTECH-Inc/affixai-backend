"""Subscription routes — provider-agnostic billing endpoints.

Endpoints:
  GET  /me                — current user's subscription state
  GET  /plans             — public plans catalog (frontend pricing page)
  GET  /invoices          — user's invoice history
  POST /checkout          — start a checkout via the ACTIVE provider
  POST /portal            — open the provider's hosted customer portal (Stripe only)
  POST /cancel            — cancel at period end (provider-specific)
  POST /webhook/{provider} — provider-signed webhook receiver (no auth)

All gateway-specific logic lives behind `payments.get_provider()`. The active
provider is selected by `settings.PAYMENT_PROVIDER`.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.payments import (
    ProviderError,
    active_provider_name,
    get_provider,
)
from app.common.services.payments.router import (
    price_for as region_price_for,
    provider_for_user,
    provider_name_for_country,
    region_for_country,
)
from app.common.geo import country_from_request
from app.common.services.subscription_service import (
    apply_normalized_invoice,
    apply_normalized_subscription,
    days_left,
    ensure_subscription,
    user_can_access_paid_features,
)
from app.core.config import settings
from app.db.models.audit_log import AuditAction
from app.db.models.invoice import Invoice
from app.db.models.stripe_event import StripeEvent
from app.db.models.subscription import SubscriptionPlan
from app.db.models.user import User
from app.models.subscription_schemas import (
    CheckoutDto,
    CheckoutOut,
    InvoiceOut,
    PlanOut,
    PortalDto,
    PortalOut,
    SubscriptionStateOut,
)

router = APIRouter()


# ---- Plans catalog ----------------------------------------------------------


# Static pricing for the MVP (USD). When wiring to providers, set the
# corresponding *_PRICE_PRO / *_PLAN_PRO env vars to the right id. The amount
# below is for display only — the gateway is source of truth.
_PLAN_DEFAULTS: dict[SubscriptionPlan, dict] = {
    SubscriptionPlan.PRO: {
        "name": "Pro",
        "description": "Unlimited auto-affix, vault, signed-doc email & rich extraction.",
        "amount": Decimal("19.00"),
        "features": [
            "Unlimited document signings",
            "AI auto-affix on any form",
            "Rich vault with auto-extract",
            "Email signed PDFs",
            "Draft auto-save",
            "Priority support",
        ],
    },
    SubscriptionPlan.ENTERPRISE: {
        "name": "Enterprise",
        "description": "Bulk-sign API, multi-user, custom rate limits.",
        "amount": Decimal("99.00"),
        "features": [
            "Everything in Pro",
            "Bulk-sign REST API",
            "Test + live API keys",
            "Multi-user organization",
            "SLA & priority support",
        ],
    },
}


@router.get("/me", response_model=SubscriptionStateOut)
async def my_subscription(user: User = Depends(get_current_user)) -> SubscriptionStateOut:
    sub = await ensure_subscription(user)
    # The "active provider" we surface is the one THIS user would be routed
    # to (based on their country) — not the platform global default. That's
    # what the BillingPage uses to label the upgrade button correctly.
    user_provider = sub.provider or provider_name_for_country(user.country_code)
    user_region = region_for_country(user.country_code)
    return SubscriptionStateOut(
        plan=sub.plan,
        status=sub.status,
        provider=sub.provider,
        trial_ends_at=sub.trial_ends_at,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        has_paid_features=user_can_access_paid_features(sub),
        free_tier_enabled=settings.FREE_TIER_ENABLED,
        active_provider=user_provider,
        currency=(
            next(iter(user_region.pricing.values())).currency
            if user_region.pricing
            else (settings.BILLING_CURRENCY or "USD")
        ),
        days_left=days_left(sub),
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    request: Request,
    country: str | None = None,
) -> list[PlanOut]:
    """Plans catalog — country-aware.

    Resolution order for the country:
      1. `?country=XX` query param (frontend can pass the user's selection /
         their auto-detect result)
      2. CDN edge header on the request (CF-IPCountry / X-Vercel-IP-Country)
      3. None → platform default (Stripe pricing)

    The response includes:
      * region-specific amount + currency (NGN for NG, USD for the rest)
      * the gateway's native `price_id` (Stripe price_xxx, Paystack PLN_xxx,
        Flutterwave numeric id) — null if that gateway hasn't been
        configured yet, in which case the frontend shows the price but
        disables the upgrade button.
    """
    resolved_country = (country or "").upper() or country_from_request(request)
    region = region_for_country(resolved_country)
    # Use the region's preferred provider for the price_id lookup.
    provider = get_provider(region.provider)

    plans: list[PlanOut] = [
        PlanOut(
            plan=SubscriptionPlan.TRIAL,
            name="Free Trial",
            description=f"{settings.FREE_TRIAL_DAYS}-day free trial — all features unlocked.",
            price_id=None,
            amount=Decimal("0"),
            currency="USD",
            interval="month",
            features=[
                f"{settings.FREE_TRIAL_DAYS}-day trial",
                "All Pro features",
                "No card required",
            ],
            free_trial_days=settings.FREE_TRIAL_DAYS,
        ),
    ]
    for plan, defaults in _PLAN_DEFAULTS.items():
        # Region-specific price overrides the static default.
        regional = region_price_for(resolved_country, plan)
        amount = regional.amount if regional else defaults["amount"]
        currency = regional.currency if regional else (settings.BILLING_CURRENCY or "USD")
        price_id = provider.price_id_for(plan)
        plans.append(
            PlanOut(
                plan=plan,
                name=defaults["name"],
                description=defaults["description"],
                price_id=price_id,
                amount=amount,
                currency=currency,
                interval="month",
                features=defaults["features"],
            )
        )
    return plans


@router.get("/invoices", response_model=list[InvoiceOut])
async def my_invoices(user: User = Depends(get_current_user)) -> list[InvoiceOut]:
    rows = await Invoice.filter(user_id=user.id).order_by("-created_at")
    return [
        InvoiceOut(
            id=r.id,
            provider=r.provider,
            provider_invoice_id=r.provider_invoice_id,
            amount=r.amount,
            currency=r.currency,
            status=r.status,
            description=r.description,
            hosted_url=r.hosted_url,
            pdf_url=r.pdf_url,
            paid_at=r.paid_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---- Checkout / portal ------------------------------------------------------


@router.post("/checkout", response_model=CheckoutOut)
async def start_checkout(
    payload: CheckoutDto, user: User = Depends(get_current_user)
) -> CheckoutOut:
    """Open a checkout session on the provider that matches the user's
    country — Paystack for Nigeria, Flutterwave for other African
    countries, Stripe everywhere else. Falls back to the platform default
    when the user has no country_code attached."""
    provider = provider_for_user(user)
    if not provider.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{provider.name.title()} is not configured on this server. "
                f"This is the gateway for your region "
                f"({user.country_code or 'unknown'}); ask an admin to set "
                f"the relevant API keys, or update your country in Settings."
            ),
        )
    if provider.price_id_for(payload.plan) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No {provider.name} price configured for plan {payload.plan.value}. "
                "Ask an admin to set the price/plan id in the .env."
            ),
        )

    sub = await ensure_subscription(user)
    success_url = (
        str(payload.success_url) if payload.success_url
        else f"{settings.FRONTEND_URL}/billing?status=success"
    )
    cancel_url = (
        str(payload.cancel_url) if payload.cancel_url
        else f"{settings.FRONTEND_URL}/billing?status=canceled"
    )

    # Trial honor: give them a fresh trial period only if they haven't been
    # through checkout yet on ANY provider (provider_subscription_id is null
    # AND stripe_subscription_id is null).
    has_been_through_checkout = bool(
        sub.provider_subscription_id or sub.stripe_subscription_id
    )
    trial_days = None if has_been_through_checkout else settings.FREE_TRIAL_DAYS

    # Stripe needs the customer id reused across checkouts; other providers
    # create customer on first charge.
    existing_customer = (
        sub.provider_customer_id
        or (sub.stripe_customer_id if provider.name == "stripe" else None)
    )
    try:
        result = await provider.create_checkout(
            user_id=str(user.id),
            user_email=user.email,
            user_name=(
                " ".join(filter(None, [user.first_name, user.last_name])).strip()
                or None
            ),
            plan=payload.plan,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_days=trial_days,
            existing_customer_id=existing_customer,
        )
    except ProviderError as exc:
        logger.warning(f"checkout failed via {provider.name}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    # Persist whatever customer-identifier we now have. Stripe gives us one
    # at customer create; others give us nothing here (we'll backfill at
    # webhook time).
    sub.provider = provider.name
    if result.customer_id:
        sub.provider_customer_id = result.customer_id
        if provider.name == "stripe":
            sub.stripe_customer_id = result.customer_id
        await sub.save()

    await log_audit(
        user_id=user.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="subscription",
        entity_id=str(sub.id),
        description=f"Started {provider.name} checkout for {payload.plan.value}",
        metadata={"plan": payload.plan.value, "provider": provider.name},
    )
    return CheckoutOut(checkout_url=result.checkout_url)


@router.post("/portal", response_model=PortalOut)
async def open_portal(
    payload: PortalDto, user: User = Depends(get_current_user)
) -> PortalOut:
    """Open the user's resolved-provider hosted portal.

    For users routed to Stripe this opens the full self-serve portal. For
    Paystack / Flutterwave the provider raises NotImplemented and we
    surface a 501 — those gateways don't ship a comparable customer portal
    yet, so the user manages their subscription via the support team.
    """
    # Prefer the subscription's recorded provider (where their card lives).
    # Falls back to country routing for users who haven't started checkout.
    sub = await ensure_subscription(user)
    provider_name = sub.provider or provider_name_for_country(user.country_code)
    provider = get_provider(provider_name)
    if not provider.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider.name.title()} is not configured on this server",
        )
    customer_id = sub.provider_customer_id or sub.stripe_customer_id
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No customer record for this user yet — start a checkout first",
        )
    return_url = (
        str(payload.return_url) if payload.return_url
        else f"{settings.FRONTEND_URL}/billing"
    )
    try:
        result = await provider.open_portal(
            customer_id=customer_id, return_url=return_url
        )
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc
    return PortalOut(portal_url=result.portal_url)


# ---- Webhooks ---------------------------------------------------------------


@router.post("/webhook/{provider_name}", include_in_schema=False)
async def webhook(provider_name: str, request: Request) -> dict:
    """Provider-specific webhook receiver. Idempotent on event id.

    Each provider's verify_and_parse_webhook handles its own signature
    scheme; we then apply the normalized event uniformly.

    Configure:
      Stripe:       https://<host>/api/v1/subscriptions/webhook/stripe
      Paystack:     https://<host>/api/v1/subscriptions/webhook/paystack
      Flutterwave:  https://<host>/api/v1/subscriptions/webhook/flutterwave
    """
    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.verify_and_parse_webhook(body=body, headers=headers)
    except ProviderError as exc:
        logger.warning(f"webhook verify failed ({provider_name}): {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        ) from exc

    # Idempotency. We piggy-back on the existing StripeEvent table even for
    # non-Stripe providers — the column is just a string. (A future cleanup
    # would rename the table to `processed_webhooks` to reflect this.)
    if event.event_id:
        if await StripeEvent.get_or_none(event_id=event.event_id):
            return {"received": True, "duplicate": True, "kind": event.kind}
        try:
            await StripeEvent.create(
                event_id=event.event_id, event_type=event.kind
            )
        except Exception as exc:
            logger.warning(f"could not record webhook event {event.event_id}: {exc}")
            return {"received": True, "duplicate": True, "kind": event.kind}

    if event.kind in {"subscription.created", "subscription.updated", "subscription.canceled"}:
        if event.subscription:
            await apply_normalized_subscription(event.subscription)
    elif event.kind in {"invoice.paid", "invoice.failed"}:
        if event.invoice:
            await apply_normalized_invoice(event.invoice)

    return {"received": True, "kind": event.kind}

"""Admin-only routes for platform owners.

These endpoints require `UserRole.SUPER_ADMIN`. They're separate from the
enterprise-admin routes (which manage an organization, not the platform).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from decimal import Decimal

from fastapi import Query
from tortoise.expressions import Q

from app.common.deps import require_super_admin
from app.common.services.audit_service import log_audit
from app.common.services.subscription_service import ensure_subscription
from app.core.config import settings
from app.db.models.audit_log import AuditAction
from app.db.models.document import Document
from app.db.models.enterprise import Enterprise
from app.db.models.invoice import Invoice, InvoiceStatus
from app.db.models.subscription import Subscription, SubscriptionStatus
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.subscription_schemas import (
    AdminStatsOut,
    AdminUserRow,
    AdminUsersOut,
    ExtendTrialDto,
    SubscriptionStateOut,
)

router = APIRouter()


@router.post(
    "/users/{user_id}/extend-trial",
    response_model=SubscriptionStateOut,
)
async def extend_trial(
    user_id: UUID,
    payload: ExtendTrialDto,
    admin: User = Depends(require_super_admin),
) -> SubscriptionStateOut:
    """Add `payload.days` to the target user's `trial_ends_at`.

    Useful for power users, sales pilots, and goodwill credits. We:
      * never SHORTEN a trial (use the cancel endpoint for that)
      * extend from whichever is later: now() or current trial_ends_at
        (otherwise extending a long-expired trial by 7 days does nothing)
      * flip status back to TRIALING so the user regains access
    """
    target = await User.get_or_none(id=user_id, deleted_at=None)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    sub = await ensure_subscription(target)

    now = datetime.now(timezone.utc)
    anchor = sub.trial_ends_at or now
    # If the existing trial date is naive (some DB drivers), tz-localize.
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    base = anchor if anchor > now else now
    sub.trial_ends_at = base + timedelta(days=payload.days)
    sub.status = SubscriptionStatus.TRIALING
    await sub.save()

    await log_audit(
        user_id=admin.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="subscription",
        entity_id=str(sub.id),
        description=(
            f"Admin {admin.email} extended trial for {target.email} "
            f"by {payload.days} days"
            + (f" (reason: {payload.reason})" if payload.reason else "")
        ),
        metadata={
            "target_user_id": str(target.id),
            "days": payload.days,
            "reason": payload.reason,
            "new_trial_ends_at": sub.trial_ends_at.isoformat(),
        },
    )

    from app.common.services.subscription_service import (
        days_left,
        user_can_access_paid_features,
    )
    from app.common.services.payments import active_provider_name
    from app.core.config import settings

    return SubscriptionStateOut(
        plan=sub.plan,
        status=sub.status,
        provider=sub.provider,
        trial_ends_at=sub.trial_ends_at,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        has_paid_features=user_can_access_paid_features(sub),
        free_tier_enabled=settings.FREE_TIER_ENABLED,
        active_provider=active_provider_name(),
        currency=settings.BILLING_CURRENCY or "USD",
        days_left=days_left(sub),
    )


@router.post(
    "/users/{user_id}/grant-access",
    response_model=MessageOut,
)
async def grant_paid_access(
    user_id: UUID,
    admin: User = Depends(require_super_admin),
) -> MessageOut:
    """Force the target user's subscription to ACTIVE with a far-future
    period end. Equivalent to a permanent comp. Use sparingly.
    """
    target = await User.get_or_none(id=user_id, deleted_at=None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    sub = await ensure_subscription(target)
    sub.status = SubscriptionStatus.ACTIVE
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=365 * 10)
    sub.cancel_at_period_end = False
    await sub.save()
    await log_audit(
        user_id=admin.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="subscription",
        entity_id=str(sub.id),
        description=f"Admin {admin.email} granted complimentary access to {target.email}",
        metadata={"target_user_id": str(target.id)},
    )
    return MessageOut(message=f"Granted 10-year comp access to {target.email}")


# ---- Read endpoints ----------------------------------------------------------


@router.get("/users", response_model=AdminUsersOut)
async def list_users(
    q: str | None = Query(default=None, description="Email/name substring"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(require_super_admin),
) -> AdminUsersOut:
    """Paginated user list for the admin panel. `q` matches against email,
    first_name, or last_name (case-insensitive).
    """
    query = User.filter(deleted_at=None)
    if q:
        needle = q.strip()
        query = query.filter(
            Q(email__icontains=needle)
            | Q(first_name__icontains=needle)
            | Q(last_name__icontains=needle)
        )
    total = await query.count()
    users = await query.order_by("-created_at").offset(offset).limit(limit)

    # Bulk-fetch subscriptions in one round-trip rather than N+1.
    user_ids = [u.id for u in users]
    subs = await Subscription.filter(user_id__in=user_ids)
    sub_by_user = {s.user_id: s for s in subs}

    items = [
        AdminUserRow(
            id=u.id,
            email=u.email,
            first_name=u.first_name,
            last_name=u.last_name,
            role=u.role.value if hasattr(u.role, "value") else str(u.role),
            status=u.status.value if hasattr(u.status, "value") else str(u.status),
            enterprise_id=u.enterprise_id,
            plan=(sub_by_user.get(u.id).plan if sub_by_user.get(u.id) else None),
            sub_status=(sub_by_user.get(u.id).status if sub_by_user.get(u.id) else None),
            trial_ends_at=(
                sub_by_user.get(u.id).trial_ends_at if sub_by_user.get(u.id) else None
            ),
            current_period_end=(
                sub_by_user.get(u.id).current_period_end if sub_by_user.get(u.id) else None
            ),
            last_login_at=u.last_login_at,
            created_at=u.created_at,
        )
        for u in users
    ]
    return AdminUsersOut(items=items, total=total, limit=limit, offset=offset)


@router.get("/stats", response_model=AdminStatsOut)
async def platform_stats(
    admin: User = Depends(require_super_admin),
) -> AdminStatsOut:
    """Platform-wide health snapshot. Counts only — fast even with many rows."""
    # Month-to-date window in UTC.
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_users = await User.filter(deleted_at=None).count()
    active_subs = await Subscription.filter(status=SubscriptionStatus.ACTIVE).count()
    trialing = await Subscription.filter(status=SubscriptionStatus.TRIALING).count()
    enterprises = await Enterprise.filter(deleted_at=None).count()

    documents_this_month = await Document.filter(
        deleted_at=None, created_at__gte=month_start
    ).count()
    invoices_this_month = await Invoice.filter(
        status=InvoiceStatus.PAID, created_at__gte=month_start
    ).count()

    # Sum revenue in Python — invoice volume is small, no need for a SQL
    # aggregate that complicates things across providers/currencies.
    paid_rows = await Invoice.filter(
        status=InvoiceStatus.PAID, created_at__gte=month_start
    ).only("amount", "currency")
    # We only sum invoices in the active billing currency. Mixed-currency
    # totals would be misleading.
    active_currency = (settings.BILLING_CURRENCY or "USD").upper()
    revenue = sum(
        (r.amount for r in paid_rows if r.currency.upper() == active_currency),
        start=Decimal("0"),
    )

    return AdminStatsOut(
        total_users=total_users,
        active_subscriptions=active_subs,
        trialing=trialing,
        documents_this_month=documents_this_month,
        invoices_this_month=invoices_this_month,
        revenue_this_month=revenue,
        currency=active_currency,
        enterprises=enterprises,
    )


# ---- Leads (Contact + Careers inbox) ---------------------------------------
#
# Super-admins read and triage the inbound submissions from the public
# /contact and /careers forms. Lead rows themselves are CREATED by the
# public POST /api/v1/leads endpoint — these admin handlers are read-only
# + a single status update.


from app.db.models.lead import Lead, LeadKind, LeadStatus  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class AdminLeadRow(BaseModel):
    id: UUID
    kind: LeadKind
    status: LeadStatus
    name: str
    email: str
    topic: str | None
    message: str
    extra: dict | None
    ip_address: str | None
    created_at: datetime
    reviewed_at: datetime | None


class AdminLeadsOut(BaseModel):
    total: int
    new_count: int
    rows: list[AdminLeadRow]


class LeadStatusUpdateDto(BaseModel):
    status: LeadStatus


@router.get("/leads", response_model=AdminLeadsOut)
async def list_leads(
    admin: User = Depends(require_super_admin),
    kind: LeadKind | None = Query(default=None),
    status_filter: LeadStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, le=500, ge=1),
) -> AdminLeadsOut:
    """List inbound leads, most recent first. Filterable by kind/status."""
    qs = Lead.all()
    if kind is not None:
        qs = qs.filter(kind=kind)
    if status_filter is not None:
        qs = qs.filter(status=status_filter)

    rows = await qs.order_by("-created_at").limit(limit)
    total = await Lead.filter(
        **({"kind": kind} if kind is not None else {})
    ).count()
    new_count = await Lead.filter(
        status=LeadStatus.NEW,
        **({"kind": kind} if kind is not None else {}),
    ).count()

    return AdminLeadsOut(
        total=total,
        new_count=new_count,
        rows=[
            AdminLeadRow(
                id=r.id,
                kind=r.kind,
                status=r.status,
                name=r.name,
                email=r.email,
                topic=r.topic,
                message=r.message,
                extra=r.extra,
                ip_address=r.ip_address,
                created_at=r.created_at,
                reviewed_at=r.reviewed_at,
            )
            for r in rows
        ],
    )


@router.patch("/leads/{lead_id}", response_model=AdminLeadRow)
async def update_lead_status(
    lead_id: UUID,
    payload: LeadStatusUpdateDto,
    admin: User = Depends(require_super_admin),
) -> AdminLeadRow:
    """Mark a lead reviewed / archived / back to new."""
    lead = await Lead.get_or_none(id=lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead.status = payload.status
    # Stamp the reviewer when the status crosses into reviewed/archived; we
    # leave it alone if they're flipping back to "new" because that means
    # "unread" and a reviewer field would be misleading.
    if payload.status in (LeadStatus.REVIEWED, LeadStatus.ARCHIVED):
        lead.reviewed_at = datetime.now(timezone.utc)
        lead.reviewed_by_id = admin.id
    else:
        lead.reviewed_at = None
        lead.reviewed_by_id = None
    await lead.save()
    return AdminLeadRow(
        id=lead.id,
        kind=lead.kind,
        status=lead.status,
        name=lead.name,
        email=lead.email,
        topic=lead.topic,
        message=lead.message,
        extra=lead.extra,
        ip_address=lead.ip_address,
        created_at=lead.created_at,
        reviewed_at=lead.reviewed_at,
    )


# ---- Referrals (affiliate program payouts) ---------------------------------
#
# Super-admins use these to see all referrals across the platform and to
# export a CSV they can hand to finance / accounting for payout.


from app.db.models.referral import Referral, ReferralStatus  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
import csv  # noqa: E402
import io  # noqa: E402


class AdminReferralRow(BaseModel):
    id: UUID
    referrer_email: str
    referred_email: str
    code_used: str
    status: ReferralStatus
    signed_up_at: datetime
    commission_started_at: datetime | None
    commission_expires_at: datetime | None
    total_commission: Decimal
    commission_currency: str
    paid_out_at: datetime | None
    paid_out_amount: Decimal | None
    payout_reference: str | None


class AdminReferralsOut(BaseModel):
    total: int
    pending_payout_total_by_currency: dict[str, Decimal]
    rows: list[AdminReferralRow]


async def _hydrate_referral_rows(rows: list[Referral]) -> list[AdminReferralRow]:
    """Join referrer + referred emails in one IN-query for the listing view."""
    user_ids = {r.referrer_user_id for r in rows} | {r.referred_user_id for r in rows}
    users = await User.filter(id__in=list(user_ids)).only("id", "email")
    email_by_id = {u.id: u.email for u in users}
    return [
        AdminReferralRow(
            id=r.id,
            referrer_email=email_by_id.get(r.referrer_user_id, "?"),
            referred_email=email_by_id.get(r.referred_user_id, "?"),
            code_used=r.code_used,
            status=r.status,
            signed_up_at=r.signed_up_at,
            commission_started_at=r.commission_started_at,
            commission_expires_at=r.commission_expires_at,
            total_commission=r.total_commission,
            commission_currency=r.commission_currency,
            paid_out_at=r.paid_out_at,
            paid_out_amount=r.paid_out_amount,
            payout_reference=r.payout_reference,
        )
        for r in rows
    ]


@router.get("/referrals", response_model=AdminReferralsOut)
async def list_referrals(
    admin: User = Depends(require_super_admin),
    status_filter: ReferralStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> AdminReferralsOut:
    qs = Referral.all()
    if status_filter is not None:
        qs = qs.filter(status=status_filter)
    rows = await qs.order_by("-signed_up_at").limit(limit)

    # Per-currency pending payout totals — Naira can't be summed with USD.
    pending: dict[str, Decimal] = {}
    for r in rows:
        if r.status == ReferralStatus.CONVERTED:
            outstanding = r.total_commission - (r.paid_out_amount or Decimal("0"))
            if outstanding > 0:
                pending[r.commission_currency] = (
                    pending.get(r.commission_currency, Decimal("0")) + outstanding
                )

    return AdminReferralsOut(
        total=await Referral.all().count(),
        pending_payout_total_by_currency=pending,
        rows=await _hydrate_referral_rows(rows),
    )


@router.get("/referrals/export.csv")
async def export_referrals_csv(
    admin: User = Depends(require_super_admin),
    status_filter: ReferralStatus | None = Query(default=None, alias="status"),
) -> StreamingResponse:
    """CSV of referrals for payout / accounting. One row per referred user;
    columns include both emails, amount owed, currency, last payout. Open
    in Excel or feed into Stripe Connect / Wise."""
    qs = Referral.all()
    if status_filter is not None:
        qs = qs.filter(status=status_filter)
    rows = await qs.order_by("-signed_up_at")

    hydrated = await _hydrate_referral_rows(rows)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "referral_id", "referrer_email", "referred_email", "code", "status",
            "signed_up_at", "commission_started_at", "commission_expires_at",
            "total_commission", "currency", "paid_out_amount", "paid_out_at",
            "outstanding", "payout_reference",
        ]
    )
    for r in hydrated:
        outstanding = r.total_commission - (r.paid_out_amount or Decimal("0"))
        writer.writerow([
            str(r.id), r.referrer_email, r.referred_email, r.code_used, r.status.value,
            r.signed_up_at.isoformat(),
            r.commission_started_at.isoformat() if r.commission_started_at else "",
            r.commission_expires_at.isoformat() if r.commission_expires_at else "",
            str(r.total_commission), r.commission_currency,
            str(r.paid_out_amount) if r.paid_out_amount is not None else "",
            r.paid_out_at.isoformat() if r.paid_out_at else "",
            str(outstanding), r.payout_reference or "",
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename=referrals-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
            )
        },
    )


class MarkPaidDto(BaseModel):
    amount: Decimal
    payout_reference: str | None = None


@router.post("/referrals/{referral_id}/mark-paid", response_model=AdminReferralRow)
async def mark_referral_paid(
    referral_id: UUID,
    payload: MarkPaidDto,
    admin: User = Depends(require_super_admin),
) -> AdminReferralRow:
    """Record a manual payout against a referrer. Adds to paid_out_amount
    so successive payouts accumulate, and stamps the timestamp."""
    referral = await Referral.get_or_none(id=referral_id)
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")
    referral.paid_out_amount = (referral.paid_out_amount or Decimal("0")) + payload.amount
    referral.paid_out_at = datetime.now(timezone.utc)
    if payload.payout_reference:
        referral.payout_reference = payload.payout_reference
    await referral.save()
    return (await _hydrate_referral_rows([referral]))[0]

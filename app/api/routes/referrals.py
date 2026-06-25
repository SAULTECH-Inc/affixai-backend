"""User-facing referral endpoints.

  GET  /api/v1/referrals/me          → my code, share link, stats, recent referrals
  GET  /api/v1/referrals/events      → my commission events (drill-down)
  POST /api/v1/referrals/payout-request → user requests a payout (admin processes manually)

Admin-side endpoints live in admin.py (list all referrals across the
platform + CSV export). Keeping these split so a non-admin user calling
/admin/* still 403s cleanly.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.common.deps import get_current_user
from app.common.services.referral_service import ensure_code
from app.core.config import settings
from app.db.models.referral import CommissionEvent, Referral, ReferralStatus
from app.db.models.user import User


router = APIRouter()


# ---- Schemas ---------------------------------------------------------------


class ReferralRowOut(BaseModel):
    id: UUID
    # Email of the referred user — partially masked because surfacing a
    # full email of a third party to the referrer is creepy. "j***@x.com"
    referred_email_masked: str
    status: ReferralStatus
    signed_up_at: datetime
    commission_started_at: datetime | None
    commission_expires_at: datetime | None
    total_commission: Decimal
    commission_currency: str


class ReferralsMineOut(BaseModel):
    code: str
    share_url: str
    # Headline numbers — what the user actually wants to know.
    total_referred: int
    total_converted: int
    total_earned: Decimal
    total_paid_out: Decimal
    pending_payout: Decimal
    currency: str
    # Rate + window pulled from settings so the page can render
    # "You earn 20% for 12 months on every referred user's payments."
    rate_percent: int
    window_months: int
    rows: list[ReferralRowOut]


class CommissionEventOut(BaseModel):
    id: UUID
    referral_id: UUID
    amount: Decimal
    currency: str
    rate: Decimal
    occurred_at: datetime


# ---- Helpers ---------------------------------------------------------------


def _mask_email(email: str) -> str:
    """j***@example.com — keeps the first letter so the referrer can
    recognize a user they invited without us spraying full PII back."""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}{'*' * max(2, len(local) - 1)}@{domain}"


# ---- Routes ----------------------------------------------------------------


@router.get("/me", response_model=ReferralsMineOut)
async def my_referrals(user: User = Depends(get_current_user)) -> ReferralsMineOut:
    """Everything the /referrals page needs in one round-trip."""
    code = await ensure_code(user)

    rows = await Referral.filter(referrer_user_id=user.id).order_by("-signed_up_at")
    converted = [r for r in rows if r.status == ReferralStatus.CONVERTED]

    # Sum earnings + paid-out. Per-currency aggregation gets ugly when a
    # user has referrals across regions — we report in the FIRST currency
    # we see, which matches what the user mostly cares about (their
    # primary market). Admin CSV has per-row currency for full accuracy.
    primary_currency = next(
        (r.commission_currency for r in rows if r.total_commission > 0), "USD"
    )
    earned = sum(
        (r.total_commission for r in rows if r.commission_currency == primary_currency),
        start=Decimal("0"),
    )
    paid_out = sum(
        (
            (r.paid_out_amount or Decimal("0"))
            for r in rows
            if r.commission_currency == primary_currency
        ),
        start=Decimal("0"),
    )

    # Lazy-load referred users' emails for the masking. Could be a JOIN
    # but the list is small (most users have <20 referrals) so a single
    # IN-query is fine.
    referred_ids = [r.referred_user_id for r in rows]
    referred_users = await User.filter(id__in=referred_ids).only("id", "email")
    email_by_id = {u.id: u.email for u in referred_users}

    frontend_url = settings.FRONTEND_URL.rstrip("/")
    return ReferralsMineOut(
        code=code,
        share_url=f"{frontend_url}/?ref={code}",
        total_referred=len(rows),
        total_converted=len(converted),
        total_earned=earned,
        total_paid_out=paid_out,
        pending_payout=earned - paid_out,
        currency=primary_currency,
        rate_percent=int(getattr(settings, "REFERRAL_COMMISSION_RATE", 0.20) * 100),
        window_months=int(getattr(settings, "REFERRAL_COMMISSION_MONTHS", 12)),
        rows=[
            ReferralRowOut(
                id=r.id,
                referred_email_masked=_mask_email(
                    email_by_id.get(r.referred_user_id, "")
                ),
                status=r.status,
                signed_up_at=r.signed_up_at,
                commission_started_at=r.commission_started_at,
                commission_expires_at=r.commission_expires_at,
                total_commission=r.total_commission,
                commission_currency=r.commission_currency,
            )
            for r in rows
        ],
    )


@router.get("/events", response_model=list[CommissionEventOut])
async def my_events(user: User = Depends(get_current_user)) -> list[CommissionEventOut]:
    """Per-event commission history. Backs a "show me the math" drill-down
    on the referrals page."""
    my_refs = await Referral.filter(referrer_user_id=user.id).only("id")
    ref_ids = [r.id for r in my_refs]
    if not ref_ids:
        return []
    events = await CommissionEvent.filter(referral_id__in=ref_ids).order_by(
        "-occurred_at"
    )
    return [
        CommissionEventOut(
            id=e.id,
            referral_id=e.referral_id,
            amount=e.amount,
            currency=e.currency,
            rate=e.rate,
            occurred_at=e.occurred_at,
        )
        for e in events
    ]

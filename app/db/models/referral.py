"""Referral / affiliate tracking.

Two related tables:
  * `User.referral_code`            — each user has ONE public referral code
                                       (set on first /referrals page visit
                                       or eagerly at signup)
  * `Referral`                       — one row per referred signup,
                                       linking referrer → referred. Tracks
                                       lifecycle (signed up / paid /
                                       commission earned) so we can pay
                                       out and let users see their funnel.

Commission rule (encoded in `referral_service.py`, not the model):
  20% of net paid revenue for 12 months from the referred user's first
  successful charge. Adjust via env / settings without a schema change.

Why a separate `Referral` table rather than `User.referred_by_id`:
  * a user might be referred multiple times across re-signups; we want
    the audit trail
  * we record commission state per-referred-user, not per-referrer-user,
    so a 12-month window is easy to query
  * payout tooling iterates this table, not the user table
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from tortoise import fields
from tortoise.models import Model


class ReferralStatus(str, Enum):
    # Referred user signed up but hasn't paid yet — no commission earned.
    SIGNED_UP = "signed_up"
    # Referred user converted to a paid plan; commission is now accruing.
    CONVERTED = "converted"
    # 12-month commission window has elapsed; no more earnings.
    EXPIRED = "expired"
    # Spam / chargebacks / abuse — frozen, no payout.
    VOID = "void"


class Referral(Model):
    id = fields.UUIDField(pk=True)
    referrer_user_id = fields.UUIDField(index=True)
    referred_user_id = fields.UUIDField(unique=True)
    # Snapshot of the code used at signup — useful for forensics if a user
    # later changes their code (we don't allow that today, but might).
    code_used = fields.CharField(max_length=24, index=True)

    status = fields.CharEnumField(
        ReferralStatus, max_length=16, default=ReferralStatus.SIGNED_UP, index=True
    )

    # Lifecycle timestamps. `commission_started_at` = first paid charge of
    # the referred user, which kicks off the 12-month window.
    signed_up_at = fields.DatetimeField(auto_now_add=True)
    commission_started_at = fields.DatetimeField(null=True)
    commission_expires_at = fields.DatetimeField(null=True)

    # Running totals — accumulated by `record_commission_event` whenever
    # the referred user pays. Currency follows the referred user's
    # subscription currency.
    total_commission = fields.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0")
    )
    commission_currency = fields.CharField(max_length=3, default="USD")

    # Soft-paid bookkeeping. payout_id ties to whatever external payout
    # tool the admin uses (CSV export, Stripe Connect, manual bank
    # transfer). Null until paid.
    paid_out_at = fields.DatetimeField(null=True)
    paid_out_amount = fields.DecimalField(
        max_digits=14, decimal_places=2, null=True
    )
    payout_reference = fields.CharField(max_length=128, null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "referrals"
        ordering = ["-signed_up_at"]


class CommissionEvent(Model):
    """One row per accrued commission — paid invoice on the referred user."""
    id = fields.UUIDField(pk=True)
    referral_id = fields.UUIDField(index=True)
    # Source invoice on the referred user. Lets us audit "where did this
    # $3.80 commission come from?" later.
    invoice_id = fields.UUIDField(null=True)
    amount = fields.DecimalField(max_digits=14, decimal_places=2)
    currency = fields.CharField(max_length=3)
    # Multiplier applied at the time of the event — stored so the rate
    # can change in settings without rewriting historical commissions.
    rate = fields.DecimalField(max_digits=5, decimal_places=4)
    occurred_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "commission_events"
        ordering = ["-occurred_at"]

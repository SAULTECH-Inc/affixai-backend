"""Referral / commission business logic.

Three responsibilities, each as a single function so they can be unit-
tested without route plumbing:

  * `ensure_code(user)`            — lazily assign a referral_code if the
                                      user doesn't have one yet
  * `attribute_signup(...)`        — wire a new user to their referrer
                                      based on a `?ref=` code they had on
                                      the landing page
  * `record_commission_event(...)` — book a commission entry when the
                                      referred user pays an invoice

The commission rate + window are pulled from settings so they can be
tuned without code changes. Defaults:

  * 20% (`REFERRAL_COMMISSION_RATE = 0.20`)
  * 12 months (`REFERRAL_COMMISSION_MONTHS = 12`)
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from loguru import logger

from app.core.config import settings
from app.db.models.invoice import Invoice
from app.db.models.referral import CommissionEvent, Referral, ReferralStatus
from app.db.models.user import User


# ---- Code generation -------------------------------------------------------

_CODE_ALPHABET = string.ascii_lowercase + string.digits  # no l/1, no O/0 worries
_CODE_LENGTH = 8


def _generate_code() -> str:
    """8-char alphanumeric (lowercase + digits). 36^8 ≈ 2.8 trillion — safe
    against accidental collision, short enough for "affixai.com/?ref=ab12cd34".
    """
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


async def ensure_code(user: User) -> str:
    """Return the user's referral_code, generating one if missing.

    Idempotent: subsequent calls return the same code. Race-safe enough
    for normal usage — if two requests both find code=null and both
    generate, the second `save()` will fail uniqueness and we retry.
    """
    if user.referral_code:
        return user.referral_code

    # Up to 5 attempts to avoid the (astronomically unlikely) collision
    # tying up the request.
    for _ in range(5):
        candidate = _generate_code()
        try:
            user.referral_code = candidate
            await user.save(update_fields=["referral_code", "updated_at"])
            return candidate
        except Exception as exc:
            # IntegrityError on unique constraint OR a stale-row race —
            # refetch and try again.
            await user.refresh_from_db()
            if user.referral_code:
                return user.referral_code
            logger.warning(f"referral code collision for user {user.id}: {exc}")

    raise RuntimeError("could not generate a unique referral code after 5 tries")


# ---- Attribution -----------------------------------------------------------


async def attribute_signup(
    *, referred_user_id: UUID, code: str | None
) -> Referral | None:
    """Wire a newly-registered user to their referrer.

    Looks up the referrer by `code`. Returns the persisted Referral row,
    or None if the code is empty / unknown (we silently no-op — referrals
    are best-effort, never block signup).

    Self-referral guard: if the code resolves to the SAME user (which is
    impossible at signup time but defensive), we drop it.
    """
    if not code:
        return None
    referrer = await User.get_or_none(referral_code=code.strip().lower())
    if referrer is None:
        logger.info(f"referral code {code!r} not found — skipping attribution")
        return None
    if referrer.id == referred_user_id:
        return None

    # Idempotent: if a Referral row already exists (e.g. duplicate signup
    # webhook), reuse it.
    existing = await Referral.get_or_none(referred_user_id=referred_user_id)
    if existing:
        return existing

    return await Referral.create(
        referrer_user_id=referrer.id,
        referred_user_id=referred_user_id,
        code_used=code.strip().lower(),
        status=ReferralStatus.SIGNED_UP,
    )


# ---- Commission accrual ----------------------------------------------------


def _rate() -> Decimal:
    raw = getattr(settings, "REFERRAL_COMMISSION_RATE", 0.20)
    return Decimal(str(raw))


def _window_months() -> int:
    return int(getattr(settings, "REFERRAL_COMMISSION_MONTHS", 12))


async def record_commission_event(invoice: Invoice) -> CommissionEvent | None:
    """Called when a referred user's invoice transitions to PAID.

    Looks up the Referral by `invoice.user_id`. If found and within the
    commission window, books a CommissionEvent + updates running totals.
    Returns None if there's no referral, the user was self-referred (no
    fraud), the window has elapsed, or the invoice isn't paid.

    Caller: subscription webhook handler, AFTER it persists the invoice.
    Idempotent on invoice_id — re-running on the same invoice is a no-op.
    """
    if invoice.status != "paid":
        return None

    referral = await Referral.get_or_none(referred_user_id=invoice.user_id)
    if referral is None or referral.status == ReferralStatus.VOID:
        return None

    # First successful charge → starts the commission window.
    now = datetime.now(timezone.utc)
    if referral.commission_started_at is None:
        referral.commission_started_at = now
        # 12 months out, give-or-take a few hours from leap years; we don't
        # need second-level precision for a payout window.
        referral.commission_expires_at = now + timedelta(days=30 * _window_months())
        referral.status = ReferralStatus.CONVERTED

    # Window check.
    if referral.commission_expires_at and now > referral.commission_expires_at:
        if referral.status != ReferralStatus.EXPIRED:
            referral.status = ReferralStatus.EXPIRED
            await referral.save()
        return None

    # De-dupe: if we've already booked this invoice, bail.
    existing = await CommissionEvent.get_or_none(invoice_id=invoice.id)
    if existing is not None:
        return existing

    rate = _rate()
    amount = (invoice.amount * rate).quantize(Decimal("0.01"))

    event = await CommissionEvent.create(
        referral_id=referral.id,
        invoice_id=invoice.id,
        amount=amount,
        currency=invoice.currency,
        rate=rate,
    )

    # Mixed-currency totals are tricky — we keep the first currency we see
    # and just sum. If a user crosses currencies (e.g. moves country), the
    # admin export shows per-event currency for accurate payout.
    if (
        referral.total_commission == Decimal("0")
        or referral.commission_currency == invoice.currency
    ):
        referral.commission_currency = invoice.currency
        referral.total_commission = referral.total_commission + amount

    await referral.save()
    return event

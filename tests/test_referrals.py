"""Tests for the referral / affiliate system.

We pin down four classes of behavior:
  1. Code generation is unique + persistent (no churn between calls).
  2. Signup attribution links the right referrer; unknown codes don't fail.
  3. Commission accrues on paid invoices, idempotently.
  4. Commission window (12 months) closes correctly.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.common.services.referral_service import (
    attribute_signup,
    ensure_code,
    record_commission_event,
)
from app.db.models.invoice import Invoice, InvoiceStatus
from app.db.models.referral import CommissionEvent, Referral, ReferralStatus
from app.db.models.user import User, UserRole, UserStatus


# ---- Helpers ---------------------------------------------------------------


async def _make_user(email: str | None = None) -> User:
    return await User.create(
        email=email or f"u-{uuid.uuid4().hex[:8]}@example.com",
        password="x",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )


async def _paid_invoice(user_id: uuid.UUID, amount: Decimal = Decimal("19")) -> Invoice:
    """Minimal Invoice row representing a successful charge."""
    return await Invoice.create(
        user_id=user_id,
        provider="stripe",
        provider_invoice_id=f"in_{uuid.uuid4().hex[:16]}",
        amount=amount,
        currency="USD",
        status=InvoiceStatus.PAID,
        paid_at=datetime.now(timezone.utc),
    )


# ---- Code generation -------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_code_is_persistent(client):
    """Calling ensure_code twice returns the SAME code — not a fresh one."""
    user = await _make_user()
    first = await ensure_code(user)
    second = await ensure_code(user)
    assert first == second
    assert len(first) == 8


@pytest.mark.asyncio
async def test_codes_are_unique_across_users(client):
    """Two users get two different codes. Trivially likely given 36^8 space
    but worth pinning so a future "use email prefix" refactor can't break
    uniqueness silently."""
    a = await _make_user()
    b = await _make_user()
    code_a = await ensure_code(a)
    code_b = await ensure_code(b)
    assert code_a != code_b


# ---- Attribution -----------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_signup_links_referrer(client):
    """A new user signing up with a referrer's code creates a Referral row."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()

    referral = await attribute_signup(referred_user_id=referred.id, code=code)
    assert referral is not None
    assert referral.referrer_user_id == referrer.id
    assert referral.referred_user_id == referred.id
    assert referral.status == ReferralStatus.SIGNED_UP


@pytest.mark.asyncio
async def test_unknown_code_silently_drops(client):
    """Bad referral codes must not block signups — they no-op."""
    referred = await _make_user()
    result = await attribute_signup(
        referred_user_id=referred.id, code="zzzzzzzz"
    )
    assert result is None
    assert await Referral.filter(referred_user_id=referred.id).count() == 0


@pytest.mark.asyncio
async def test_empty_code_no_ops(client):
    """No referral code = no attribution. Same outcome as unknown code."""
    referred = await _make_user()
    assert await attribute_signup(referred_user_id=referred.id, code=None) is None
    assert await attribute_signup(referred_user_id=referred.id, code="") is None


@pytest.mark.asyncio
async def test_attribute_signup_is_idempotent(client):
    """Calling attribute twice for the same referred user reuses the row."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()

    first = await attribute_signup(referred_user_id=referred.id, code=code)
    second = await attribute_signup(referred_user_id=referred.id, code=code)
    assert first is not None and second is not None
    assert first.id == second.id


# ---- Commission accrual ----------------------------------------------------


@pytest.mark.asyncio
async def test_paid_invoice_accrues_commission(client):
    """Referred user pays $19 → 20% rate → $3.80 commission booked."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()
    await attribute_signup(referred_user_id=referred.id, code=code)

    inv = await _paid_invoice(referred.id, Decimal("19"))
    event = await record_commission_event(inv)

    assert event is not None
    assert event.amount == Decimal("3.80")
    assert event.currency == "USD"

    referral = await Referral.get(referred_user_id=referred.id)
    assert referral.status == ReferralStatus.CONVERTED
    assert referral.total_commission == Decimal("3.80")
    assert referral.commission_started_at is not None
    assert referral.commission_expires_at is not None


@pytest.mark.asyncio
async def test_commission_is_idempotent_per_invoice(client):
    """Re-running record_commission_event on the same invoice doesn't
    double-book. Critical because payment webhooks can deliver twice."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()
    await attribute_signup(referred_user_id=referred.id, code=code)

    inv = await _paid_invoice(referred.id, Decimal("19"))
    await record_commission_event(inv)
    await record_commission_event(inv)  # again
    await record_commission_event(inv)  # and again

    # Still only one event and one $3.80 total.
    assert await CommissionEvent.filter(invoice_id=inv.id).count() == 1
    referral = await Referral.get(referred_user_id=referred.id)
    assert referral.total_commission == Decimal("3.80")


@pytest.mark.asyncio
async def test_no_commission_for_user_with_no_referrer(client):
    """User signed up directly (no referral) → paid invoice → nothing
    accrues, no errors."""
    user = await _make_user()
    inv = await _paid_invoice(user.id)
    event = await record_commission_event(inv)
    assert event is None


@pytest.mark.asyncio
async def test_unpaid_invoice_does_not_accrue(client):
    """Pending / failed invoices must not generate commission events."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()
    await attribute_signup(referred_user_id=referred.id, code=code)

    pending = await Invoice.create(
        user_id=referred.id,
        provider="stripe",
        provider_invoice_id="in_pending",
        amount=Decimal("19"),
        currency="USD",
        status=InvoiceStatus.PENDING,
    )
    event = await record_commission_event(pending)
    assert event is None


@pytest.mark.asyncio
async def test_commission_window_expires_after_12_months(client):
    """Paid invoice after the 12-month window: status flips to EXPIRED, no
    new commission booked."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()
    await attribute_signup(referred_user_id=referred.id, code=code)

    # First charge starts the window.
    first = await _paid_invoice(referred.id)
    await record_commission_event(first)

    # Wind the clock back so the next "second" is past the 12-month window.
    referral = await Referral.get(referred_user_id=referred.id)
    referral.commission_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await referral.save()

    second = await _paid_invoice(referred.id, Decimal("19"))
    event = await record_commission_event(second)
    assert event is None

    await referral.refresh_from_db()
    assert referral.status == ReferralStatus.EXPIRED
    # Total stays at the original $3.80 — second charge contributed nothing.
    assert referral.total_commission == Decimal("3.80")


@pytest.mark.asyncio
async def test_void_referrals_dont_accrue(client):
    """Admin can mark a referral VOID (fraud / chargebacks). Paid charges
    after that don't add commission."""
    referrer = await _make_user()
    code = await ensure_code(referrer)
    referred = await _make_user()
    referral = await attribute_signup(referred_user_id=referred.id, code=code)
    assert referral is not None
    referral.status = ReferralStatus.VOID
    await referral.save()

    inv = await _paid_invoice(referred.id)
    event = await record_commission_event(inv)
    assert event is None

"""Tests for the country → payment-provider routing.

These pin down the business rules:
  * Nigeria        → Paystack
  * Africa (else)  → Flutterwave
  * Anything else  → Stripe
  * No country     → platform default (Stripe)

Plus regional pricing: Nigerian users see NGN, the rest see USD; the
amounts differ per region.
"""
from __future__ import annotations

import pytest

from app.common.services.payments.router import (
    REGIONS,
    price_for,
    provider_name_for_country,
    region_for_country,
)
from app.db.models.subscription import SubscriptionPlan


@pytest.mark.parametrize(
    "country,expected",
    [
        ("NG", "paystack"),
        ("ng", "paystack"),  # case-insensitive
        ("KE", "flutterwave"),
        ("ZA", "flutterwave"),
        ("GH", "flutterwave"),
        ("EG", "flutterwave"),  # North Africa included
        ("GB", "stripe"),
        ("US", "stripe"),
        ("DE", "stripe"),
        ("JP", "stripe"),
        ("AU", "stripe"),
        ("BR", "stripe"),
        # Unknown / missing → default. Override the platform default via
        # PAYMENT_PROVIDER if needed; the test env uses Stripe.
        (None, "stripe"),
        ("", "stripe"),
        ("ZZ", "stripe"),  # not a real country code
    ],
)
def test_provider_name_for_country(country, expected):
    assert provider_name_for_country(country) == expected


def test_nigeria_pricing_is_ngn():
    """Nigeria gets NGN-denominated pricing through Paystack."""
    price = price_for("NG", SubscriptionPlan.PRO)
    assert price is not None
    assert price.currency == "NGN"
    # Sanity-check the amount is in the local-price range (not the USD
    # range) — a NGN price under 1000 would be suspicious.
    assert price.amount > 1000


def test_kenya_pricing_is_usd_via_flutterwave():
    """Other African countries see USD pricing (Flutterwave converts at
    checkout)."""
    price = price_for("KE", SubscriptionPlan.PRO)
    assert price is not None
    assert price.currency == "USD"


def test_global_pricing_higher_than_african_pricing():
    """The global tier should be priced ABOVE the Africa tier — PPP-style.
    If this ever flips, someone has made a configuration mistake."""
    africa_pro = price_for("KE", SubscriptionPlan.PRO)
    global_pro = price_for("US", SubscriptionPlan.PRO)
    assert africa_pro is not None and global_pro is not None
    assert africa_pro.currency == global_pro.currency == "USD"
    assert global_pro.amount > africa_pro.amount


def test_region_for_country_returns_correct_provider_tag():
    """The Region object carries the provider name too — used by the
    /plans endpoint to pick the right price_id source."""
    assert region_for_country("NG").provider == "paystack"
    assert region_for_country("KE").provider == "flutterwave"
    assert region_for_country("FR").provider == "stripe"
    assert region_for_country(None).provider == "stripe"


def test_all_regions_have_pricing_for_paid_plans():
    """If we add a new plan to SubscriptionPlan, this test reminds us to
    add a price for it in every region — otherwise the upgrade button
    would show '$0' or break."""
    paid_plans = [SubscriptionPlan.PRO, SubscriptionPlan.ENTERPRISE]
    for region in REGIONS.values():
        for plan in paid_plans:
            assert plan in region.pricing, (
                f"region {region.code!r} is missing pricing for {plan.value}"
            )

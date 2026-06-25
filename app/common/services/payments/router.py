"""Country-aware payment provider + pricing resolution.

The platform supports three gateways:
  * Paystack    — best for Nigeria (NGN cards + bank transfer + USSD)
  * Flutterwave — best across the rest of Africa (Kenya, Ghana, South
                  Africa, Uganda, Tanzania, Rwanda, etc.)
  * Stripe      — best for Europe, Americas, Asia-Pacific

`PAYMENT_PROVIDER` from settings still acts as the global default — used
when a user has no country attached (legacy accounts, server-to-server
flows). For authenticated users we route per `user.country_code`.

Pricing is region-tiered. Africa gets a meaningfully lower price point
than the global default — the per-capita gap is large enough that a flat
USD price would price the product out of the market. This is encoded as
a simple lookup; the per-provider price IDs (Stripe price_xxx, Paystack
plan PLN_xxx, Flutterwave numeric plan id) still come from the .env so
the rates can be edited without a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.config import settings
from app.db.models.subscription import SubscriptionPlan

from . import get_provider as _get_provider
from .base import PaymentProvider

if TYPE_CHECKING:
    from app.db.models.user import User


# ---- Country → provider ----------------------------------------------------
#
# Mapping is explicit rather than "Africa = X" so we can grandfather any
# country into a different provider if the partner relationship changes.
# Anything not listed defaults to Stripe (the broadest international coverage).

PAYSTACK_COUNTRIES = {"NG"}  # Nigeria — Paystack home market

FLUTTERWAVE_COUNTRIES = {
    # Anglophone West Africa
    "GH",  # Ghana
    "LR",  # Liberia
    "SL",  # Sierra Leone
    "GM",  # Gambia
    # East Africa
    "KE",  # Kenya
    "UG",  # Uganda
    "TZ",  # Tanzania
    "RW",  # Rwanda
    "BI",  # Burundi
    "ET",  # Ethiopia
    # Southern Africa
    "ZA",  # South Africa
    "ZM",  # Zambia
    "ZW",  # Zimbabwe
    "MW",  # Malawi
    "MZ",  # Mozambique
    "BW",  # Botswana
    "NA",  # Namibia
    "LS",  # Lesotho
    "SZ",  # Eswatini
    # Central Africa
    "CM",  # Cameroon
    "CD",  # DRC
    "CG",  # Congo
    "GA",  # Gabon
    "TD",  # Chad
    # Francophone West Africa
    "CI",  # Cote d'Ivoire
    "SN",  # Senegal
    "BJ",  # Benin
    "BF",  # Burkina Faso
    "ML",  # Mali
    "NE",  # Niger
    "TG",  # Togo
    "GN",  # Guinea
    # North Africa (Flutterwave coverage is partial here but better than
    # Paystack; tune as the relationship evolves)
    "EG",  # Egypt
    "MA",  # Morocco
    "DZ",  # Algeria
    "TN",  # Tunisia
}


def provider_name_for_country(country_code: str | None) -> str:
    """Return the gateway name we'd route this country through.

    Falls back to the platform default (`settings.PAYMENT_PROVIDER`) when
    the country is missing or unknown.
    """
    if not country_code:
        return (settings.PAYMENT_PROVIDER or "stripe").lower()
    cc = country_code.upper()
    if cc in PAYSTACK_COUNTRIES:
        return "paystack"
    if cc in FLUTTERWAVE_COUNTRIES:
        return "flutterwave"
    return "stripe"


def provider_for_country(country_code: str | None) -> PaymentProvider:
    """Resolve a PaymentProvider instance for this country."""
    return _get_provider(provider_name_for_country(country_code))


def provider_for_user(user: "User | None") -> PaymentProvider:
    """Resolve a provider for a user's preferred region."""
    cc = getattr(user, "country_code", None) if user else None
    return provider_for_country(cc)


# ---- Region pricing --------------------------------------------------------
#
# Three tiers: NG (Paystack, NGN), Africa-ex-NG (Flutterwave, USD by
# default), Default (Stripe, USD). Adjust the amounts to taste — values
# kept centralized so it's a one-line change to roll out PPP pricing.
#
# `price_id_env` names map back to the env var that holds the gateway's
# native price/plan identifier — Stripe price_xxx, Paystack PLN_xxx,
# Flutterwave numeric plan id. The PROVIDER class still reads from
# settings under those names, so this module doesn't duplicate the lookup.


@dataclass(frozen=True)
class RegionPrice:
    amount: Decimal
    currency: str  # ISO 4217


@dataclass(frozen=True)
class Region:
    code: str         # short tag for logging / debugging
    provider: str
    pricing: dict[SubscriptionPlan, RegionPrice]


_NGN = "NGN"
_USD = "USD"


REGIONS: dict[str, Region] = {
    "nigeria": Region(
        code="ng",
        provider="paystack",
        pricing={
            # ~NGN 7,500 ≈ $5 at June-2026 exchange (illustrative)
            SubscriptionPlan.PRO: RegionPrice(Decimal("7500"), _NGN),
            SubscriptionPlan.ENTERPRISE: RegionPrice(Decimal("70000"), _NGN),
        },
    ),
    "africa": Region(
        code="afr",
        provider="flutterwave",
        pricing={
            # Local-priced via Flutterwave but quoted in USD on the
            # landing page; FW handles the local-currency conversion at
            # checkout time.
            SubscriptionPlan.PRO: RegionPrice(Decimal("8"), _USD),
            SubscriptionPlan.ENTERPRISE: RegionPrice(Decimal("50"), _USD),
        },
    ),
    "global": Region(
        code="glb",
        provider="stripe",
        pricing={
            SubscriptionPlan.PRO: RegionPrice(Decimal("19"), _USD),
            SubscriptionPlan.ENTERPRISE: RegionPrice(Decimal("99"), _USD),
        },
    ),
}


def region_for_country(country_code: str | None) -> Region:
    """Return the Region the user falls into based on their country."""
    if not country_code:
        return REGIONS["global"]
    cc = country_code.upper()
    if cc in PAYSTACK_COUNTRIES:
        return REGIONS["nigeria"]
    if cc in FLUTTERWAVE_COUNTRIES:
        return REGIONS["africa"]
    return REGIONS["global"]


def price_for(country_code: str | None, plan: SubscriptionPlan) -> RegionPrice | None:
    region = region_for_country(country_code)
    return region.pricing.get(plan)

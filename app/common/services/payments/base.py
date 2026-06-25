"""Common interface every payment provider must implement.

Each provider returns its own native objects opaquely — we just need URLs to
redirect the browser to (checkout, customer portal) and a normalized form of
incoming webhook events so the route handlers don't care which provider sent
them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.db.models.subscription import SubscriptionPlan


class ProviderError(Exception):
    """Wrapper around any gateway-specific error. Routes catch this and
    return a 502 / 503 to the caller without leaking SDK internals.
    """


@dataclass
class CheckoutResult:
    checkout_url: str
    provider: str
    # Some providers (Stripe) create a Customer up-front; others (Paystack,
    # Flutterwave) attach the customer to the transaction itself. We capture
    # the customer-identifier the provider gave us so we can store it on
    # Subscription for reuse on the next upgrade.
    customer_id: str | None = None


@dataclass
class PortalResult:
    portal_url: str


WebhookEventKind = Literal[
    "subscription.created",
    "subscription.updated",
    "subscription.canceled",
    "invoice.paid",
    "invoice.failed",
    "unknown",
]


@dataclass
class NormalizedInvoice:
    """The subset of fields we persist on the Invoice table.

    Providers vary wildly in their event shapes; each provider's webhook
    handler is responsible for normalizing to this dataclass so the
    persistence layer is single-code-path.
    """
    provider: str
    provider_invoice_id: str
    amount: Decimal
    currency: str
    status: Literal["pending", "paid", "failed", "refunded"]
    description: str | None = None
    hosted_url: str | None = None
    pdf_url: str | None = None
    paid_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Used to find the User this invoice belongs to. Providers attach this
    # to the customer/transaction as our internal user_id during checkout.
    user_id: str | None = None


@dataclass
class NormalizedSubscription:
    """What we'll merge into our local Subscription row on subscription.*
    webhook events.
    """
    provider: str
    provider_subscription_id: str
    provider_customer_id: str | None
    plan: SubscriptionPlan | None
    status: str  # raw provider status string ("active"/"trialing"/etc.)
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_ends_at: datetime | None = None
    cancel_at_period_end: bool = False
    user_id: str | None = None


@dataclass
class NormalizedEvent:
    """A parsed + verified webhook event ready for the persistence layer."""
    kind: WebhookEventKind
    event_id: str
    subscription: NormalizedSubscription | None = None
    invoice: NormalizedInvoice | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    """Provider-agnostic billing operations the routes call into."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True iff this provider has the keys it needs to talk to its
        gateway. Used by routes to return a clean 503 when admins point
        PAYMENT_PROVIDER at a provider whose env block is empty.
        """

    @abstractmethod
    def price_id_for(self, plan: SubscriptionPlan) -> str | None:
        """Provider-native identifier for a plan (Stripe price_id, Paystack
        plan_code, Flutterwave plan_id). Returns None when unconfigured.
        """

    @abstractmethod
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
        """Open a checkout / payment page and return its URL."""

    @abstractmethod
    async def open_portal(
        self,
        *,
        customer_id: str,
        return_url: str,
    ) -> PortalResult:
        """Open a customer self-service portal (subscription mgmt, card update).

        Not every provider has a hosted portal — Stripe does, Paystack/Flw don't.
        Stubs may raise ProviderError("not supported"); routes should surface
        that as a friendly "manage via the provider dashboard" message.
        """

    @abstractmethod
    def verify_and_parse_webhook(
        self, *, body: bytes, headers: dict[str, str]
    ) -> NormalizedEvent:
        """Verify the gateway signature and return a normalized event.

        Raises ProviderError on signature failure.
        """

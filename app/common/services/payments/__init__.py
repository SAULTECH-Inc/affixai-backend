"""Payment provider abstraction.

We support Stripe (default), Paystack, and Flutterwave. The active provider
is selected by `settings.PAYMENT_PROVIDER` and exposed through `get_provider()`.

Routes should NEVER import a provider implementation directly — go through
`get_provider()` so swapping providers is one env-var change away.
"""
from __future__ import annotations

from app.core.config import settings

from .base import PaymentProvider, ProviderError
from .flutterwave_provider import FlutterwaveProvider
from .paystack_provider import PaystackProvider
from .stripe_provider import StripeProvider


_PROVIDERS: dict[str, type[PaymentProvider]] = {
    "stripe": StripeProvider,
    "paystack": PaystackProvider,
    "flutterwave": FlutterwaveProvider,
}


def get_provider(name: str | None = None) -> PaymentProvider:
    """Return an instance of the active provider.

    If `name` is given, returns that specific provider (useful for webhook
    routes which are per-provider). Otherwise returns the active default from
    `PAYMENT_PROVIDER`.
    """
    key = (name or settings.PAYMENT_PROVIDER or "stripe").lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ProviderError(f"Unknown payment provider: {key!r}")
    return cls()


def active_provider_name() -> str:
    return (settings.PAYMENT_PROVIDER or "stripe").lower()


__all__ = [
    "PaymentProvider",
    "ProviderError",
    "get_provider",
    "active_provider_name",
]

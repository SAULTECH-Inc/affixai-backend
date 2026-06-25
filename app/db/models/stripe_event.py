"""Tracks processed Stripe webhook events so retries don't double-apply."""
from tortoise import fields
from tortoise.models import Model


class StripeEvent(Model):
    """Idempotency log for Stripe webhooks.

    Stripe retries on non-2xx responses. Without this guard, a payment-paid
    event could be applied twice if a transient error happened between the
    sync and the response. We insert on first sight and short-circuit if the
    event_id is already present.
    """
    event_id = fields.CharField(pk=True, max_length=128)
    event_type = fields.CharField(max_length=64)
    received_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "stripe_events"

"""Subscription model — one row per user tracking trial + Stripe state."""
from enum import Enum

from tortoise import fields
from tortoise.models import Model


class SubscriptionStatus(str, Enum):
    TRIALING = "trialing"        # in the 30-day free trial
    ACTIVE = "active"            # paid, current
    PAST_DUE = "past_due"        # payment failed, grace period
    CANCELED = "canceled"        # user-initiated cancel
    EXPIRED = "expired"          # trial ended + free tier disabled + no paid sub
    INCOMPLETE = "incomplete"    # checkout started, not yet confirmed by webhook


class SubscriptionPlan(str, Enum):
    TRIAL = "trial"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class Subscription(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(unique=True, index=True)  # one subscription per user

    # Which gateway this subscription was created on. NULL for trial-only
    # rows that never went through checkout. Possible values:
    # "stripe" | "paystack" | "flutterwave".
    provider = fields.CharField(max_length=32, null=True)

    # Provider-native identifiers. We keep the original `stripe_*` columns
    # for backwards compatibility (they're already populated for existing
    # users) — the new generic `provider_*` columns mirror them and are
    # what new code should read.
    stripe_customer_id = fields.CharField(max_length=128, null=True, index=True)
    stripe_subscription_id = fields.CharField(max_length=128, null=True, index=True)
    stripe_price_id = fields.CharField(max_length=128, null=True)
    provider_customer_id = fields.CharField(max_length=128, null=True, index=True)
    provider_subscription_id = fields.CharField(max_length=128, null=True, index=True)

    plan = fields.CharEnumField(
        SubscriptionPlan, max_length=32, default=SubscriptionPlan.TRIAL
    )
    status = fields.CharEnumField(
        SubscriptionStatus, max_length=32, default=SubscriptionStatus.TRIALING
    )

    trial_ends_at = fields.DatetimeField(null=True)
    current_period_start = fields.DatetimeField(null=True)
    current_period_end = fields.DatetimeField(null=True)
    cancel_at_period_end = fields.BooleanField(default=False)
    canceled_at = fields.DatetimeField(null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "subscriptions"

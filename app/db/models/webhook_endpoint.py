"""User-configured outgoing webhooks.

Users register URLs that should receive HTTP POST notifications when events
they care about happen on their account — typical "become infrastructure"
plumbing fintechs want.

Each row is one (user OR enterprise, URL, events-filter) tuple. Outbound
requests are signed with the row's `secret` via HMAC-SHA256 so receivers
can verify authenticity without us shipping secrets in headers.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class WebhookEndpointStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"      # owner-paused (e.g. while debugging their receiver)
    DISABLED = "disabled"  # auto-disabled after N consecutive failures


class WebhookEndpoint(Model):
    id = fields.UUIDField(pk=True)

    # Exactly one of user_id / enterprise_id is set. Per-user endpoints get
    # events for that user's activity; per-enterprise endpoints get events
    # for any member's activity in that org.
    user_id = fields.UUIDField(null=True, index=True)
    enterprise_id = fields.UUIDField(null=True, index=True)

    # The URL we POST to. https only enforced in the route handler.
    url = fields.CharField(max_length=512)

    # JSON list of event types this endpoint subscribes to. Empty list = all.
    # See WebhookEventType in webhook_events.py for the catalog.
    events = fields.JSONField(null=True)

    # HMAC-SHA256 key. Generated at create time; shown ONCE on the create
    # response (like API keys). Receivers verify each request via the
    # X-AffixAI-Signature header.
    secret = fields.CharField(max_length=128)

    # Human label for the management UI ("Production receiver", "Slack
    # bridge", etc.).
    name = fields.CharField(max_length=120, null=True)

    status = fields.CharEnumField(
        WebhookEndpointStatus, max_length=16, default=WebhookEndpointStatus.ACTIVE
    )

    # Delivery stats — useful for the management UI to surface unhealthy
    # endpoints (lots of failures → "needs attention" badge).
    delivery_attempts = fields.IntField(default=0)
    delivery_successes = fields.IntField(default=0)
    delivery_failures = fields.IntField(default=0)
    consecutive_failures = fields.IntField(default=0)
    last_success_at = fields.DatetimeField(null=True)
    last_failure_at = fields.DatetimeField(null=True)
    last_failure_reason = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "webhook_endpoints"

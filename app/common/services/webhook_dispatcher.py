"""Outgoing webhook dispatcher.

Other parts of the system call `dispatch_event(event_type, payload, user_id)`
when something notable happens (a document gets signed, a participant
declines, etc.). This module:

  1. Finds every active WebhookEndpoint matching the user / enterprise scope
     AND subscribed to that event (or to all events).
  2. Builds a signed JSON envelope.
  3. POSTs to each endpoint with a short timeout, fire-and-forget.
  4. Records success / failure counters back on the endpoint row.

Why fire-and-forget: webhook receivers can be slow, and we don't want the
caller (a signing route handler, say) to block on remote network I/O. We
schedule the deliveries as background tasks on the asyncio loop.

Auto-disable: after `_MAX_CONSECUTIVE_FAILURES` consecutive failures, the
endpoint flips to DISABLED so we stop hammering a dead URL. The owner can
re-enable it from the management UI after fixing their receiver.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

import httpx
from loguru import logger

from app.db.models.webhook_endpoint import WebhookEndpoint, WebhookEndpointStatus
from app.db.models.user import User


class WebhookEventType(str, Enum):
    """Catalog of events we emit. Adding a new one is just: add to this
    enum + call `dispatch_event` from wherever the trigger lives."""
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_AUTO_SIGNED = "document.auto_signed"
    DOCUMENT_SIGNED = "document.signed"
    DOCUMENT_SHARED = "document.shared"
    DOCUMENT_COMPLETED = "document.completed"
    DOCUMENT_DECLINED = "document.declined"
    DOCUMENT_EXPIRED = "document.expired"
    DOCUMENT_VOIDED = "document.voided"
    PARTICIPANT_SIGNED = "participant.signed"
    PARTICIPANT_DECLINED = "participant.declined"
    PARTICIPANT_VIEWED = "participant.viewed"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    SUBSCRIPTION_CANCELED = "subscription.canceled"


_MAX_CONSECUTIVE_FAILURES = 10
_DELIVERY_TIMEOUT_SECONDS = 8.0

# Retry schedule (seconds). Each delivery makes up to len(_RETRY_DELAYS) + 1
# total attempts; on the first 5xx / network error we wait the first delay
# and try again. Capped at ~10 minutes total so the background task doesn't
# linger indefinitely. For more durable retries (server restart, long
# back-offs), graduate to a Redis-backed queue.
_RETRY_DELAYS = (5.0, 30.0, 120.0)  # 3 retries → 4 total attempts


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    """HMAC-SHA256 over `{timestamp}.{body}`.

    Stripe-style: timestamp included in the signed payload so a captured
    body can't be replayed days later. Receivers should reject signatures
    older than 5 minutes.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    )
    return mac.hexdigest()


async def _endpoints_for(
    *, user_id: UUID | None, enterprise_id: UUID | None,
) -> list[WebhookEndpoint]:
    """Active endpoints that match the actor's scope.

    A user's event reaches:
      * their own per-user endpoints
      * their enterprise's endpoints, if they belong to one
    """
    from tortoise.expressions import Q

    scope = Q()
    if user_id:
        scope |= Q(user_id=user_id)
    if enterprise_id:
        scope |= Q(enterprise_id=enterprise_id)
    # If neither scope is provided we have nothing to match — return empty.
    if user_id is None and enterprise_id is None:
        return []
    return await WebhookEndpoint.filter(
        scope,
        status=WebhookEndpointStatus.ACTIVE,
        deleted_at=None,
    )


def _is_subscribed(endpoint: WebhookEndpoint, event_type: str) -> bool:
    """Match the event against the endpoint's filter.

    Empty / null filter means "all events" (Stripe-style). Otherwise the
    event_type must appear verbatim in the list — we don't currently support
    wildcards like `document.*`, but the filter is JSON so adding them later
    is a one-line change.
    """
    if not endpoint.events:
        return True
    if not isinstance(endpoint.events, list):
        return True
    return event_type in endpoint.events


async def dispatch_event(
    event_type: WebhookEventType,
    payload: dict[str, Any],
    *,
    user_id: UUID | None = None,
    enterprise_id: UUID | None = None,
) -> None:
    """Fire-and-forget delivery.

    Call from any service that wants to notify users — auto-signed,
    completed, etc. We resolve the actor's enterprise lazily if you only
    pass `user_id`, so callers don't need to denormalize.
    """
    # Resolve enterprise_id from user_id if not given.
    if user_id and enterprise_id is None:
        u = await User.get_or_none(id=user_id, deleted_at=None)
        if u:
            enterprise_id = u.enterprise_id

    endpoints = await _endpoints_for(
        user_id=user_id, enterprise_id=enterprise_id
    )
    if not endpoints:
        return
    matching = [e for e in endpoints if _is_subscribed(e, event_type.value)]
    if not matching:
        return

    envelope = {
        "id": payload.get("id") or _random_event_id(),
        "type": event_type.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }
    body = json.dumps(envelope, default=str).encode("utf-8")

    # Schedule one task per endpoint so a slow receiver doesn't block the
    # others. The caller doesn't await them.
    for ep in matching:
        asyncio.create_task(_deliver(ep, body, event_type.value))


def _random_event_id() -> str:
    """Stable-prefixed random id so receivers can dedupe retries cleanly."""
    import secrets
    return f"evt_{secrets.token_urlsafe(16)}"


async def _deliver(
    endpoint: WebhookEndpoint, body: bytes, event_type: str
) -> None:
    """Deliver one event with retries on transient failures.

    Retry policy:
      - 4xx (other than 408/429) → permanent failure, no retry
        (the receiver rejected the payload; resending won't help)
      - 408/429/5xx/network errors → retry with the backoff schedule above
      - Success → exit early

    We sign each attempt with a FRESH timestamp so the receiver's clock-skew
    tolerance applies per-attempt rather than to the original event time.
    The event payload (including its id) stays the same — receivers should
    dedupe by the event id from the body.
    """
    endpoint.delivery_attempts += 1
    ok = False
    err: str | None = None
    last_status: int | None = None
    attempts_made = 0

    for attempt_idx in range(len(_RETRY_DELAYS) + 1):
        attempts_made = attempt_idx + 1
        ts = int(time.time())
        signature = _sign(endpoint.secret, body, ts)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AffixAI-Webhooks/1.0",
            "X-AffixAI-Event": event_type,
            "X-AffixAI-Timestamp": str(ts),
            "X-AffixAI-Signature": f"sha256={signature}",
            # Tell receivers which attempt this is so they can debug retries.
            "X-AffixAI-Delivery-Attempt": str(attempts_made),
        }

        try:
            async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
                r = await client.post(endpoint.url, content=body, headers=headers)
            last_status = r.status_code
            if 200 <= r.status_code < 300:
                ok = True
                err = None
                break
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            # 4xx (other than 408/429) is the receiver explicitly rejecting
            # this payload — retrying won't change the answer. Bail.
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                break
        except httpx.HTTPError as exc:
            err = f"Network error: {exc}"
        except Exception as exc:
            err = f"Unexpected: {exc}"

        # Sleep before the next attempt (if there is one).
        if attempt_idx < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt_idx])

    if ok:
        endpoint.delivery_successes += 1
        endpoint.consecutive_failures = 0
        endpoint.last_success_at = datetime.now(timezone.utc)
        endpoint.last_failure_reason = None
        if attempts_made > 1:
            logger.info(
                f"webhook to {endpoint.url} succeeded on attempt {attempts_made}"
            )
    else:
        endpoint.delivery_failures += 1
        endpoint.consecutive_failures += 1
        endpoint.last_failure_at = datetime.now(timezone.utc)
        endpoint.last_failure_reason = (
            f"{err} (after {attempts_made} attempt{'s' if attempts_made != 1 else ''})"
        )
        if endpoint.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            endpoint.status = WebhookEndpointStatus.DISABLED
            logger.warning(
                f"webhook endpoint {endpoint.id} auto-DISABLED after "
                f"{endpoint.consecutive_failures} failures"
            )
        logger.warning(
            f"webhook delivery failed for {endpoint.url} "
            f"(attempts={attempts_made}, last_status={last_status}): {err}"
        )
    try:
        await endpoint.save()
    except Exception as exc:
        logger.warning(f"could not record webhook stats for {endpoint.id}: {exc}")


# ---- Convenience for "test fire" from the management UI -------------------


async def test_endpoint(endpoint: WebhookEndpoint) -> dict[str, Any]:
    """Send a synthetic event NOW (synchronously awaited) so the user can
    debug their receiver right after setup. Returns a small result dict."""
    body = json.dumps({
        "id": _random_event_id(),
        "type": "test.ping",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": {"message": "If you can read this, the integration works!"},
    }, default=str).encode("utf-8")

    ts = int(time.time())
    sig = _sign(endpoint.secret, body, ts)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AffixAI-Webhooks/1.0",
        "X-AffixAI-Event": "test.ping",
        "X-AffixAI-Timestamp": str(ts),
        "X-AffixAI-Signature": f"sha256={sig}",
    }
    try:
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
            r = await client.post(endpoint.url, content=body, headers=headers)
        return {
            "ok": 200 <= r.status_code < 300,
            "status_code": r.status_code,
            "body": r.text[:500],
        }
    except Exception as exc:
        return {"ok": False, "status_code": 0, "body": str(exc)}


__all__ = [
    "WebhookEventType",
    "dispatch_event",
    "test_endpoint",
]

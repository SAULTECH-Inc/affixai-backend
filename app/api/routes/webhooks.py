"""CRUD for user-configured outgoing webhooks.

Mounted at /api/v1/webhooks. Per-user OR per-enterprise (admin-only); the
list endpoint returns whichever scope the caller can see.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from tortoise.expressions import Q

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.webhook_dispatcher import (
    WebhookEventType,
    test_endpoint as run_test_endpoint,
)
from app.db.models.audit_log import AuditAction
from app.db.models.user import User, UserRole
from app.db.models.webhook_endpoint import (
    WebhookEndpoint,
    WebhookEndpointStatus,
)
from app.models.auth_schemas import MessageOut
from app.models.webhook_schemas import (
    CreateWebhookDto,
    TestPingResultOut,
    UpdateWebhookDto,
    WebhookCreatedOut,
    WebhookOut,
)

router = APIRouter()


def _is_enterprise_admin(user: User) -> bool:
    return user.role in (UserRole.ENTERPRISE_ADMIN, UserRole.SUPER_ADMIN)


def _out(ep: WebhookEndpoint) -> WebhookOut:
    return WebhookOut(
        id=ep.id,
        user_id=ep.user_id,
        enterprise_id=ep.enterprise_id,
        url=ep.url,
        name=ep.name,
        events=ep.events,
        status=ep.status,
        delivery_attempts=ep.delivery_attempts,
        delivery_successes=ep.delivery_successes,
        delivery_failures=ep.delivery_failures,
        consecutive_failures=ep.consecutive_failures,
        last_success_at=ep.last_success_at,
        last_failure_at=ep.last_failure_at,
        last_failure_reason=ep.last_failure_reason,
        created_at=ep.created_at,
    )


def _validate_events(events: list[str]) -> None:
    """Reject typos at create time — saves debugging later."""
    valid = {e.value for e in WebhookEventType}
    bad = [e for e in events if e not in valid]
    if bad:
        raise HTTPException(
            400,
            f"Unknown event types: {bad}. Valid: {sorted(valid)}",
        )


@router.get("", response_model=list[WebhookOut])
async def list_webhooks(
    user: User = Depends(get_current_user),
) -> list[WebhookOut]:
    """Returns the user's own endpoints + enterprise-scope ones they can see.

    Non-admin members of an enterprise see (read-only) enterprise endpoints
    too so they understand what their org has configured.
    """
    scope = Q(user_id=user.id)
    if user.enterprise_id:
        scope |= Q(enterprise_id=user.enterprise_id)
    rows = await WebhookEndpoint.filter(scope, deleted_at=None).order_by(
        "-created_at"
    )
    return [_out(r) for r in rows]


@router.post("", response_model=WebhookCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: CreateWebhookDto, user: User = Depends(get_current_user)
) -> WebhookCreatedOut:
    url_str = str(payload.url)
    if not url_str.startswith("https://"):
        # HTTPS-only — never POST secrets to plaintext endpoints.
        raise HTTPException(
            400,
            "Webhook URL must use https:// — http endpoints aren't allowed",
        )
    _validate_events(payload.events)

    # Resolve scope.
    user_id: UUID | None = user.id
    enterprise_id: UUID | None = None
    if payload.scope == "enterprise":
        if not user.enterprise_id:
            raise HTTPException(
                400,
                "You're not part of an enterprise — cannot create enterprise webhook",
            )
        if not _is_enterprise_admin(user):
            raise HTTPException(
                403,
                "Only enterprise admins can create enterprise-scope webhooks",
            )
        user_id = None
        enterprise_id = user.enterprise_id

    secret = f"whsec_{secrets.token_urlsafe(40)}"
    ep = await WebhookEndpoint.create(
        user_id=user_id,
        enterprise_id=enterprise_id,
        url=url_str,
        name=payload.name,
        events=payload.events or None,
        secret=secret,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="webhook",
        entity_id=str(ep.id),
        description=f"Webhook created → {url_str}",
        metadata={"scope": payload.scope, "events": payload.events},
    )
    return WebhookCreatedOut(**_out(ep).model_dump(), secret=secret)


@router.put("/{endpoint_id}", response_model=WebhookOut)
async def update_webhook(
    endpoint_id: UUID,
    payload: UpdateWebhookDto,
    user: User = Depends(get_current_user),
) -> WebhookOut:
    ep = await _resolve_writable(endpoint_id, user)
    if payload.url:
        if not str(payload.url).startswith("https://"):
            raise HTTPException(400, "Webhook URL must use https://")
        ep.url = str(payload.url)
    if payload.name is not None:
        ep.name = payload.name
    if payload.events is not None:
        _validate_events(payload.events)
        ep.events = payload.events or None
    if payload.status is not None:
        ep.status = payload.status
        if payload.status == WebhookEndpointStatus.ACTIVE:
            # Re-enabling resets the consecutive-failure counter so we don't
            # immediately re-disable on the next blip.
            ep.consecutive_failures = 0
    await ep.save()
    return _out(ep)


@router.delete("/{endpoint_id}", response_model=MessageOut)
async def delete_webhook(
    endpoint_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    ep = await _resolve_writable(endpoint_id, user)
    ep.deleted_at = datetime.now(timezone.utc)
    await ep.save()
    return MessageOut(message="Webhook deleted")


@router.post(
    "/{endpoint_id}/test", response_model=TestPingResultOut
)
async def test_webhook(
    endpoint_id: UUID, user: User = Depends(get_current_user)
) -> TestPingResultOut:
    """Send a synthetic test event to the endpoint NOW. Useful for the user
    to verify their receiver is wired up correctly before relying on real
    events to fire."""
    ep = await _resolve_writable(endpoint_id, user)
    result = await run_test_endpoint(ep)
    return TestPingResultOut(**result)


@router.get("/event-types", response_model=list[str])
async def list_event_types() -> list[str]:
    """Catalog of supported event type strings. UI uses this to populate
    the subscription filter checkboxes."""
    return [e.value for e in WebhookEventType]


async def _resolve_writable(endpoint_id: UUID, user: User) -> WebhookEndpoint:
    """Load + permission-check an endpoint for a write operation.

    Per-user endpoints: only the owner can mutate.
    Per-enterprise endpoints: enterprise admins of the same org only.
    """
    ep = await WebhookEndpoint.get_or_none(id=endpoint_id, deleted_at=None)
    if not ep:
        raise HTTPException(404, "Webhook not found")
    if ep.user_id and ep.user_id == user.id:
        return ep
    if ep.enterprise_id and ep.enterprise_id == user.enterprise_id and _is_enterprise_admin(user):
        return ep
    raise HTTPException(403, "Not allowed to modify this webhook")

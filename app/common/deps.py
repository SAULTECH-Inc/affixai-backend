"""Shared FastAPI dependencies.

- `get_current_user` resolves the JWT and returns the active User row.
- `get_current_enterprise` validates an `X-API-Key` and returns the enterprise.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.common.services.subscription_service import (
    ensure_subscription,
    user_can_access_paid_features,
)
from app.core.security import get_current_user_id
from app.db.models.api_key import ApiKey, ApiKeyStatus
from app.db.models.enterprise import Enterprise
from app.db.models.user import User, UserRole, UserStatus


async def get_current_user(user_id: str = Depends(get_current_user_id)) -> User:
    try:
        uid = UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        ) from exc

    user = await User.get_or_none(id=uid, deleted_at=None)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.status == UserStatus.SUSPENDED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")
    return user


public_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_enterprise(
    request: Request,
    api_key: str | None = Security(public_api_key_header),
) -> tuple[Enterprise, ApiKey]:
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required")

    hashed = hashlib.sha256(api_key.encode()).hexdigest()
    record = await ApiKey.get_or_none(key=hashed, deleted_at=None).prefetch_related("enterprise")
    if not record or record.status != ApiKeyStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if record.expires_at and record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")

    if record.ip_whitelist:
        client_ip = request.client.host if request.client else None
        if client_ip and client_ip not in record.ip_whitelist:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed")

    record.usage_count += 1
    record.last_used_at = datetime.now(timezone.utc)
    await record.save(update_fields=["usage_count", "last_used_at"])

    enterprise = await record.enterprise  # type: ignore[assignment]
    return enterprise, record


async def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Gate routes that only the platform owner should reach (manual trial
    extensions, refunds, support tooling). Returns the user unchanged on
    success; raises 403 otherwise.
    """
    if user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin role required",
        )
    return user


async def require_active_subscription(user: User = Depends(get_current_user)) -> User:
    """Block access if the user's subscription can't access paid features.

    Returns the user unchanged on success; raises 402 Payment Required with the
    subscription state in the body so the frontend can route to checkout.

    Enterprise users (those with `enterprise_id` set) bypass this — they're
    billed through their organization (Phase 6 governs that).
    """
    if user.enterprise_id is not None:
        return user

    sub = await ensure_subscription(user)
    if user_can_access_paid_features(sub):
        return user

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": "subscription_required",
            "status": sub.status.value,
            "plan": sub.plan.value,
            "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        },
    )

"""Enterprise + API key management routes."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.core.encryption import sha256_hex
from app.db.models.api_key import ApiKey, ApiKeyStatus, ApiKeyType
from app.db.models.audit_log import AuditAction
from app.db.models.document import Document, DocumentStatus
from app.db.models.enterprise import Enterprise
from app.db.models.subscription import Subscription, SubscriptionStatus
from app.db.models.user import User, UserRole
from app.models.auth_schemas import MessageOut
from app.models.enterprise_schemas import (
    ApiKeyOut,
    CreateApiKeyDto,
    CreateApiKeyOut,
    CreateEnterpriseDto,
    EnterpriseDocumentOut,
    EnterpriseOut,
    EnterpriseStatsOut,
    UpdateEnterpriseDto,
)

router = APIRouter()


def _api_key_out(record: ApiKey, enterprise_id: UUID) -> ApiKeyOut:
    return ApiKeyOut(
        id=record.id,
        enterprise_id=enterprise_id,
        name=record.name,
        description=record.description,
        key_type=record.key_type,
        status=record.status,
        permissions=record.permissions,
        ip_whitelist=record.ip_whitelist,
        usage_count=record.usage_count,
        rate_limit=record.rate_limit,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        created_at=record.created_at,
    )


async def _user_has_paid_subscription(user_id: UUID) -> bool:
    """True iff the user's subscription is in a state that's been paid for.

    We treat ACTIVE and PAST_DUE as "they paid us at least once"; TRIALING is
    NOT enough to mint live keys — those are still a free perk. Without a paid
    plan we lock new live-key creation behind an upgrade.
    """
    sub = await Subscription.get_or_none(user_id=user_id)
    if sub is None:
        return False
    return sub.status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}


@router.post("", response_model=EnterpriseOut, status_code=status.HTTP_201_CREATED)
async def create_enterprise(
    payload: CreateEnterpriseDto, user: User = Depends(get_current_user)
) -> EnterpriseOut:
    # A user can only belong to one enterprise — block if they already do.
    if user.enterprise_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already belong to an enterprise",
        )
    data = payload.model_dump(exclude_unset=True)
    ent = await Enterprise.create(**data)
    # Bind the creating user to the enterprise and bump them to admin so the
    # frontend admin panel actually shows up after creation.
    user.enterprise_id = ent.id
    user.role = UserRole.ENTERPRISE_ADMIN
    await user.save(update_fields=["enterprise_id", "role"])
    await log_audit(
        user_id=user.id,
        enterprise_id=ent.id,
        action=AuditAction.USER_CREATED,
        entity_type="enterprise",
        entity_id=str(ent.id),
        description=f"Created enterprise {ent.name}",
    )
    return EnterpriseOut.model_validate(ent, from_attributes=True)


@router.get("/{enterprise_id}", response_model=EnterpriseOut)
async def get_enterprise(
    enterprise_id: UUID, user: User = Depends(get_current_user)
) -> EnterpriseOut:
    ent = await Enterprise.get_or_none(id=enterprise_id, deleted_at=None)
    if not ent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")
    return EnterpriseOut.model_validate(ent, from_attributes=True)


@router.put("/{enterprise_id}", response_model=EnterpriseOut)
async def update_enterprise(
    enterprise_id: UUID,
    payload: UpdateEnterpriseDto,
    user: User = Depends(get_current_user),
) -> EnterpriseOut:
    ent = await Enterprise.get_or_none(id=enterprise_id, deleted_at=None)
    if not ent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(ent, key, value)
    await ent.save()
    await log_audit(
        user_id=user.id,
        enterprise_id=ent.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="enterprise",
        entity_id=str(ent.id),
    )
    return EnterpriseOut.model_validate(ent, from_attributes=True)


@router.get("/{enterprise_id}/stats", response_model=EnterpriseStatsOut)
async def enterprise_stats(
    enterprise_id: UUID, user: User = Depends(get_current_user)
) -> EnterpriseStatsOut:
    ent = await Enterprise.get_or_none(id=enterprise_id, deleted_at=None)
    if not ent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")
    active_keys = await ApiKey.filter(
        enterprise_id=enterprise_id, status=ApiKeyStatus.ACTIVE, deleted_at=None
    ).count()
    user_count = await User.filter(enterprise_id=enterprise_id, deleted_at=None).count()
    # Only count "real" documents — exclude drafts (in-editor work that may
    # never be committed) and uploaded-but-never-touched extractions.
    document_count = await Document.filter(
        enterprise_id=enterprise_id, deleted_at=None
    ).exclude(status=DocumentStatus.DRAFT).count()
    # Sum usage_count across all (non-deleted) keys for this enterprise. The
    # number of keys per org is tiny (single digits typically), so doing this
    # in Python is fine — avoids fragile annotate/aggregate chaining.
    all_keys = await ApiKey.filter(
        enterprise_id=enterprise_id, deleted_at=None
    ).only("usage_count")
    api_calls = sum(k.usage_count for k in all_keys)
    return EnterpriseStatsOut(
        users=user_count,
        documents=document_count,
        api_calls=api_calls,
        active_api_keys=active_keys,
        limits={
            "max_users": ent.max_users,
            "max_documents": ent.max_documents,
            "max_api_calls": ent.max_api_calls,
        },
    )


@router.get(
    "/{enterprise_id}/documents", response_model=list[EnterpriseDocumentOut]
)
async def list_enterprise_documents(
    enterprise_id: UUID,
    limit: int = 20,
    user: User = Depends(get_current_user),
) -> list[EnterpriseDocumentOut]:
    """Recent documents signed through this enterprise's API keys.

    Used by the admin panel to show what's been processed lately. Capped at
    `limit` (default 20) — full pagination can come later if anyone asks.
    """
    ent = await Enterprise.get_or_none(id=enterprise_id, deleted_at=None)
    if not ent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")
    # Drafts are work-in-progress in the live editor — they shouldn't show up
    # in the enterprise audit/recent-docs view alongside actually-signed work.
    rows = (
        await Document.filter(enterprise_id=enterprise_id, deleted_at=None)
        .exclude(status=DocumentStatus.DRAFT)
        .order_by("-created_at")
        .limit(max(1, min(limit, 100)))
    )
    return [
        EnterpriseDocumentOut(
            id=d.id,
            original_file_name=d.original_file_name,
            file_size=d.file_size,
            status=d.status,
            document_type=d.document_type,
            completed_at=d.completed_at,
            created_at=d.created_at,
        )
        for d in rows
    ]


@router.post(
    "/{enterprise_id}/api-keys",
    response_model=CreateApiKeyOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    enterprise_id: UUID,
    payload: CreateApiKeyDto,
    user: User = Depends(get_current_user),
) -> CreateApiKeyOut:
    ent = await Enterprise.get_or_none(id=enterprise_id, deleted_at=None)
    if not ent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")

    # Gate live keys behind a paid subscription — trial users get test keys only.
    if payload.key_type == ApiKeyType.LIVE:
        if not await _user_has_paid_subscription(user.id):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    "Live API keys require a paid subscription. "
                    "Upgrade your plan or create a test key (free during trial)."
                ),
            )

    # Stripe-style prefix so devs can tell at a glance which mode a key is in.
    prefix = "sk_live" if payload.key_type == ApiKeyType.LIVE else "sk_test"
    plaintext = f"{prefix}_{secrets.token_hex(28)}"
    hashed = sha256_hex(plaintext)
    record = await ApiKey.create(
        enterprise=ent,
        name=payload.name,
        key=hashed,
        key_type=payload.key_type,
        description=payload.description,
        permissions=payload.permissions,
        ip_whitelist=payload.ip_whitelist,
        rate_limit=payload.rate_limit,
        expires_at=payload.expires_at,
    )
    await log_audit(
        user_id=user.id,
        enterprise_id=ent.id,
        action=AuditAction.API_KEY_CREATED,
        entity_type="api_key",
        entity_id=str(record.id),
    )
    return CreateApiKeyOut(api_key=_api_key_out(record, ent.id), key=plaintext)


@router.get("/{enterprise_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    enterprise_id: UUID, user: User = Depends(get_current_user)
) -> list[ApiKeyOut]:
    rows = await ApiKey.filter(enterprise_id=enterprise_id, deleted_at=None).order_by(
        "-created_at"
    )
    return [_api_key_out(r, enterprise_id) for r in rows]


@router.delete("/{enterprise_id}/api-keys/{key_id}", response_model=MessageOut)
async def revoke_api_key(
    enterprise_id: UUID,
    key_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    record = await ApiKey.get_or_none(
        id=key_id, enterprise_id=enterprise_id, deleted_at=None
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    record.status = ApiKeyStatus.REVOKED
    record.deleted_at = datetime.now(timezone.utc)
    await record.save()
    await log_audit(
        user_id=user.id,
        enterprise_id=enterprise_id,
        action=AuditAction.API_KEY_REVOKED,
        entity_type="api_key",
        entity_id=str(record.id),
    )
    return MessageOut(message="API key revoked")

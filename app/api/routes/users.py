"""User profile and account routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.db.models.audit_log import AuditAction
from app.db.models.data_vault import DataVault
from app.db.models.document import Document
from app.db.models.signature import Signature
from app.db.models.user import User
from app.models.auth_schemas import MessageOut, UserOut
from app.models.user_schemas import UpdateUserDto, UserStatsOut

router = APIRouter()


@router.get("/profile", response_model=UserOut)
async def get_profile(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user, from_attributes=True)


@router.put("/profile", response_model=UserOut)
async def update_profile(
    payload: UpdateUserDto, user: User = Depends(get_current_user)
) -> UserOut:
    before = {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone_number": user.phone_number,
        "profile_image": user.profile_image,
        "preferences": user.preferences,
    }
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)
    await user.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.USER_UPDATED,
        entity_type="user",
        entity_id=str(user.id),
        changes={"before": before, "after": update_data},
    )
    return UserOut.model_validate(user, from_attributes=True)


@router.get("/stats", response_model=UserStatsOut)
async def get_stats(user: User = Depends(get_current_user)) -> UserStatsOut:
    docs = await Document.filter(user_id=user.id, deleted_at=None).count()
    sigs = await Signature.filter(user_id=user.id, deleted_at=None).count()
    fields = await DataVault.filter(user_id=user.id, is_active=True, deleted_at=None).count()
    return UserStatsOut(
        documents_count=docs,
        signatures_count=sigs,
        data_fields_count=fields,
        last_activity=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@router.delete("/account", response_model=MessageOut)
async def delete_account(user: User = Depends(get_current_user)) -> MessageOut:
    user.deleted_at = datetime.now(timezone.utc)
    await user.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.USER_DELETED,
        entity_type="user",
        entity_id=str(user.id),
    )
    return MessageOut(message="Account deleted")

"""Audit log read-only routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.common.deps import get_current_user
from app.db.models.audit_log import AuditAction, AuditLog
from app.db.models.user import User
from app.models.audit_schemas import AuditLogOut, AuditStatsOut

router = APIRouter()


def _to_out(log: AuditLog) -> AuditLogOut:
    return AuditLogOut.model_validate(log, from_attributes=True)


@router.get("/logs", response_model=list[AuditLogOut])
async def list_logs(
    action: AuditAction | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
) -> list[AuditLogOut]:
    query = AuditLog.filter(user_id=user.id)
    if action:
        query = query.filter(action=action)
    if start_date:
        query = query.filter(created_at__gte=start_date)
    if end_date:
        query = query.filter(created_at__lte=end_date)
    rows = await query.order_by("-created_at").limit(limit)
    return [_to_out(r) for r in rows]


@router.get("/stats", response_model=AuditStatsOut)
async def stats(user: User = Depends(get_current_user)) -> AuditStatsOut:
    total = await AuditLog.filter(user_id=user.id).count()
    errors = await AuditLog.filter(user_id=user.id, success=False).count()
    success_rate = ((total - errors) / total * 100) if total else 100.0
    recent = await AuditLog.filter(user_id=user.id).order_by("-created_at").limit(10)
    return AuditStatsOut(
        total=total,
        errors=errors,
        success_rate=round(success_rate, 2),
        recent_activity=[_to_out(r) for r in recent],
    )

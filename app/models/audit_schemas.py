from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.db.models.audit_log import AuditAction, AuditSeverity


class AuditLogOut(BaseModel):
    id: UUID
    user_id: UUID | None
    enterprise_id: UUID | None
    action: AuditAction
    severity: AuditSeverity
    entity_type: str | None
    entity_id: str | None
    description: str | None
    metadata: dict | None
    changes: dict | None
    ip_address: str | None
    success: bool
    error_message: str | None
    created_at: datetime


class AuditStatsOut(BaseModel):
    total: int
    errors: int
    success_rate: float
    recent_activity: list[AuditLogOut]

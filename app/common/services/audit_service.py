"""Audit logging helper. All writes are best-effort — failures never abort the request."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger

from app.db.models.audit_log import AuditAction, AuditLog, AuditSeverity


async def log_audit(
    *,
    user_id: UUID | str | None = None,
    enterprise_id: UUID | str | None = None,
    action: AuditAction,
    severity: AuditSeverity = AuditSeverity.INFO,
    entity_type: str | None = None,
    entity_id: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> AuditLog | None:
    try:
        return await AuditLog.create(
            user_id=user_id,
            enterprise_id=enterprise_id,
            action=action,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            metadata=metadata,
            changes=changes,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            success=success,
            error_message=error_message,
        )
    except Exception as exc:
        logger.warning(f"audit log write failed: {exc}")
        return None

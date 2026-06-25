from enum import Enum

from tortoise import fields
from tortoise.models import Model


class AuditAction(str, Enum):
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DELETED = "user_deleted"
    DATA_CREATED = "data_created"
    DATA_UPDATED = "data_updated"
    DATA_DELETED = "data_deleted"
    DATA_ACCESSED = "data_accessed"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_PROCESSED = "document_processed"
    DOCUMENT_SIGNED = "document_signed"
    DOCUMENT_SHARED = "document_shared"
    DOCUMENT_DOWNLOADED = "document_downloaded"
    DOCUMENT_DELETED = "document_deleted"
    SIGNATURE_CREATED = "signature_created"
    SIGNATURE_USED = "signature_used"
    API_KEY_CREATED = "api_key_created"
    API_KEY_USED = "api_key_used"
    API_KEY_REVOKED = "api_key_revoked"
    SETTINGS_CHANGED = "settings_changed"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_REVOKED = "permission_revoked"


class AuditSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditLog(Model):
    id = fields.UUIDField(pk=True)

    user_id = fields.UUIDField(null=True, index=True)
    enterprise_id = fields.UUIDField(null=True)

    action = fields.CharEnumField(AuditAction, max_length=64)
    severity = fields.CharEnumField(AuditSeverity, max_length=16, default=AuditSeverity.INFO)

    entity_type = fields.CharField(max_length=64, null=True)
    entity_id = fields.CharField(max_length=64, null=True)

    description = fields.TextField(null=True)
    metadata = fields.JSONField(null=True)
    changes = fields.JSONField(null=True)

    ip_address = fields.CharField(max_length=64, null=True)
    user_agent = fields.CharField(max_length=512, null=True)
    request_id = fields.CharField(max_length=128, null=True)

    success = fields.BooleanField(default=True)
    error_message = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True, index=True)

    class Meta:
        table = "audit_logs"

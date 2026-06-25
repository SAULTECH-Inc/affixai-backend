from enum import Enum

from tortoise import fields
from tortoise.models import Model


class ApiKeyStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    REVOKED = "revoked"


class ApiKeyType(str, Enum):
    """Stripe-style key tiers.

    - TEST keys: always allowed; calls hit a sandbox-flavoured code path
      (still real signing, but not counted toward billing).
    - LIVE keys: only issued to enterprises whose owning user has an active
      paid subscription. Calls count toward billed usage.
    """

    TEST = "test"
    LIVE = "live"


class ApiKey(Model):
    id = fields.UUIDField(pk=True)
    enterprise = fields.ForeignKeyField(
        "models.Enterprise", related_name="api_keys", on_delete=fields.CASCADE
    )

    name = fields.CharField(max_length=255)
    key = fields.CharField(max_length=128, unique=True)
    description = fields.TextField(null=True)

    key_type = fields.CharEnumField(
        ApiKeyType, max_length=8, default=ApiKeyType.TEST
    )
    status = fields.CharEnumField(ApiKeyStatus, max_length=16, default=ApiKeyStatus.ACTIVE)

    permissions = fields.JSONField(null=True)
    ip_whitelist = fields.JSONField(null=True)

    usage_count = fields.IntField(default=0)
    rate_limit = fields.IntField(null=True)

    last_used_at = fields.DatetimeField(null=True)
    expires_at = fields.DatetimeField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "api_keys"

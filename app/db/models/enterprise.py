from enum import Enum

from tortoise import fields
from tortoise.models import Model


class EnterpriseStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    TRIAL = "trial"


class EnterprisePlan(str, Enum):
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"
    CUSTOM = "custom"


class Enterprise(Model):
    id = fields.UUIDField(pk=True)

    name = fields.CharField(max_length=255, unique=True)
    domain = fields.CharField(max_length=255, unique=True, null=True)
    description = fields.TextField(null=True)
    logo_url = fields.CharField(max_length=1024, null=True)

    status = fields.CharEnumField(
        EnterpriseStatus, max_length=32, default=EnterpriseStatus.TRIAL
    )
    plan = fields.CharEnumField(
        EnterprisePlan, max_length=32, default=EnterprisePlan.STARTER
    )

    contact_email = fields.CharField(max_length=255, null=True)
    contact_phone = fields.CharField(max_length=64, null=True)
    address = fields.JSONField(null=True)

    max_users = fields.IntField(default=10)
    max_documents = fields.IntField(default=1000)
    max_api_calls = fields.IntField(default=10000)

    features = fields.JSONField(null=True)
    custom_branding = fields.JSONField(null=True)
    sso_config = fields.JSONField(null=True)
    webhooks = fields.JSONField(null=True)

    trial_ends_at = fields.DatetimeField(null=True)
    subscription_starts_at = fields.DatetimeField(null=True)
    subscription_ends_at = fields.DatetimeField(null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "enterprises"

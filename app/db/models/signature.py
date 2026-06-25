from enum import Enum

from tortoise import fields
from tortoise.models import Model


class SignatureType(str, Enum):
    DRAWN = "drawn"
    TYPED = "typed"
    UPLOADED = "uploaded"
    DIGITAL_CERTIFICATE = "digital_certificate"


class Signature(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    type = fields.CharEnumField(SignatureType, max_length=32)
    signature_url = fields.CharField(max_length=1024)
    signature_name = fields.CharField(max_length=255, null=True)
    is_default = fields.BooleanField(default=False)
    signature_data = fields.TextField(null=True)
    certificate_id = fields.CharField(max_length=255, null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "signatures"

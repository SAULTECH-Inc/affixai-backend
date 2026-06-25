"""Predefined-segment vault: each row is one field of one segment for one user.

`segment` and `field_name` together identify which predefined field this row
holds. The registry in `app.common.vault_schema` enumerates the allowed pairs.
`encrypted_value` is AES-256-GCM ciphertext.
"""
from enum import Enum

from tortoise import fields
from tortoise.models import Model


class DataSource(str, Enum):
    USER_INPUT = "user_input"
    DOCUMENT_EXTRACTION = "document_extraction"
    THIRD_PARTY = "third_party"
    API_IMPORT = "api_import"


class DataVault(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    # Stored as the VaultSegment enum value string. We don't use CharEnumField
    # here so the registry can evolve in code without DB enum churn.
    segment = fields.CharField(max_length=32, index=True)
    field_name = fields.CharField(max_length=64, index=True)

    encrypted_value = fields.TextField()

    source = fields.CharEnumField(DataSource, max_length=32, default=DataSource.USER_INPUT)
    source_document_id = fields.UUIDField(null=True)
    confidence_score = fields.FloatField(null=True)

    is_active = fields.BooleanField(default=True)
    is_verified = fields.BooleanField(default=False)
    verified_at = fields.DatetimeField(null=True)
    verified_by = fields.UUIDField(null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "data_vault"
        # One value per (user, segment, field) — upsert semantics.
        unique_together = (("user_id", "segment", "field_name"),)

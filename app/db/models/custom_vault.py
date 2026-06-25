"""User-defined vault sections and fields.

The built-in vault has 7 segments (Personal, Identity, Address, Contact,
Employment, Financial, Next-of-Kin) coded into `vault_schema.py`. Users can
also create THEIR OWN sections with their own fields — e.g. "Insurance",
"Memberships", "Vehicle". Those are stored in these two tables.

Value storage is unified with built-in fields: a custom field's value lives
in the same `data_vault` table, with a synthetic segment of `custom:<key>`
and the field's slug as `field_name`. This means:

  - the auto-affix label matcher transparently sees custom fields
  - existing extract/apply endpoints work without changes
  - we don't need a parallel "custom_vault_values" table

The two tables here only carry the SCHEMA (what sections/fields exist for
this user) — not the values themselves.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class CustomFieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    FILE = "file"


class CustomScope(str, Enum):
    """Who owns the section/field DEFINITION (not the values).

    - USER: visible only to the user who created it (`user_id` is set).
    - ENTERPRISE: visible to every member of the enterprise
      (`enterprise_id` is set, `user_id` is null). Only enterprise admins
      can create or edit these. Values remain per-user — each member has
      their OWN "Policy Number" but the section/field schema is shared.
    """
    USER = "user"
    ENTERPRISE = "enterprise"


class CustomVaultSection(Model):
    id = fields.UUIDField(pk=True)
    # Exactly one of these is set, depending on `scope`. Both nullable so the
    # unique constraints below can coexist without conflict.
    user_id = fields.UUIDField(index=True, null=True)
    enterprise_id = fields.UUIDField(index=True, null=True)
    scope = fields.CharEnumField(CustomScope, max_length=16, default=CustomScope.USER)

    # Display name as the user typed it ("Insurance", "Vehicle Details").
    name = fields.CharField(max_length=120)
    # Slug derived from name at create time. We persist it so renaming the
    # display name doesn't break existing data_vault rows that key on it.
    # Stored as `custom:<key>` in the data_vault.segment column.
    key = fields.CharField(max_length=64)

    # Optional Lucide icon name (e.g. "shield", "car"). Frontend resolves it.
    icon = fields.CharField(max_length=32, null=True)

    # Position in the user's vault sidebar. Lower = higher in the list.
    display_order = fields.IntField(default=100)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "custom_vault_sections"
        # In Postgres, NULL != NULL in unique constraints, so user-scope rows
        # (enterprise_id null) and enterprise-scope rows (user_id null) don't
        # conflict with each other on (user_id, key) or (enterprise_id, key).
        unique_together = (
            ("user_id", "key"),
            ("enterprise_id", "key"),
        )


class CustomVaultField(Model):
    id = fields.UUIDField(pk=True)
    # Mirrors the parent section's owner — denormalized for cheap permission
    # checks and to keep value-lookup queries simple.
    user_id = fields.UUIDField(index=True, null=True)
    enterprise_id = fields.UUIDField(index=True, null=True)
    section: fields.ForeignKeyRelation[CustomVaultSection] = fields.ForeignKeyField(
        "models.CustomVaultSection",
        related_name="custom_fields",
        on_delete=fields.CASCADE,
    )

    name = fields.CharField(max_length=120)        # display label
    key = fields.CharField(max_length=64)          # slug; data_vault.field_name
    field_type = fields.CharEnumField(
        CustomFieldType, max_length=16, default=CustomFieldType.TEXT
    )

    # Extra label aliases for auto-affix matching. e.g. for a field named
    # "Policy Number" the user might add ["Insurance #", "Pol. No."].
    aliases = fields.JSONField(null=True)

    # Soft constraints surfaced by the UI; we don't enforce server-side
    # beyond type coercion.
    placeholder = fields.CharField(max_length=255, null=True)
    required = fields.BooleanField(default=False)

    display_order = fields.IntField(default=100)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "custom_vault_fields"
        unique_together = (("section_id", "key"),)

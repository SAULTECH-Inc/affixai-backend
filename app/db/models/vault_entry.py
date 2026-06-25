"""Multi-entry vault rows.

The existing `DataVault` table is shaped for SINGLE-VALUE fields (one
"date of birth" per user, one "passport number"). It can't naturally model
the things a CV has many of: education degrees, employment roles.

This table is the answer. One row per entry. The entry's payload (all its
fields) is encrypted as a single JSON blob — we don't index any of the
inner fields, so storing them granularly buys nothing.

Sections (current):
  * "education"  — schools/degrees attended
  * "employment" — job roles held

Adding more in the future = add an enum value + a field-list constant in
`vault_schema.py`. Nothing else changes.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class VaultEntry(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    # Slug name of the section, matches VaultSegment values like "education".
    # Stored as a string so we can add sections without an ENUM migration.
    section = fields.CharField(max_length=32, index=True)

    # JSON map of field_name → value, then AES-encrypted as one string via
    # the same vault helpers used elsewhere. Decoded on read.
    encrypted_payload = fields.TextField()

    # "Currently here" — frontend renders this as a checkbox; auto-affix
    # picks the is_current entry first when resolving labels like
    # "Employer Name". At most one entry per (user, section) should have
    # this true; the route handler enforces it on save.
    is_current = fields.BooleanField(default=False)

    # Display order in the editor. Lower = higher in the list. We keep it
    # explicit so users can rearrange ("most-impressive role first") without
    # depending on creation timestamps.
    sort_order = fields.IntField(default=100)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "vault_entries"

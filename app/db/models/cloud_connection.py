"""Per-user OAuth credentials for cloud-storage exports.

One row per (user, provider). We store access + refresh tokens here so the
backend can upload on the user's behalf without re-prompting consent every
time.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class CloudProvider(str, Enum):
    GOOGLE_DRIVE = "google_drive"
    DROPBOX = "dropbox"
    ONEDRIVE = "onedrive"
    MS365 = "ms365"  # OneDrive for Business / SharePoint via Microsoft Graph


class CloudConnection(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    provider = fields.CharEnumField(CloudProvider, max_length=24)

    # OAuth tokens — encrypted at rest. We use the same encrypt/decrypt
    # helpers as the data vault so a DB dump leak is still not a token leak.
    encrypted_access_token = fields.TextField()
    encrypted_refresh_token = fields.TextField(null=True)
    expires_at = fields.DatetimeField(null=True)

    # The remote account this connection represents — surfaced in the UI so
    # users with multiple accounts know which one they hooked up.
    account_email = fields.CharField(max_length=255, null=True)
    account_name = fields.CharField(max_length=255, null=True)

    # Scopes we have. Stored so we can tell at upload time whether we need
    # to re-prompt for additional permissions.
    scopes = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "cloud_connections"
        # One active connection per (user, provider). Re-connecting replaces
        # the existing row in-place so we don't accumulate stale tokens.
        unique_together = (("user_id", "provider"),)

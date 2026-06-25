"""Inbound leads from the public marketing pages.

Two flavors share the same table because they have ~90% the same shape:
  * "contact" — generic /contact form messages
  * "careers" — applications submitted via /careers

The `kind` discriminator + a freeform `extra` JSON column keep the table
flexible (we can add new lead sources later without a migration).

Leads are PUBLIC-WRITE: anyone on the internet can POST one. The route
that creates them must apply rate-limiting and basic spam heuristics.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class LeadKind(str, Enum):
    CONTACT = "contact"
    CAREERS = "careers"


class LeadStatus(str, Enum):
    # New = unread, reviewed = a human looked at it, archived = soft-removed
    # from the inbox view but kept for audit.
    NEW = "new"
    REVIEWED = "reviewed"
    ARCHIVED = "archived"


class Lead(Model):
    id = fields.UUIDField(pk=True)

    kind = fields.CharEnumField(LeadKind, max_length=16, index=True)
    status = fields.CharEnumField(LeadStatus, max_length=16, default=LeadStatus.NEW, index=True)

    # The visitor's self-reported identity. Length caps are generous but
    # bounded — we don't want a single payload eating a row's worth of disk.
    name = fields.CharField(max_length=200)
    email = fields.CharField(max_length=254)  # RFC 5321 max length

    # Contact: "Sales" / "Support" / etc. Careers: the role title.
    topic = fields.CharField(max_length=120, null=True)

    message = fields.TextField()

    # Freeform extras (e.g. resume URL, LinkedIn, referrer). Keep small.
    extra = fields.JSONField(null=True)

    # Forensics: IP and UA help us identify abuse patterns. We don't surface
    # these in the admin UI by default but they're available for audit.
    ip_address = fields.CharField(max_length=64, null=True)
    user_agent = fields.CharField(max_length=512, null=True)

    # Review trail.
    reviewed_at = fields.DatetimeField(null=True)
    reviewed_by_id = fields.UUIDField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "leads"
        ordering = ["-created_at"]

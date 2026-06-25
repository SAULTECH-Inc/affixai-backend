"""Sender-defined signing placeholders awaiting a specific participant.

`field_placements` on Document stores the OWNER's stamped values (already
applied to the PDF). Signing targets are different: they're EMPTY rectangles
the owner reserved for a participant to fill in when they sign.

Per-participant: each target belongs to exactly one DocumentParticipant.
Per-document: a target is meaningless without its document (so it cascades
on document delete — soft delete here too).
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class SigningTargetKind(str, Enum):
    SIGNATURE = "signature"   # paste the participant's signature image
    INITIALS = "initials"     # short typed initials
    DATE = "date"             # today's date when signed
    TEXT = "text"             # arbitrary text supplied by participant


class DocumentSigningTarget(Model):
    id = fields.UUIDField(pk=True)
    document_id = fields.UUIDField(index=True)
    participant_id = fields.UUIDField(index=True)
    kind = fields.CharEnumField(SigningTargetKind, max_length=16)

    # Box on the PDF. Coordinates use the same "top-of-rect" semantics as
    # `field_placements` so the editor overlay and re-stamp paths agree.
    page = fields.IntField()
    x = fields.FloatField()
    y = fields.FloatField()
    width = fields.FloatField(default=180.0)
    height = fields.FloatField(default=36.0)

    # For TEXT / INITIALS targets — short label the participant sees ("Initials
    # here", "Print full name", etc.). Optional.
    label = fields.CharField(max_length=120, null=True)

    # Set when the participant fills this target. We store the actual stamped
    # value (decrypted signature URL, typed initials, etc.) so the editor can
    # render a preview without re-rasterizing the PDF.
    filled_at = fields.DatetimeField(null=True)
    filled_value = fields.TextField(null=True)

    # Ordering when more than one target exists for the same participant on
    # the same page — we use this so guests sign top-to-bottom naturally.
    sort_order = fields.IntField(default=100)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "document_signing_targets"

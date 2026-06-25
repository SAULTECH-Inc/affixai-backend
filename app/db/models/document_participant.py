"""Who's involved in a document, with what role, and where they are in the
signing/review flow.

This table is the foundation of the Collaboration layer (Phase C) and the
Workflow layer (Phase D). A row links a Document to either:
  * an existing User (user_id set), OR
  * a not-yet-registered invitee identified by email (user_id null until
    they create an account using the invite link).

`invite_token` is a long random string we put in the magic-link URL.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class ParticipantRole(str, Enum):
    """What can this participant do on the document?

    SIGNER   — must apply their signature; counts toward completion
    REVIEWER — can comment / approve / decline; doesn't sign
    VIEWER   — read-only access (e.g. legal, observers)
    """
    SIGNER = "signer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class ParticipantStatus(str, Enum):
    INVITED = "invited"      # email sent, not yet clicked
    VIEWED = "viewed"        # opened the document via magic link or app
    SIGNED = "signed"        # signed (only valid for SIGNER role)
    APPROVED = "approved"    # reviewer approved
    DECLINED = "declined"    # signer/reviewer declined
    REVOKED = "revoked"      # owner revoked their invitation


class DocumentParticipant(Model):
    id = fields.UUIDField(pk=True)
    document_id = fields.UUIDField(index=True)

    # Exactly one of (user_id) or (email) is the strong identifier. If the
    # invitee already has a platform account, we resolve their user at invite
    # time and store both. Otherwise user_id stays null until they sign up.
    user_id = fields.UUIDField(null=True, index=True)
    email = fields.CharField(max_length=255, index=True)
    # Display name — pulled from the invite payload OR from the user row when
    # resolved. Used in lists / emails so we don't always show raw addresses.
    name = fields.CharField(max_length=255, null=True)

    role = fields.CharEnumField(
        ParticipantRole, max_length=16, default=ParticipantRole.SIGNER
    )
    status = fields.CharEnumField(
        ParticipantStatus, max_length=16, default=ParticipantStatus.INVITED
    )

    # Workflow ordering. For Phase C this is informational; Phase D's state
    # machine uses it to enforce sequential vs parallel signing chains.
    sequence_order = fields.IntField(default=1)

    # Magic-link token for unauthenticated access (Phase D will activate this).
    invite_token = fields.CharField(max_length=64, unique=True, index=True)

    invited_by = fields.UUIDField()  # the user who created this invite

    # Lifecycle timestamps.
    invited_at = fields.DatetimeField(auto_now_add=True)
    first_viewed_at = fields.DatetimeField(null=True)
    completed_at = fields.DatetimeField(null=True)  # signed / approved / declined

    # Free-form message attached to the invitation email.
    message = fields.TextField(null=True)

    # Misc. The decline-reason / IP / user-agent live here so the table
    # doesn't grow a column per audit field.
    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "document_participants"
        # A given email can only be invited once per document — re-inviting
        # the same email updates the existing row instead of creating a new
        # one. (Enforced in the route, not just here.)
        unique_together = (("document_id", "email"),)

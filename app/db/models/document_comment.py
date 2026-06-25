"""Side-panel comments on a document.

MVP scope: flat list of comments per document, attributable to a user or
guest participant (via invite_token). We deliberately avoid in-PDF position
anchoring (x/y per comment) in this version — that's a UI overhaul, not a
data-model change, and we want the data model stable BEFORE we wire the
anchored-comments visual layer.

Threads: not yet — `parent_id` is reserved for the future flat-to-tree
upgrade but unused in MVP.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class DocumentComment(Model):
    id = fields.UUIDField(pk=True)
    document_id = fields.UUIDField(index=True)

    # The author. We resolve participants to a user ONLY if they have an
    # account; guests commenting via magic-link have `user_id=null` and we
    # surface their `author_name` (captured at comment time).
    user_id = fields.UUIDField(null=True, index=True)
    participant_id = fields.UUIDField(null=True, index=True)
    author_name = fields.CharField(max_length=255)
    author_email = fields.CharField(max_length=255, null=True)

    # Body. Treated as plain text in the MVP; markdown rendering can come
    # later without a schema change.
    body = fields.TextField()

    # Reserved for future threading + in-PDF anchoring. Null in MVP.
    parent_id = fields.UUIDField(null=True)
    page = fields.IntField(null=True)
    x = fields.FloatField(null=True)
    y = fields.FloatField(null=True)

    resolved = fields.BooleanField(default=False)
    resolved_at = fields.DatetimeField(null=True)
    resolved_by = fields.UUIDField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "document_comments"

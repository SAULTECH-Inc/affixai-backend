from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models.document_participant import (
    ParticipantRole,
    ParticipantStatus,
)


# ---- Participants ----------------------------------------------------------


class InviteParticipantDto(BaseModel):
    """Add or update one participant on a document."""
    email: EmailStr
    name: str | None = None
    role: ParticipantRole = ParticipantRole.SIGNER
    sequence_order: int = Field(default=1, ge=1, le=99)
    message: str | None = Field(default=None, max_length=2000)


class BatchInviteDto(BaseModel):
    """Invite multiple participants in one call. Each one is created/updated
    independently — partial failures (e.g. one bad email) don't abort the
    rest, they're returned in the response."""
    participants: list[InviteParticipantDto] = Field(min_length=1, max_length=50)
    # Optional message applied to every participant who doesn't have their
    # own. Keeps the common case tidy.
    default_message: str | None = None


class ParticipantOut(BaseModel):
    id: UUID
    document_id: UUID
    user_id: UUID | None
    email: str
    name: str | None
    role: ParticipantRole
    status: ParticipantStatus
    sequence_order: int
    invited_at: datetime
    first_viewed_at: datetime | None
    completed_at: datetime | None
    message: str | None
    # NEVER include `invite_token` here — it's a credential. We expose it
    # only on the create response so the inviter can preview the magic link.


class ParticipantCreatedOut(ParticipantOut):
    """Returned ONLY on first invite — includes the magic link the email
    template used. Subsequent reads via GET /participants strip the token."""
    invite_url: str


class BatchInviteResultOut(BaseModel):
    created: list[ParticipantCreatedOut] = Field(default_factory=list)
    updated: list[ParticipantOut] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)  # {email, reason}


# ---- Comments -------------------------------------------------------------


class CommentCreateDto(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    # Anchored-comment coordinates (page-space, top-left origin). All three
    # must be present together or all None.
    page: int | None = None
    x: float | None = None
    y: float | None = None
    # Reply to an existing comment. Server enforces that the parent belongs
    # to the same document.
    parent_id: UUID | None = None


class CommentUpdateDto(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=10_000)
    resolved: bool | None = None


class CommentOut(BaseModel):
    id: UUID
    document_id: UUID
    user_id: UUID | None
    parent_id: UUID | None
    author_name: str
    author_email: str | None
    body: str
    page: int | None
    x: float | None
    y: float | None
    resolved: bool
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Convenience: number of replies. Saves the frontend from a second query
    # to count threaded children for top-level comments.
    reply_count: int = 0


# ---- Pending signatures (used by extension + dashboard) --------------------


class PendingSignatureOut(BaseModel):
    """A document awaiting the current user's signature."""
    document_id: UUID
    document_title: str
    invite_token: str
    sender_name: str | None
    sender_email: str | None
    role: ParticipantRole
    created_at: datetime


# ---- Activity -------------------------------------------------------------


class ActivityEntryOut(BaseModel):
    """A flattened audit_log row for the document activity feed."""
    id: UUID
    action: str
    description: str | None
    actor_email: str | None
    actor_name: str | None
    metadata: dict | None
    created_at: datetime

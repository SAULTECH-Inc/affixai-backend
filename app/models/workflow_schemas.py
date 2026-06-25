from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models.document import RoutingMode, RoutingStatus
from app.db.models.document_participant import ParticipantRole, ParticipantStatus
from app.db.models.document_signing_target import SigningTargetKind


class SendForSignatureDto(BaseModel):
    routing_mode: RoutingMode = RoutingMode.PARALLEL
    expires_at: datetime | None = None
    # Optional message tacked onto every fresh invitation email if the
    # participant didn't have their own message set.
    message: str | None = None


class VoidWorkflowDto(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class DeclineDto(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class WorkflowStatusOut(BaseModel):
    routing_status: RoutingStatus
    routing_mode: RoutingMode
    sent_at: datetime | None
    expires_at: datetime | None
    completed_at: datetime | None
    declined_reason: str | None
    total_required: int           # signers + reviewers
    completed_required: int        # already signed/approved
    is_complete: bool
    is_expired: bool
    next_actor_email: str | None = None
    next_actor_id: UUID | None = None


# --- Guest-token views (no JWT auth; the token IS the credential) ----------


class GuestParticipantOut(BaseModel):
    """Slim profile we expose to the guest themselves. Never includes any
    other participant's email or token."""
    id: UUID
    email: str
    name: str | None
    role: ParticipantRole
    status: ParticipantStatus
    sequence_order: int
    message: str | None
    is_my_turn: bool


class SigningTargetIn(BaseModel):
    participant_id: UUID
    kind: SigningTargetKind = SigningTargetKind.SIGNATURE
    page: int = Field(ge=0)
    x: float
    y: float
    width: float = Field(default=180.0, gt=0)
    height: float = Field(default=36.0, gt=0)
    label: str | None = Field(default=None, max_length=120)
    sort_order: int | None = None


class SigningTargetOut(BaseModel):
    id: UUID
    document_id: UUID
    participant_id: UUID
    kind: SigningTargetKind
    page: int
    x: float
    y: float
    width: float
    height: float
    label: str | None
    sort_order: int
    filled_at: datetime | None
    filled_value: str | None
    created_at: datetime


class GuestDocumentOut(BaseModel):
    document_id: UUID
    original_file_name: str
    file_mime_type: str
    file_size: int
    routing_status: RoutingStatus
    routing_mode: RoutingMode
    sent_at: datetime | None
    expires_at: datetime | None
    completed_at: datetime | None
    sender_name: str | None
    # The guest's own participant row.
    me: GuestParticipantOut
    # Targets the guest needs to fill — empty list means "free placement"
    # (current bottom-right stamping behavior).
    my_targets: list[SigningTargetOut] = Field(default_factory=list)

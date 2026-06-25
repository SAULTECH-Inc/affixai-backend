"""Workflow state machine for document signing flows.

Two orthogonal concepts:

  * `Document.routing_status` ∈ {draft, sent, in_progress, completed,
    declined, expired, voided} — the workflow as a whole.
  * `DocumentParticipant.status` ∈ {invited, viewed, signed, approved,
    declined, revoked} — per-signer state.

Transitions, all enforced by this service (no route handler should mutate
the columns directly):

  Document:
    draft       → sent            (send_for_signature)
    sent        → in_progress     (any participant acts)
    in_progress → completed       (last required participant acts)
    *           → declined        (any signer declines)
    *           → expired         (lazy check at access)
    sent | in_progress → voided   (owner cancels)

  Participant:
    invited → viewed → (signed | approved | declined)
    revoked is a terminal owner-side state and is set on revoke/delete.

Routing modes:
  * PARALLEL   — every participant gets their invite on send.
  * SEQUENTIAL — only the lowest unfinished sequence_order participant is
    active. Their email is fired when their turn arrives.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from loguru import logger

from app.db.models.audit_log import AuditAction
from app.db.models.document import Document, DocumentStatus, RoutingMode, RoutingStatus
from app.db.models.document_participant import (
    DocumentParticipant,
    ParticipantRole,
    ParticipantStatus,
)


# ---- Utilities --------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_terminal_participant(status: ParticipantStatus) -> bool:
    return status in {
        ParticipantStatus.SIGNED,
        ParticipantStatus.APPROVED,
        ParticipantStatus.DECLINED,
        ParticipantStatus.REVOKED,
    }


def _requires_sign(role: ParticipantRole) -> bool:
    """Signers must SIGN to count toward completion. Reviewers must APPROVE.
    Viewers don't need to act for completion."""
    return role in {ParticipantRole.SIGNER, ParticipantRole.REVIEWER}


@dataclass
class WorkflowSnapshot:
    """Lightweight read-model of the workflow state for one document."""

    status: RoutingStatus
    mode: RoutingMode
    total_required: int        # signers + reviewers
    completed_required: int    # how many of those have signed/approved
    next_actor: DocumentParticipant | None
    is_complete: bool
    is_expired: bool
    expires_at: datetime | None


# ---- Read APIs --------------------------------------------------------------


async def snapshot(doc: Document) -> WorkflowSnapshot:
    """Compute the current workflow snapshot for a document.

    Used by routes / templates to render progress without mutating anything.
    Also runs the lazy expiration check (see expire_if_due) so reading the
    snapshot can be enough to flip an overdue doc to EXPIRED.
    """
    await expire_if_due(doc)

    participants = await DocumentParticipant.filter(
        document_id=doc.id, deleted_at=None
    ).order_by("sequence_order", "invited_at")
    required = [p for p in participants if _requires_sign(p.role)]
    completed = [
        p for p in required
        if p.status in {ParticipantStatus.SIGNED, ParticipantStatus.APPROVED}
    ]

    next_actor = await next_pending_actor(doc, participants=participants)

    is_complete = (
        bool(required)
        and len(completed) == len(required)
        and doc.routing_status not in {RoutingStatus.DECLINED, RoutingStatus.EXPIRED}
    )
    return WorkflowSnapshot(
        status=doc.routing_status,
        mode=doc.routing_mode,
        total_required=len(required),
        completed_required=len(completed),
        next_actor=next_actor,
        is_complete=is_complete,
        is_expired=doc.routing_status == RoutingStatus.EXPIRED,
        expires_at=doc.expires_at,
    )


async def next_pending_actor(
    doc: Document,
    *,
    participants: list[DocumentParticipant] | None = None,
) -> DocumentParticipant | None:
    """The next participant whose action the workflow is waiting on.

    PARALLEL: any non-terminal required participant (the first one in
    sequence_order).
    SEQUENTIAL: the participant with the LOWEST sequence_order whose status
    is still pending.
    """
    if participants is None:
        participants = await DocumentParticipant.filter(
            document_id=doc.id, deleted_at=None
        ).order_by("sequence_order", "invited_at")

    pending = [
        p for p in participants
        if _requires_sign(p.role) and not _is_terminal_participant(p.status)
    ]
    if not pending:
        return None

    if doc.routing_mode == RoutingMode.SEQUENTIAL:
        pending.sort(key=lambda p: (p.sequence_order, p.invited_at))
        return pending[0]
    # PARALLEL — there is no "next" per se; surface the first pending so the
    # frontend has something to show as a hint.
    return pending[0]


async def is_participant_actionable(
    doc: Document, participant: DocumentParticipant
) -> bool:
    """Decide whether `participant` is allowed to ACT on `doc` right now.

    Used to gate guest-link sign/decline/approve actions.
    """
    if doc.routing_status not in {RoutingStatus.SENT, RoutingStatus.IN_PROGRESS}:
        return False
    if not _requires_sign(participant.role):
        # Viewers can always view but never "act" — caller's responsibility
        # to differentiate read-only.
        return False
    if _is_terminal_participant(participant.status):
        return False
    if doc.routing_mode == RoutingMode.PARALLEL:
        return True
    # SEQUENTIAL: only the current head-of-queue can act.
    head = await next_pending_actor(doc)
    return head is not None and head.id == participant.id


# ---- Transitions ------------------------------------------------------------


async def send_for_signature(
    doc: Document,
    sender_user_id: UUID,
    expires_at: datetime | None = None,
) -> WorkflowSnapshot:
    """Transition: DRAFT → SENT. Idempotent on already-sent docs.

    Caller is responsible for actually sending the invitation emails (the
    collaboration router does that — we don't need duplicated email logic
    here). This function ONLY mutates the workflow state.
    """
    if doc.routing_status not in {RoutingStatus.DRAFT, RoutingStatus.SENT}:
        raise ValueError(
            f"Cannot send a document already in state {doc.routing_status.value}"
        )

    participants = await DocumentParticipant.filter(
        document_id=doc.id, deleted_at=None
    )
    if not [p for p in participants if _requires_sign(p.role)]:
        raise ValueError(
            "At least one signer or reviewer must be added before sending"
        )

    doc.routing_status = RoutingStatus.SENT
    doc.sent_at = _utc_now()
    if expires_at:
        doc.expires_at = expires_at
    # Reflect the workflow change in the per-doc processing status too so
    # the Documents list shows "pending signature" instead of "draft".
    doc.status = DocumentStatus.PENDING_SIGNATURE
    await doc.save()
    logger.info(
        f"workflow: doc={doc.id} sent (mode={doc.routing_mode}) "
        f"required={sum(1 for p in participants if _requires_sign(p.role))}"
    )
    return await snapshot(doc)


async def mark_viewed(
    doc: Document, participant: DocumentParticipant
) -> None:
    """First time a participant opens the document via their token."""
    if participant.first_viewed_at is None:
        participant.first_viewed_at = _utc_now()
    if participant.status == ParticipantStatus.INVITED:
        participant.status = ParticipantStatus.VIEWED
    await participant.save()
    # Owner-side state: any view bumps the doc from SENT to IN_PROGRESS.
    if doc.routing_status == RoutingStatus.SENT:
        doc.routing_status = RoutingStatus.IN_PROGRESS
        await doc.save()


async def record_signature(
    doc: Document, participant: DocumentParticipant
) -> WorkflowSnapshot:
    """Participant has signed (signers) or approved (reviewers).

    Caller has ALREADY applied the signature to the PDF and saved the
    re-stamped file — this function just moves the workflow forward.
    """
    if not await is_participant_actionable(doc, participant):
        raise ValueError(
            "This participant is not currently allowed to act on the document"
        )

    target_status = (
        ParticipantStatus.SIGNED
        if participant.role == ParticipantRole.SIGNER
        else ParticipantStatus.APPROVED
    )
    participant.status = target_status
    participant.completed_at = _utc_now()
    if participant.first_viewed_at is None:
        participant.first_viewed_at = participant.completed_at
    await participant.save()

    if doc.routing_status == RoutingStatus.SENT:
        doc.routing_status = RoutingStatus.IN_PROGRESS

    snap = await snapshot(doc)
    if snap.is_complete:
        doc.routing_status = RoutingStatus.COMPLETED
        doc.status = DocumentStatus.COMPLETED
        doc.completed_at = _utc_now()
        await doc.save()
        snap = await snapshot(doc)
        logger.info(f"workflow: doc={doc.id} COMPLETED")
    else:
        await doc.save()
    return snap


async def record_decline(
    doc: Document,
    participant: DocumentParticipant,
    reason: str | None = None,
) -> WorkflowSnapshot:
    """A signer declined. Halts the workflow."""
    if not await is_participant_actionable(doc, participant):
        raise ValueError("This participant is not currently allowed to act")
    participant.status = ParticipantStatus.DECLINED
    participant.completed_at = _utc_now()
    participant.metadata = {
        **(participant.metadata or {}),
        "decline_reason": reason,
    }
    await participant.save()

    doc.routing_status = RoutingStatus.DECLINED
    doc.declined_by = participant.user_id
    doc.declined_reason = reason
    await doc.save()
    logger.info(f"workflow: doc={doc.id} DECLINED by participant={participant.id}")
    return await snapshot(doc)


async def void(doc: Document, *, reason: str | None = None) -> WorkflowSnapshot:
    """Owner cancels the workflow."""
    if doc.routing_status in {RoutingStatus.COMPLETED, RoutingStatus.VOIDED}:
        return await snapshot(doc)
    doc.routing_status = RoutingStatus.VOIDED
    if reason:
        doc.declined_reason = reason
    await doc.save()
    return await snapshot(doc)


async def expire_if_due(doc: Document) -> bool:
    """Lazy expiration: if expires_at has passed AND the doc is not already
    in a terminal state, flip routing_status to EXPIRED.

    Returns True if we flipped the state, False otherwise. Cheap to call
    repeatedly — the conditional means almost-always a no-op.
    """
    if not doc.expires_at:
        return False
    if doc.routing_status in {
        RoutingStatus.COMPLETED,
        RoutingStatus.DECLINED,
        RoutingStatus.VOIDED,
        RoutingStatus.EXPIRED,
        RoutingStatus.DRAFT,
    }:
        return False
    expiry = doc.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= _utc_now():
        doc.routing_status = RoutingStatus.EXPIRED
        await doc.save()
        logger.info(f"workflow: doc={doc.id} EXPIRED at {expiry}")
        return True
    return False


# ---- Helpers for invitation rollout (SEQUENTIAL mode) ----------------------


async def participants_to_notify(
    doc: Document,
    *,
    participants: Iterable[DocumentParticipant] | None = None,
) -> list[DocumentParticipant]:
    """Which participants should receive an invitation email RIGHT NOW?

    PARALLEL: everyone with status==INVITED.
    SEQUENTIAL: only the current head-of-queue.

    Used by the send_for_signature flow and by reminders.
    """
    if participants is None:
        participants = await DocumentParticipant.filter(
            document_id=doc.id, deleted_at=None
        ).order_by("sequence_order", "invited_at")
    parts = list(participants)

    if doc.routing_mode == RoutingMode.PARALLEL:
        return [
            p for p in parts
            if p.status == ParticipantStatus.INVITED and _requires_sign(p.role)
        ]

    # SEQUENTIAL — single head-of-queue.
    head = await next_pending_actor(doc, participants=parts)
    if head is None:
        return []
    if head.status != ParticipantStatus.INVITED:
        return []
    return [head]


# Re-export AuditAction so route handlers don't have to chase the import.
__all__ = [
    "AuditAction",
    "WorkflowSnapshot",
    "snapshot",
    "next_pending_actor",
    "is_participant_actionable",
    "send_for_signature",
    "mark_viewed",
    "record_signature",
    "record_decline",
    "void",
    "expire_if_due",
    "participants_to_notify",
]

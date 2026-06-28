"""Collaboration layer — participants, comments, activity feed.

Mounted at `/api/v1/documents/{document_id}/...` so the URL hierarchy makes
sense:
  GET    /documents/{id}/participants               — list
  POST   /documents/{id}/participants               — single invite
  POST   /documents/{id}/participants/batch         — bulk invite
  PUT    /documents/{id}/participants/{pid}         — update role / message
  DELETE /documents/{id}/participants/{pid}         — revoke
  POST   /documents/{id}/participants/{pid}/resend  — resend invite email
  GET    /documents/{id}/comments                   — list (chronological)
  POST   /documents/{id}/comments                   — add
  PUT    /documents/{id}/comments/{cid}             — edit / resolve
  DELETE /documents/{id}/comments/{cid}             — delete
  GET    /documents/{id}/activity                   — recent audit events
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.email_service import send_collaboration_invite_email
from app.core.config import settings
from app.db.models.audit_log import AuditAction, AuditLog
from app.db.models.document import Document
from app.db.models.document_comment import DocumentComment
from app.db.models.document_participant import (
    DocumentParticipant,
    ParticipantRole,
    ParticipantStatus,
)
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.collaboration_schemas import (
    ActivityEntryOut,
    BatchInviteDto,
    BatchInviteResultOut,
    CommentCreateDto,
    CommentOut,
    CommentUpdateDto,
    InviteParticipantDto,
    ParticipantCreatedOut,
    ParticipantOut,
    PendingSignatureOut,
)

router = APIRouter()


# ---- My pending signatures --------------------------------------------------


@router.get("/pending-mine", response_model=list[PendingSignatureOut])
async def get_pending_mine(current_user: User = Depends(get_current_user)):
    """Return all documents awaiting the current user's signature.

    Matches by email so the list works even for participants who were invited
    before they registered.
    """
    participants = await DocumentParticipant.filter(
        email=current_user.email,
        status=ParticipantStatus.INVITED,
        deleted_at=None,
    ).order_by("created_at")

    if not participants:
        return []

    # Bulk-fetch the documents and their owners.
    doc_ids = [p.document_id for p in participants]
    docs: dict = {d.id: d for d in await Document.filter(id__in=doc_ids)}

    owner_ids = list({d.owner_id for d in docs.values() if hasattr(d, "owner_id") and d.owner_id})
    owners: dict = {u.id: u for u in await User.filter(id__in=owner_ids)}

    result: list[PendingSignatureOut] = []
    for p in participants:
        doc = docs.get(p.document_id)
        if not doc:
            continue
        owner = owners.get(getattr(doc, "owner_id", None))
        result.append(
            PendingSignatureOut(
                document_id=doc.id,
                document_title=getattr(doc, "title", None) or "Untitled document",
                invite_token=p.invite_token,
                sender_name=(
                    " ".join(filter(None, [owner.first_name, owner.last_name])).strip()
                    or owner.email
                )
                if owner
                else None,
                sender_email=owner.email if owner else None,
                role=p.role,
                created_at=p.created_at,
            )
        )
    return result


# ---- Helpers ----------------------------------------------------------------


async def _get_owned_document(document_id: UUID, user: User) -> Document:
    """Fetch a document the user owns, or raise 404. Centralized so we don't
    repeat the ownership check in every route handler."""
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    return doc


def _participant_to_out(p: DocumentParticipant) -> ParticipantOut:
    return ParticipantOut(
        id=p.id,
        document_id=p.document_id,
        user_id=p.user_id,
        email=p.email,
        name=p.name,
        role=p.role,
        status=p.status,
        sequence_order=p.sequence_order,
        invited_at=p.invited_at,
        first_viewed_at=p.first_viewed_at,
        completed_at=p.completed_at,
        message=p.message,
    )


def _invite_url(token: str) -> str:
    """Build the public magic-link URL the recipient clicks in the email.

    The frontend `/invite/<token>` page handles token validation + redirect
    to the editor. Wired up in Phase D (workflow); for now the page renders a
    placeholder. We hand out the URL anyway so we don't need to migrate
    existing invites when we ship the page.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/invite/{token}"


async def _resolve_user_by_email(email: str) -> User | None:
    """If `email` corresponds to a registered user, return it. Otherwise None."""
    return await User.get_or_none(email=email.lower(), deleted_at=None)


# ---- Participants ----------------------------------------------------------


@router.get("/{document_id}/participants", response_model=list[ParticipantOut])
async def list_participants(
    document_id: UUID, user: User = Depends(get_current_user)
) -> list[ParticipantOut]:
    await _get_owned_document(document_id, user)
    rows = await DocumentParticipant.filter(
        document_id=document_id, deleted_at=None
    ).order_by("sequence_order", "invited_at")
    return [_participant_to_out(p) for p in rows]


async def _upsert_participant(
    doc: Document,
    payload: InviteParticipantDto,
    inviter: User,
    fallback_message: str | None = None,
) -> tuple[DocumentParticipant, bool]:
    """Create or update a participant row. Returns (row, created_flag).

    Idempotent on (document_id, email): repeated invites for the same email
    update the existing row (role / order / message) instead of duplicating.
    """
    email = payload.email.lower()
    existing = await DocumentParticipant.get_or_none(
        document_id=doc.id, email=email, deleted_at=None
    )
    resolved_user = await _resolve_user_by_email(email)
    name = payload.name or (
        " ".join(filter(None, [resolved_user.first_name, resolved_user.last_name])).strip()
        if resolved_user else None
    )
    message = payload.message or fallback_message

    if existing:
        existing.role = payload.role
        existing.sequence_order = payload.sequence_order
        existing.name = name or existing.name
        existing.user_id = resolved_user.id if resolved_user else existing.user_id
        existing.message = message
        # Reset status if it was REVOKED so they can be re-invited.
        if existing.status == ParticipantStatus.REVOKED:
            existing.status = ParticipantStatus.INVITED
        await existing.save()
        return existing, False

    token = secrets.token_urlsafe(32)
    p = await DocumentParticipant.create(
        document_id=doc.id,
        user_id=resolved_user.id if resolved_user else None,
        email=email,
        name=name,
        role=payload.role,
        sequence_order=payload.sequence_order,
        status=ParticipantStatus.INVITED,
        invite_token=token,
        invited_by=inviter.id,
        message=message,
    )
    return p, True


@router.post(
    "/{document_id}/participants",
    response_model=ParticipantCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_participant(
    document_id: UUID,
    payload: InviteParticipantDto,
    user: User = Depends(get_current_user),
) -> ParticipantCreatedOut:
    doc = await _get_owned_document(document_id, user)
    record, created = await _upsert_participant(doc, payload, user)
    invite_url = _invite_url(record.invite_token)

    # Best-effort email — don't fail the API call if SMTP is unreachable.
    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )
    try:
        await send_collaboration_invite_email(
            email=record.email,
            document_name=doc.original_file_name or "document",
            sender_name=sender_name,
            role=record.role.value,
            invite_url=invite_url,
            personal_message=record.message,
        )
    except Exception as exc:
        logger.warning(f"invite email failed for {record.email}: {exc}")

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Invited {record.email} as {record.role.value}",
        metadata={
            "participant_id": str(record.id),
            "email": record.email,
            "role": record.role.value,
            "new_invite": created,
        },
    )

    return ParticipantCreatedOut(
        **_participant_to_out(record).model_dump(),
        invite_url=invite_url,
    )


@router.post(
    "/{document_id}/participants/batch",
    response_model=BatchInviteResultOut,
)
async def invite_participants_batch(
    document_id: UUID,
    payload: BatchInviteDto,
    user: User = Depends(get_current_user),
) -> BatchInviteResultOut:
    """Bulk invite — single network round-trip from the UI."""
    doc = await _get_owned_document(document_id, user)
    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )

    result = BatchInviteResultOut()
    for entry in payload.participants:
        try:
            record, created = await _upsert_participant(
                doc, entry, user, fallback_message=payload.default_message
            )
        except Exception as exc:
            logger.warning(f"batch invite failed for {entry.email}: {exc}")
            result.failed.append({"email": entry.email, "reason": str(exc)})
            continue

        invite_url = _invite_url(record.invite_token)
        try:
            await send_collaboration_invite_email(
                email=record.email,
                document_name=doc.original_file_name or "document",
                sender_name=sender_name,
                role=record.role.value,
                invite_url=invite_url,
                personal_message=record.message,
            )
        except Exception as exc:
            logger.warning(f"batch invite email failed for {record.email}: {exc}")

        if created:
            result.created.append(
                ParticipantCreatedOut(
                    **_participant_to_out(record).model_dump(),
                    invite_url=invite_url,
                )
            )
        else:
            result.updated.append(_participant_to_out(record))

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=(
            f"Batch invite: {len(result.created)} new, "
            f"{len(result.updated)} updated, {len(result.failed)} failed"
        ),
    )
    return result


@router.post(
    "/{document_id}/participants/import",
    response_model=BatchInviteResultOut,
)
async def import_participants_file(
    document_id: UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> BatchInviteResultOut:
    """Bulk-invite from a CSV or Excel file.

    Required columns (case-insensitive): Name, Email, Role, Order.
    Role must be one of: signer, reviewer, viewer.
    Order is an integer (1-based) used as sequence_order.
    """
    doc = await _get_owned_document(document_id, user)

    filename = (file.filename or "").lower()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    rows: list[dict[str, str]] = []
    if filename.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    elif filename.endswith((".xlsx", ".xls")):
        try:
            import openpyxl  # type: ignore[import-untyped]
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed")
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            raise HTTPException(status_code=422, detail="Excel file has no active sheet")
        headers: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(c).strip().lower() if c else "" for c in row]
            else:
                rows.append({headers[j]: str(cell).strip() if cell is not None else ""
                             for j, cell in enumerate(row)})
    else:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .csv or .xlsx file.",
        )

    required = {"name", "email", "role", "order"}
    if rows and not required.issubset(rows[0].keys()):
        missing = required - set(rows[0].keys())
        raise HTTPException(
            status_code=422,
            detail=f"Missing required column(s): {', '.join(sorted(missing))}. "
                   f"File must have: Name, Email, Role, Order",
        )

    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip() or user.email
    )
    result = BatchInviteResultOut()

    for row in rows:
        email = row.get("email", "").lower()
        if not email or "@" not in email:
            result.failed.append({"email": email or "(blank)", "reason": "Invalid email"})
            continue

        raw_role = row.get("role", "signer").lower()
        try:
            role = ParticipantRole(raw_role)
        except ValueError:
            result.failed.append({"email": email, "reason": f"Unknown role '{raw_role}'"})
            continue

        try:
            order = int(float(row.get("order", "1")))
        except (ValueError, TypeError):
            order = 1

        payload = InviteParticipantDto(
            email=email,
            name=row.get("name") or None,
            role=role,
            sequence_order=max(1, order),
        )
        try:
            record, created = await _upsert_participant(doc, payload, user)
        except Exception as exc:
            logger.warning(f"file import failed for {email}: {exc}")
            result.failed.append({"email": email, "reason": str(exc)})
            continue

        invite_url = _invite_url(record.invite_token)
        try:
            await send_collaboration_invite_email(
                email=record.email,
                document_name=doc.original_file_name or "document",
                sender_name=sender_name,
                role=record.role.value,
                invite_url=invite_url,
            )
        except Exception as exc:
            logger.warning(f"file import email failed for {record.email}: {exc}")

        if created:
            result.created.append(
                ParticipantCreatedOut(**_participant_to_out(record).model_dump(), invite_url=invite_url)
            )
        else:
            result.updated.append(_participant_to_out(record))

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=(
            f"File import: {len(result.created)} new, "
            f"{len(result.updated)} updated, {len(result.failed)} failed"
        ),
    )
    return result


@router.put(
    "/{document_id}/participants/{participant_id}",
    response_model=ParticipantOut,
)
async def update_participant(
    document_id: UUID,
    participant_id: UUID,
    payload: InviteParticipantDto,
    user: User = Depends(get_current_user),
) -> ParticipantOut:
    await _get_owned_document(document_id, user)
    p = await DocumentParticipant.get_or_none(
        id=participant_id, document_id=document_id, deleted_at=None
    )
    if not p:
        raise HTTPException(status_code=404, detail="Participant not found")
    if payload.email and payload.email.lower() != p.email:
        # Email change ⇒ effectively a new invitation. We disallow rather
        # than silently re-shuffling tokens — caller should delete + invite.
        raise HTTPException(
            status_code=400,
            detail="Cannot change email; remove the participant and re-invite",
        )
    p.role = payload.role
    p.sequence_order = payload.sequence_order
    if payload.name is not None:
        p.name = payload.name
    if payload.message is not None:
        p.message = payload.message
    await p.save()
    return _participant_to_out(p)


@router.delete(
    "/{document_id}/participants/{participant_id}",
    response_model=MessageOut,
)
async def revoke_participant(
    document_id: UUID,
    participant_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    await _get_owned_document(document_id, user)
    p = await DocumentParticipant.get_or_none(
        id=participant_id, document_id=document_id, deleted_at=None
    )
    if not p:
        raise HTTPException(status_code=404, detail="Participant not found")
    p.status = ParticipantStatus.REVOKED
    p.deleted_at = datetime.now(timezone.utc)
    await p.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(document_id),
        description=f"Revoked invitation for {p.email}",
    )
    return MessageOut(message=f"Revoked {p.email}")


@router.post(
    "/{document_id}/participants/{participant_id}/resend",
    response_model=MessageOut,
)
async def resend_invite(
    document_id: UUID,
    participant_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    doc = await _get_owned_document(document_id, user)
    p = await DocumentParticipant.get_or_none(
        id=participant_id, document_id=document_id, deleted_at=None
    )
    if not p:
        raise HTTPException(status_code=404, detail="Participant not found")

    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )
    try:
        await send_collaboration_invite_email(
            email=p.email,
            document_name=doc.original_file_name or "document",
            sender_name=sender_name,
            role=p.role.value,
            invite_url=_invite_url(p.invite_token),
            personal_message=p.message,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send email: {exc}",
        ) from exc
    return MessageOut(message=f"Re-sent invite to {p.email}")


# ---- Comments ---------------------------------------------------------------


def _comment_to_out(c: DocumentComment, *, reply_count: int = 0) -> CommentOut:
    return CommentOut(
        id=c.id,
        document_id=c.document_id,
        user_id=c.user_id,
        parent_id=c.parent_id,
        author_name=c.author_name,
        author_email=c.author_email,
        body=c.body,
        page=c.page,
        x=c.x,
        y=c.y,
        resolved=c.resolved,
        resolved_at=c.resolved_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
        reply_count=reply_count,
    )


@router.get("/{document_id}/comments", response_model=list[CommentOut])
async def list_comments(
    document_id: UUID, user: User = Depends(get_current_user)
) -> list[CommentOut]:
    await _get_owned_document(document_id, user)
    rows = await DocumentComment.filter(
        document_id=document_id, deleted_at=None
    ).order_by("created_at")
    # Pre-count replies per top-level so the frontend can render the
    # "N replies" affordance without N+1 queries.
    reply_counts: dict[UUID, int] = {}
    for c in rows:
        if c.parent_id:
            reply_counts[c.parent_id] = reply_counts.get(c.parent_id, 0) + 1
    return [
        _comment_to_out(c, reply_count=reply_counts.get(c.id, 0))
        for c in rows
    ]


@router.post(
    "/{document_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    document_id: UUID,
    payload: CommentCreateDto,
    user: User = Depends(get_current_user),
) -> CommentOut:
    doc = await _get_owned_document(document_id, user)

    # Validate the parent belongs to this same document — don't let a reply
    # leak across documents.
    if payload.parent_id:
        parent = await DocumentComment.get_or_none(
            id=payload.parent_id, document_id=doc.id, deleted_at=None
        )
        if not parent:
            raise HTTPException(
                status_code=400,
                detail="Parent comment not found on this document",
            )
        # We allow a one-level reply tree (parent is itself top-level). Nested
        # replies-to-replies get re-parented to the original top-level
        # comment to keep the tree flat.
        if parent.parent_id:
            payload.parent_id = parent.parent_id

    # Either all three coords are set or all None — reject partial anchoring.
    coord_set = sum(
        v is not None for v in (payload.page, payload.x, payload.y)
    )
    if coord_set not in (0, 3):
        raise HTTPException(
            status_code=400,
            detail="Provide all of page/x/y for an anchored comment, or none.",
        )

    author_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )
    c = await DocumentComment.create(
        document_id=doc.id,
        user_id=user.id,
        parent_id=payload.parent_id,
        author_name=author_name,
        author_email=user.email,
        body=payload.body.strip(),
        page=payload.page,
        x=payload.x,
        y=payload.y,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,  # closest existing action
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Commented on {doc.original_file_name}",
        metadata={"comment_id": str(c.id)},
    )
    return _comment_to_out(c)


@router.put(
    "/{document_id}/comments/{comment_id}", response_model=CommentOut
)
async def update_comment(
    document_id: UUID,
    comment_id: UUID,
    payload: CommentUpdateDto,
    user: User = Depends(get_current_user),
) -> CommentOut:
    await _get_owned_document(document_id, user)
    c = await DocumentComment.get_or_none(
        id=comment_id, document_id=document_id, deleted_at=None
    )
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found")
    # Edit gating: only the author can edit; anyone with doc access can
    # resolve.
    if payload.body is not None:
        if c.user_id != user.id:
            raise HTTPException(
                status_code=403, detail="Only the author can edit a comment"
            )
        c.body = payload.body.strip()
    if payload.resolved is not None and payload.resolved != c.resolved:
        c.resolved = payload.resolved
        c.resolved_at = datetime.now(timezone.utc) if payload.resolved else None
        c.resolved_by = user.id if payload.resolved else None
    await c.save()
    return _comment_to_out(c)


@router.delete(
    "/{document_id}/comments/{comment_id}", response_model=MessageOut
)
async def delete_comment(
    document_id: UUID,
    comment_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    await _get_owned_document(document_id, user)
    c = await DocumentComment.get_or_none(
        id=comment_id, document_id=document_id, deleted_at=None
    )
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found")
    if c.user_id != user.id:
        raise HTTPException(
            status_code=403, detail="Only the author can delete a comment"
        )
    c.deleted_at = datetime.now(timezone.utc)
    await c.save()
    return MessageOut(message="Comment deleted")


# ---- Activity feed ----------------------------------------------------------


@router.get("/{document_id}/activity", response_model=list[ActivityEntryOut])
async def list_activity(
    document_id: UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
) -> list[ActivityEntryOut]:
    """Document-specific activity feed.

    Pulled from the audit_log table where entity_type='document' AND
    entity_id matches. We join in actor email/name in a single Python loop
    rather than a JOIN — keeps the query simple and the volume is small.
    """
    await _get_owned_document(document_id, user)
    rows = (
        await AuditLog.filter(
            entity_type="document", entity_id=str(document_id)
        )
        .order_by("-created_at")
        .limit(max(1, min(limit, 200)))
    )
    if not rows:
        return []
    actor_ids = {r.user_id for r in rows if r.user_id}
    actors = {
        u.id: u
        for u in await User.filter(id__in=list(actor_ids))
    }
    out: list[ActivityEntryOut] = []
    for r in rows:
        actor = actors.get(r.user_id) if r.user_id else None
        out.append(
            ActivityEntryOut(
                id=r.id,
                action=r.action.value if hasattr(r.action, "value") else str(r.action),
                description=r.description,
                actor_email=actor.email if actor else None,
                actor_name=(
                    " ".join(filter(None, [actor.first_name, actor.last_name])).strip()
                    or actor.email
                )
                if actor
                else None,
                metadata=r.metadata,
                created_at=r.created_at,
            )
        )
    return out

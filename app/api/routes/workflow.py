"""Workflow routes — owner-side actions + guest token endpoints.

Mounted at:
  /api/v1/documents/{id}/...        — owner-side workflow controls (JWT auth)
  /api/v1/shared/{invite_token}/... — guest-side actions (token = credential)
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.email_service import send_collaboration_invite_email
from app.common.services.webhook_dispatcher import (
    WebhookEventType,
    dispatch_event,
)
from app.common.services.workflow import (
    participants_to_notify,
    record_decline,
    record_signature,
    send_for_signature,
    snapshot,
    void,
    mark_viewed,
    is_participant_actionable,
)
from app.common.services.local_storage import UPLOADS_ROOT
from app.core.config import settings
from app.db.models.audit_log import AuditAction
from app.db.models.document import Document, RoutingMode, RoutingStatus
from app.db.models.document_participant import (
    DocumentParticipant,
    ParticipantStatus,
)
from app.db.models.document_signing_target import (
    DocumentSigningTarget,
    SigningTargetKind,
)
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.workflow_schemas import (
    DeclineDto,
    GuestDocumentOut,
    GuestParticipantOut,
    SendForSignatureDto,
    SigningTargetIn,
    SigningTargetOut,
    VoidWorkflowDto,
    WorkflowStatusOut,
)


owner_router = APIRouter()   # mounted at /api/v1/documents
guest_router = APIRouter()   # mounted at /api/v1/shared


# ---- Helpers ----------------------------------------------------------------


async def _get_owned_doc(document_id: UUID, user: User) -> Document:
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(404, "Document not found")
    return doc


async def _get_participant_by_token(token: str) -> tuple[Document, DocumentParticipant]:
    """Resolve a magic-link token to its (document, participant) pair.

    Raises 404 for invalid / expired (deleted_at) tokens — we intentionally
    don't distinguish "wrong token" vs "doesn't exist" to avoid leaking
    information.
    """
    p = await DocumentParticipant.get_or_none(invite_token=token, deleted_at=None)
    if not p:
        raise HTTPException(404, "Invitation not found or revoked")
    if p.status == ParticipantStatus.REVOKED:
        raise HTTPException(403, "This invitation has been revoked")
    doc = await Document.get_or_none(id=p.document_id, deleted_at=None)
    if not doc:
        raise HTTPException(404, "Document no longer exists")
    return doc, p


def _build_status_out(doc: Document, snap) -> WorkflowStatusOut:
    return WorkflowStatusOut(
        routing_status=doc.routing_status,
        routing_mode=doc.routing_mode,
        sent_at=doc.sent_at,
        expires_at=doc.expires_at,
        completed_at=doc.completed_at,
        declined_reason=doc.declined_reason,
        total_required=snap.total_required,
        completed_required=snap.completed_required,
        is_complete=snap.is_complete,
        is_expired=snap.is_expired,
        next_actor_email=snap.next_actor.email if snap.next_actor else None,
        next_actor_id=snap.next_actor.id if snap.next_actor else None,
    )


def _invite_url(token: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/invite/{token}"


# =============================================================================
# Owner-side endpoints
# =============================================================================


@owner_router.get("/{document_id}/workflow", response_model=WorkflowStatusOut)
async def get_workflow_status(
    document_id: UUID, user: User = Depends(get_current_user)
) -> WorkflowStatusOut:
    doc = await _get_owned_doc(document_id, user)
    snap = await snapshot(doc)
    return _build_status_out(doc, snap)


@owner_router.post("/{document_id}/send", response_model=WorkflowStatusOut)
async def send_document_for_signature(
    document_id: UUID,
    payload: SendForSignatureDto,
    user: User = Depends(get_current_user),
) -> WorkflowStatusOut:
    """Transition the document to SENT and dispatch invitation emails.

    Routing mode + optional expiry are set here (overrides any prior values).
    For SEQUENTIAL flows, only the first signer is notified now; the rest
    receive emails as each predecessor signs.
    """
    doc = await _get_owned_doc(document_id, user)
    doc.routing_mode = payload.routing_mode
    if payload.expires_at:
        doc.expires_at = payload.expires_at
    await doc.save()

    try:
        snap = await send_for_signature(
            doc, sender_user_id=user.id, expires_at=payload.expires_at
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Dispatch invitation emails. Best-effort — SMTP outages shouldn't reverse
    # the workflow transition we just made.
    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )
    to_notify = await participants_to_notify(doc)
    for p in to_notify:
        try:
            await send_collaboration_invite_email(
                email=p.email,
                document_name=doc.original_file_name or "document",
                sender_name=sender_name,
                role=p.role.value,
                invite_url=_invite_url(p.invite_token),
                personal_message=p.message or payload.message,
            )
        except Exception as exc:
            logger.warning(f"send-for-sig email failed for {p.email}: {exc}")

    await dispatch_event(
        WebhookEventType.DOCUMENT_SHARED,
        {
            "document_id": str(doc.id),
            "name": doc.original_file_name,
            "routing_mode": payload.routing_mode.value,
            "expires_at": (
                payload.expires_at.isoformat() if payload.expires_at else None
            ),
            "notified": [
                {"participant_id": str(p.id), "email": p.email, "role": p.role.value}
                for p in to_notify
            ],
        },
        user_id=user.id,
    )

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=(
            f"Sent for signature ({payload.routing_mode.value}) "
            f"to {len(to_notify)} participant(s)"
        ),
        metadata={
            "mode": payload.routing_mode.value,
            "notified": [str(p.id) for p in to_notify],
            "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
        },
    )
    return _build_status_out(doc, snap)


@owner_router.post("/{document_id}/void", response_model=WorkflowStatusOut)
async def void_workflow(
    document_id: UUID,
    payload: VoidWorkflowDto,
    user: User = Depends(get_current_user),
) -> WorkflowStatusOut:
    doc = await _get_owned_doc(document_id, user)
    snap = await void(doc, reason=payload.reason)
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Voided workflow"
        + (f" ({payload.reason})" if payload.reason else ""),
    )
    return _build_status_out(doc, snap)


@owner_router.post("/{document_id}/remind", response_model=MessageOut)
async def remind_pending(
    document_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    """Re-send the invitation email to every participant whose action is
    still pending. For sequential workflows, only the current head-of-queue
    is notified (re-emailing later signers prematurely would be wrong)."""
    doc = await _get_owned_doc(document_id, user)
    if doc.routing_status not in {RoutingStatus.SENT, RoutingStatus.IN_PROGRESS}:
        raise HTTPException(
            400,
            f"Cannot remind on a workflow in state {doc.routing_status.value}",
        )
    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )
    sent_count = 0
    for p in await participants_to_notify(doc):
        try:
            await send_collaboration_invite_email(
                email=p.email,
                document_name=doc.original_file_name or "document",
                sender_name=sender_name,
                role=p.role.value,
                invite_url=_invite_url(p.invite_token),
                personal_message=p.message,
            )
            sent_count += 1
        except Exception as exc:
            logger.warning(f"reminder email failed for {p.email}: {exc}")
    return MessageOut(message=f"Reminded {sent_count} pending participant(s)")


# ---- Signing targets (owner side) ------------------------------------------


def _target_to_out(t: DocumentSigningTarget) -> SigningTargetOut:
    return SigningTargetOut(
        id=t.id,
        document_id=t.document_id,
        participant_id=t.participant_id,
        kind=t.kind,
        page=t.page,
        x=t.x,
        y=t.y,
        width=t.width,
        height=t.height,
        label=t.label,
        sort_order=t.sort_order,
        filled_at=t.filled_at,
        filled_value=t.filled_value,
        created_at=t.created_at,
    )


@owner_router.get(
    "/{document_id}/signing-targets",
    response_model=list[SigningTargetOut],
)
async def list_signing_targets(
    document_id: UUID, user: User = Depends(get_current_user)
) -> list[SigningTargetOut]:
    """List ALL signing targets on the document — used by the editor overlay
    to show every reserved spot the owner has dropped, regardless of which
    participant they belong to."""
    await _get_owned_doc(document_id, user)
    rows = await DocumentSigningTarget.filter(
        document_id=document_id, deleted_at=None
    ).order_by("page", "sort_order", "y")
    return [_target_to_out(t) for t in rows]


@owner_router.post(
    "/{document_id}/signing-targets",
    response_model=list[SigningTargetOut],
)
async def replace_signing_targets(
    document_id: UUID,
    payload: list[SigningTargetIn],
    user: User = Depends(get_current_user),
) -> list[SigningTargetOut]:
    """Replace the document's full set of signing targets.

    Editor convention: when the owner edits the layout we want to send the
    complete state, not deltas. Cleaner than tracking per-target dirty flags.
    """
    doc = await _get_owned_doc(document_id, user)
    # Validate every participant in the payload belongs to this document.
    pids = {p.participant_id for p in payload}
    if pids:
        valid = {
            p.id
            for p in await DocumentParticipant.filter(
                document_id=doc.id, deleted_at=None
            )
        }
        bad = pids - valid
        if bad:
            raise HTTPException(
                400,
                f"Targets reference participants not on this document: {bad}",
            )

    now = datetime.now(timezone.utc)
    # Soft-delete existing (UNFILLED) targets — keep filled-in ones around as
    # an audit trail of what got stamped.
    await DocumentSigningTarget.filter(
        document_id=doc.id, deleted_at=None, filled_at=None
    ).update(deleted_at=now)
    # Insert the new set.
    created: list[DocumentSigningTarget] = []
    for i, t in enumerate(payload):
        row = await DocumentSigningTarget.create(
            document_id=doc.id,
            participant_id=t.participant_id,
            kind=t.kind,
            page=t.page,
            x=t.x,
            y=t.y,
            width=t.width,
            height=t.height,
            label=t.label,
            sort_order=t.sort_order if t.sort_order is not None else i,
        )
        created.append(row)
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Updated signing targets ({len(created)} total)",
    )
    return [_target_to_out(t) for t in created]


@owner_router.delete(
    "/{document_id}/signing-targets/{target_id}",
    response_model=MessageOut,
)
async def delete_signing_target(
    document_id: UUID,
    target_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    await _get_owned_doc(document_id, user)
    t = await DocumentSigningTarget.get_or_none(
        id=target_id, document_id=document_id, deleted_at=None
    )
    if not t:
        raise HTTPException(404, "Signing target not found")
    t.deleted_at = datetime.now(timezone.utc)
    await t.save()
    return MessageOut(message="Signing target removed")


# =============================================================================
# Guest-side endpoints (no JWT — invite_token is the credential)
# =============================================================================


async def _guest_document_out(
    doc: Document,
    p: DocumentParticipant,
    *,
    is_my_turn: bool,
    sender_name: str | None,
) -> GuestDocumentOut:
    """Build the guest view of the document, including the targets THIS
    participant needs to fill. We never leak other participants' targets."""
    my_targets = await DocumentSigningTarget.filter(
        document_id=doc.id, participant_id=p.id, deleted_at=None
    ).order_by("page", "sort_order", "y")
    return GuestDocumentOut(
        document_id=doc.id,
        original_file_name=doc.original_file_name,
        file_mime_type=doc.file_mime_type,
        file_size=doc.file_size,
        routing_status=doc.routing_status,
        routing_mode=doc.routing_mode,
        sent_at=doc.sent_at,
        expires_at=doc.expires_at,
        completed_at=doc.completed_at,
        sender_name=sender_name,
        me=GuestParticipantOut(
            id=p.id,
            email=p.email,
            name=p.name,
            role=p.role,
            status=p.status,
            sequence_order=p.sequence_order,
            message=p.message,
            is_my_turn=is_my_turn,
        ),
        my_targets=[_target_to_out(t) for t in my_targets],
    )


@guest_router.get("/{invite_token}", response_model=GuestDocumentOut)
async def guest_view(invite_token: str):
    """Resolve a magic link to the document + participant view.

    Returns enough information for the guest landing page to render. We mark
    the participant as VIEWED on first access via this endpoint.
    """
    doc, p = await _get_participant_by_token(invite_token)
    is_my_turn = await is_participant_actionable(doc, p)
    # First-view tracking — only fires when the row hasn't been seen yet.
    if p.first_viewed_at is None:
        await mark_viewed(doc, p)

    # Resolve the sender's display name (the document owner).
    owner = await User.get_or_none(id=doc.user_id, deleted_at=None) if doc.user_id else None
    sender_name = None
    if owner:
        sender_name = (
            " ".join(filter(None, [owner.first_name, owner.last_name])).strip()
            or owner.email
        )
    return await _guest_document_out(
        doc, p, is_my_turn=is_my_turn, sender_name=sender_name
    )


@guest_router.get("/{invite_token}/file")
async def guest_download_file(invite_token: str):
    """Stream the PDF (latest stamped version, falling back to the original)
    for a guest holding a valid invite token."""
    doc, p = await _get_participant_by_token(invite_token)
    # Mark viewed if not already (covers cases where the file is fetched
    # before /shared/{token} is hit — e.g. an inline preview).
    if p.first_viewed_at is None:
        await mark_viewed(doc, p)

    target = doc.completed_file_url or doc.file_url
    if not target or not target.startswith("local://"):
        raise HTTPException(404, "No file available")
    path = UPLOADS_ROOT / target.replace("local://", "", 1)
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(
        str(path),
        media_type=doc.file_mime_type or "application/pdf",
        filename=doc.original_file_name or "document.pdf",
    )


@guest_router.post("/{invite_token}/sign", response_model=WorkflowStatusOut)
async def guest_sign(
    invite_token: str,
    signature_image: UploadFile = File(...),
):
    """Apply the guest's signature to the document and advance the workflow.

    For the MVP guest signing experience, we accept the signature as a PNG
    image upload, stamp it bottom-right of the last page, save the new file,
    and record the participant as SIGNED. A richer placement editor for
    guests (drag-and-drop on the canvas) lives behind a future iteration —
    the current version gets out the door and progresses the workflow.
    """
    doc, p = await _get_participant_by_token(invite_token)
    if not await is_participant_actionable(doc, p):
        # Either the workflow has moved past them, or in SEQUENTIAL mode
        # it's not their turn yet.
        raise HTTPException(
            403, "It's not your turn to sign yet, or the workflow has closed."
        )

    raw = await signature_image.read()
    if not raw:
        raise HTTPException(400, "Signature image is empty")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "Signature image too large (max 5MB)")

    # Stamp the signature onto the latest version of the file and save it.
    target = doc.completed_file_url or doc.file_url
    if not target or not target.startswith("local://"):
        raise HTTPException(404, "Document file is no longer available")
    src_path = UPLOADS_ROOT / target.replace("local://", "", 1)
    if not src_path.exists():
        raise HTTPException(404, "Document file missing on disk")

    pdf_bytes = src_path.read_bytes()

    # If the owner placed targets for THIS participant, use them. Otherwise
    # fall back to a single bottom-right stamp (the original MVP behavior).
    my_targets = await DocumentSigningTarget.filter(
        document_id=doc.id,
        participant_id=p.id,
        deleted_at=None,
        filled_at=None,
    ).order_by("page", "sort_order", "y")
    if my_targets:
        stamped_bytes = _stamp_at_targets(pdf_bytes, raw, my_targets, participant=p)
        fill_time = datetime.now(timezone.utc)
        for t in my_targets:
            t.filled_at = fill_time
            # filled_value: signature → "signed"; date → ISO date; initials
            # → first letters of the participant's name; text → label or
            # name (placeholders are fine for the MVP).
            if t.kind == SigningTargetKind.SIGNATURE:
                t.filled_value = "signed"
            elif t.kind == SigningTargetKind.DATE:
                t.filled_value = fill_time.date().strftime("%d/%m/%Y")
            elif t.kind == SigningTargetKind.INITIALS:
                t.filled_value = _initials_from(p.name or p.email)
            else:
                t.filled_value = p.name or p.email
            await t.save()
    else:
        stamped_bytes = _stamp_guest_signature_bottom_right(pdf_bytes, raw)

    # Save as a NEW completed_file_url — we don't overwrite the previous
    # version so the audit trail keeps each step's artifact.
    rel = f"signed/{doc.id}_{p.id}_{uuid4().hex[:10]}.pdf"
    out_path = UPLOADS_ROOT / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(stamped_bytes)
    doc.completed_file_url = f"local://{rel}"
    doc.file_size = len(stamped_bytes)
    await doc.save()

    try:
        snap = await record_signature(doc, p)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc

    # If SEQUENTIAL and the next signer was just unlocked, send them their
    # invitation email now.
    if doc.routing_mode == RoutingMode.SEQUENTIAL and not snap.is_complete:
        owner = (
            await User.get_or_none(id=doc.user_id, deleted_at=None)
            if doc.user_id else None
        )
        sender_name = (
            (" ".join(filter(None, [owner.first_name, owner.last_name])).strip()
             or owner.email)
            if owner else "Someone"
        )
        for next_p in await participants_to_notify(doc):
            try:
                await send_collaboration_invite_email(
                    email=next_p.email,
                    document_name=doc.original_file_name or "document",
                    sender_name=sender_name,
                    role=next_p.role.value,
                    invite_url=_invite_url(next_p.invite_token),
                    personal_message=next_p.message,
                )
            except Exception as exc:
                logger.warning(
                    f"sequential rollover email failed for {next_p.email}: {exc}"
                )

    await log_audit(
        user_id=p.user_id,  # may be None for unregistered guests
        action=AuditAction.DOCUMENT_SIGNED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Signed by participant {p.email}",
        metadata={"participant_id": str(p.id)},
    )
    # Outbound webhooks. Two fires here: PARTICIPANT_SIGNED (always) and
    # DOCUMENT_COMPLETED when this signature finishes the workflow. Both
    # scoped to the OWNER's user_id so the doc's creator hears about it.
    await dispatch_event(
        WebhookEventType.PARTICIPANT_SIGNED,
        {
            "document_id": str(doc.id),
            "document_name": doc.original_file_name,
            "participant": {
                "id": str(p.id),
                "email": p.email,
                "name": p.name,
                "role": p.role.value,
            },
        },
        user_id=doc.user_id,
    )
    if snap.is_complete:
        await dispatch_event(
            WebhookEventType.DOCUMENT_COMPLETED,
            {
                "document_id": str(doc.id),
                "document_name": doc.original_file_name,
                "completed_at": doc.completed_at.isoformat()
                if doc.completed_at else None,
            },
            user_id=doc.user_id,
        )
    return _build_status_out(doc, snap)


@guest_router.post("/{invite_token}/decline", response_model=WorkflowStatusOut)
async def guest_decline(invite_token: str, payload: DeclineDto):
    doc, p = await _get_participant_by_token(invite_token)
    try:
        snap = await record_decline(doc, p, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    await log_audit(
        user_id=p.user_id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Declined by {p.email}"
        + (f" ({payload.reason})" if payload.reason else ""),
        metadata={"participant_id": str(p.id), "reason": payload.reason},
    )
    await dispatch_event(
        WebhookEventType.PARTICIPANT_DECLINED,
        {
            "document_id": str(doc.id),
            "document_name": doc.original_file_name,
            "participant": {
                "id": str(p.id),
                "email": p.email,
                "role": p.role.value,
            },
            "reason": payload.reason,
        },
        user_id=doc.user_id,
    )
    await dispatch_event(
        WebhookEventType.DOCUMENT_DECLINED,
        {
            "document_id": str(doc.id),
            "document_name": doc.original_file_name,
            "declined_by_email": p.email,
            "reason": payload.reason,
        },
        user_id=doc.user_id,
    )
    return _build_status_out(doc, snap)


# ---- PDF stamping helper for the guest sign flow ---------------------------


def _initials_from(name_or_email: str) -> str:
    """Return up to 3 uppercase initials from a name string.

    "Jane Doe" → "JD". "shadrach@x.com" → "S". Falls back to "?" for empty
    input so the stamper never writes blank text.
    """
    if not name_or_email:
        return "?"
    # Strip the email domain if it's an email.
    base = name_or_email.split("@")[0].replace(".", " ").replace("_", " ")
    parts = [p for p in base.split() if p]
    if not parts:
        return "?"
    letters = "".join(p[0].upper() for p in parts[:3])
    return letters or "?"


def _stamp_at_targets(
    pdf_bytes: bytes,
    sig_png: bytes,
    targets: list[DocumentSigningTarget],
    *,
    participant: DocumentParticipant,
) -> bytes:
    """Apply the participant's signature/initials/date/text to each target.

    SIGNATURE targets receive the uploaded PNG (preserving aspect via
    keep_proportion). INITIALS / DATE / TEXT targets receive computed strings.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    fill_date = datetime.now(timezone.utc).date().strftime("%d/%m/%Y")
    initials = _initials_from(participant.name or participant.email)
    typed_name = participant.name or participant.email

    for t in targets:
        if t.page < 0 or t.page >= len(doc):
            logger.warning(
                f"target page {t.page} out of range for doc {t.document_id}"
            )
            continue
        page = doc[t.page]
        rect = fitz.Rect(t.x, t.y, t.x + t.width, t.y + t.height)

        if t.kind == SigningTargetKind.SIGNATURE:
            page.insert_image(rect, stream=sig_png, keep_proportion=True)
        else:
            # Text-flavored targets. Choose the string per kind.
            text = (
                fill_date if t.kind == SigningTargetKind.DATE
                else initials if t.kind == SigningTargetKind.INITIALS
                else (t.label or typed_name)
            )
            # Pick a font size that fits the height roughly — leaves 4pt
            # breathing room top/bottom. Cap at 18pt so big boxes don't
            # render absurd text.
            fontsize = min(18.0, max(8.0, t.height - 6))
            # Baseline at the BOTTOM of the rect with descender clearance.
            baseline_y = t.y + t.height - max(2.0, fontsize * 0.35)
            page.insert_text(
                (t.x + 2.0, baseline_y),
                text,
                fontsize=fontsize,
                color=(0, 0, 0),
            )
    out = io.BytesIO()
    doc.save(out, deflate=True)
    doc.close()
    return out.getvalue()


def _stamp_guest_signature_bottom_right(
    pdf_bytes: bytes, sig_png: bytes
) -> bytes:
    """Drop the signature image at the bottom-right of the LAST page.

    Conservative placement that works without the guest having to pick a
    spot — enough for the MVP signing flow. Future work: let guests drag
    their signature onto the page in the editor before submitting.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) == 0:
        raise HTTPException(500, "PDF has no pages")
    page = doc[-1]
    page_w = page.rect.width
    page_h = page.rect.height
    # 180×60 box, 40pt from the right + bottom edges.
    sig_w, sig_h = 180.0, 60.0
    x0 = page_w - sig_w - 40
    y0 = page_h - sig_h - 40
    rect = fitz.Rect(x0, y0, x0 + sig_w, y0 + sig_h)
    page.insert_image(rect, stream=sig_png, keep_proportion=True)
    out = io.BytesIO()
    doc.save(out, deflate=True)
    doc.close()
    return out.getvalue()


# Both routers in one module for tight cohesion; main.py mounts each at its
# own prefix. Re-export the pair under common names.
__all__ = ["owner_router", "guest_router"]


# Module-level alias so the existing pattern (`from app.api.routes import
# workflow; app.include_router(workflow.router, ...)`) keeps working for the
# owner side; we mount the guest router under a different prefix separately.
router = owner_router

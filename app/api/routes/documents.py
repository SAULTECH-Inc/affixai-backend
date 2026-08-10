"""Document routes: upload, OCR, auto-fill, signing, sharing, download."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from loguru import logger


from app.common.deps import get_current_user, require_active_subscription
from app.common.services import fastapi_internal
from app.common.services.audit_service import log_audit
from app.common.services.auto_affix import auto_affix_pdf_bytes, auto_affix_document_bytes
from app.common.services.manual_stamp import Placement, restamp_pdf
from app.common.services.email_service import (
    send_document_shared_email,
    send_signed_document_email,
)
from app.common.services.local_storage import (
    save_bytes as local_save_bytes,
    fetch_file_bytes,
    serve_file,
)
from app.common.services.s3_service import s3_service
from app.core.encryption import decrypt
from app.db.models.audit_log import AuditAction
from app.db.models.data_vault import DataVault
from app.db.models.document import Document, DocumentStatus, DocumentType, ProcessingMode
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.document_schemas import (
    AssetOut,
    AutoSignOut,
    AutoSignReport,
    CreateBlankDto,
    DocumentOut,
    DocumentStatsOut,
    DownloadUrlOut,
    EmailDocumentDto,
    EmailDocumentOut,
    PageOpDto,
    PageOpOut,
    PresignedUploadOut,
    ProtectDto,
    RestampDto,
    RestampOut,
    SaveDraftDto,
    SaveDraftOut,
    ShareDocumentDto,
    ShareDocumentOut,
    SignDocumentDto,
    StampedFieldOut,
    UploadDocumentDto,
)

router = APIRouter()


def _resolve_download_url(target: str, filename: str = "file", expires_in: int = 300) -> str:
    """Return a URL the client can use to download a file.

    Handles all three storage backends:
      - Cloudinary (https://res.cloudinary.com/…): server-signed URL so it
        works even when the account has Strict Transformations enabled.
      - S3 keys / s3:// URLs: presigned S3 download URL.
      - local:// pseudo-URLs: returned unchanged (caller must use /file endpoint).
    """
    if target.startswith("https://") or target.startswith("http://"):
        if "cloudinary.com" in target:
            from app.common.services import cloudinary_storage
            return cloudinary_storage.signed_download_url(target, filename=filename)
        return target
    # S3 key or s3:// URL
    return s3_service().get_presigned_url(target, expires_in=expires_in)


def _to_out(doc: Document) -> DocumentOut:
    return DocumentOut.model_validate(doc, from_attributes=True)


async def _process_via_ai(doc: Document) -> None:
    """OCR + field extraction. Internal call to AI services."""
    try:
        url = s3_service().get_presigned_url(doc.file_url, expires_in=3600)
        result = await fastapi_internal.process_document(url, doc.document_type.value)
        # Result shape mirrors OCRProcessResponse in app/models/schemas.py
        if hasattr(result, "model_dump"):
            data = result.model_dump()
        elif isinstance(result, dict):
            data = result
        else:
            data = {"extractedFields": [], "overallConfidence": 0}
        doc.extracted_fields = data.get("extractedFields") or data.get("extracted_fields")
        doc.overall_confidence_score = data.get("overallConfidence") or data.get(
            "overall_confidence"
        )
        doc.status = DocumentStatus.EXTRACTED
    except Exception as exc:
        logger.error(f"process_document failed: {exc}")
        doc.status = DocumentStatus.FAILED
        doc.metadata = {**(doc.metadata or {}), "process_error": str(exc)}
    await doc.save()


@router.get("/fonts")
async def list_fonts() -> list[dict]:
    """Return the font catalog the editor's family picker should render.

    Auth-free on purpose — the catalog is static and used by every editor
    view; gating it behind a token adds latency without security value.
    """
    from app.common.services.manual_stamp import list_available_fonts
    return list_available_fonts()


@router.post("/upload-url", response_model=PresignedUploadOut)
async def get_upload_url(
    payload: UploadDocumentDto, user: User = Depends(get_current_user)
) -> PresignedUploadOut:
    s3 = s3_service()
    presigned = s3.get_presigned_upload_url(payload.file_name, payload.mime_type, "documents")
    doc = await Document.create(
        user_id=user.id,
        file_name=presigned["key"],
        original_file_name=payload.file_name,
        file_url=presigned["key"],
        file_mime_type=payload.mime_type,
        file_size=0,
        document_type=payload.document_type,
        processing_mode=payload.processing_mode,
        is_template=payload.is_template,
        template_name=payload.template_name,
    )
    return PresignedUploadOut(upload_url=presigned["upload_url"], document_id=doc.id)


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    document_type: DocumentType = Form(DocumentType.OTHER),
    processing_mode: ProcessingMode = Form(ProcessingMode.AUTO),
    is_template: bool = Form(False),
    template_name: str | None = Form(None),
    user: User = Depends(get_current_user),
) -> DocumentOut:
    body = await file.read()
    uploaded = local_save_bytes(body, file.filename or "upload.bin")

    doc = await Document.create(
        user_id=user.id,
        file_name=uploaded["key"],
        original_file_name=file.filename or "upload.bin",
        # Use the full URL (https:// for Cloudinary, local:// for local FS).
        # Storing only the key (public_id) breaks serve_file / fetch_file_bytes.
        file_url=uploaded.get("url") or uploaded["key"],
        file_mime_type=file.content_type or "application/octet-stream",
        file_size=len(body),
        document_type=document_type,
        processing_mode=processing_mode,
        is_template=is_template,
        template_name=template_name,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_UPLOADED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Uploaded {doc.original_file_name}",
    )

    if processing_mode == ProcessingMode.AUTO:
        await _process_via_ai(doc)

    return _to_out(doc)


@router.post("/{document_id}/process", response_model=DocumentOut)
async def process_document(
    document_id: UUID, user: User = Depends(get_current_user)
) -> DocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    doc.status = DocumentStatus.PROCESSING
    await doc.save()
    await _process_via_ai(doc)
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_PROCESSED,
        entity_type="document",
        entity_id=str(doc.id),
    )
    return _to_out(doc)


@router.post("/{document_id}/auto-fill", response_model=DocumentOut)
async def auto_fill(
    document_id: UUID, user: User = Depends(get_current_user)
) -> DocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    rows = await DataVault.filter(user_id=user.id, is_active=True, deleted_at=None)
    user_data: dict[str, str] = {}
    for record in rows:
        try:
            user_data[record.field_name] = decrypt(record.encrypted_value)
        except Exception:  # tolerant: skip records we can't decrypt
            continue

    try:
        url = s3_service().get_presigned_url(doc.file_url, expires_in=3600)
        result = await fastapi_internal.auto_fill_document(
            url, doc.document_type.value, user_data
        )
        data = result.model_dump() if hasattr(result, "model_dump") else result
        doc.field_placements = data.get("fieldPlacements") or data.get("field_placements")
        doc.status = DocumentStatus.DRAFT
        await doc.save()
    except Exception as exc:
        logger.error(f"auto_fill failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auto-fill failed"
        ) from exc

    return _to_out(doc)


@router.post("/{document_id}/sign", response_model=DocumentOut)
async def sign_document(
    document_id: UUID,
    payload: SignDocumentDto,
    user: User = Depends(get_current_user),
) -> DocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    entry = {
        "signature_id": str(payload.signature_id),
        "placement": payload.placement,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    existing = list(doc.signature_data or [])
    existing.append(entry)
    doc.signature_data = existing
    doc.status = DocumentStatus.SIGNED
    await doc.save()

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SIGNED,
        entity_type="document",
        entity_id=str(doc.id),
    )
    return _to_out(doc)


@router.post("/{document_id}/complete", response_model=DocumentOut)
async def complete_document(
    document_id: UUID, user: User = Depends(get_current_user)
) -> DocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    doc.status = DocumentStatus.COMPLETED
    doc.completed_at = datetime.now(timezone.utc)
    doc.completed_file_url = doc.file_url  # placeholder; real flattening lands in Phase 4
    await doc.save()
    return _to_out(doc)


@router.post("/{document_id}/share", response_model=ShareDocumentOut)
async def share_document(
    document_id: UUID,
    payload: ShareDocumentDto,
    user: User = Depends(get_current_user),
) -> ShareDocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    token = uuid4().hex
    doc.shareable_link = token
    doc.shareable_link_expiry = datetime.now(timezone.utc) + timedelta(hours=payload.expiry_hours)
    await doc.save()

    from app.core.config import settings as _settings

    share_url = f"{_settings.FRONTEND_URL}/shared/{token}"

    if payload.email:
        sender_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
        await send_document_shared_email(
            payload.email, doc.original_file_name, share_url, sender_name
        )

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Shared with {payload.email or 'link'}",
    )
    return ShareDocumentOut(share_url=share_url)


@router.get("/{document_id}/download", response_model=DownloadUrlOut)
async def download_document(
    document_id: UUID, user: User = Depends(get_current_user)
) -> DownloadUrlOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    target = doc.completed_file_url or doc.file_url
    download_url = _resolve_download_url(
        target,
        filename=doc.original_file_name or "document.pdf",
        expires_in=300,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        entity_type="document",
        entity_id=str(doc.id),
    )
    return DownloadUrlOut(download_url=download_url)


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
    document_type: DocumentType | None = Query(default=None),
    is_template: bool | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> list[DocumentOut]:
    query = Document.filter(user_id=user.id, deleted_at=None)
    if status_filter:
        query = query.filter(status=status_filter)
    if document_type:
        query = query.filter(document_type=document_type)
    if is_template is not None:
        query = query.filter(is_template=is_template)
    rows = await query.order_by("-created_at")
    return [_to_out(d) for d in rows]


@router.get("/stats", response_model=DocumentStatsOut)
async def document_stats(user: User = Depends(get_current_user)) -> DocumentStatsOut:
    total = await Document.filter(user_id=user.id, deleted_at=None).count()
    completed = await Document.filter(
        user_id=user.id, deleted_at=None, status=DocumentStatus.COMPLETED
    ).count()
    processing = await Document.filter(
        user_id=user.id, deleted_at=None, status=DocumentStatus.PROCESSING
    ).count()
    completion_rate = (completed / total * 100) if total else 0.0
    return DocumentStatsOut(
        total=total,
        completed=completed,
        processing=processing,
        completion_rate=round(completion_rate, 2),
    )


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID, user: User = Depends(get_current_user)
) -> DocumentOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _to_out(doc)


@router.delete("/{document_id}", response_model=MessageOut)
async def delete_document(
    document_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    doc.deleted_at = datetime.now(timezone.utc)
    await doc.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DELETED,
        entity_type="document",
        entity_id=str(doc.id),
    )
    return MessageOut(message="Document deleted")


# ---- Phase 4: auto-affix --------------------------------------------------


@router.post("/auto-sign", response_model=AutoSignOut)
async def auto_sign(
    file: UploadFile = File(...),
    user: User = Depends(require_active_subscription),
) -> AutoSignOut:
    """Upload a PDF or Word (.docx/.doc) document to sign.

    The platform detects labelled fields ("First Name:", "DOB:", "Sign here:"),
    matches them to the user's vault, and stamps the values onto the document.
    DOCX/DOC files are converted to PDF first via LibreOffice so the stamp
    pipeline can run; the result is always returned as a signed PDF.

    For each line containing `Label: ...`, the label is matched to a vault
    field via the same alias registry used in Phase 3. Lines containing
    `signature` / `sign here` get the user's default signature image, or a
    typed `full_legal_name` if no image signature exists.

    Returns the new Document row + a report of what was stamped.
    """
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    mime = (file.content_type or "").lower()
    filename_lower = (file.filename or "").lower()
    is_pdf = "pdf" in mime or content[:4] == b"%PDF"
    is_docx = (
        "wordprocessingml" in mime
        or "msword" in mime
        or filename_lower.endswith((".docx", ".doc"))
    )
    if not is_pdf and not is_docx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="auto-sign supports PDF (.pdf) and Word (.docx, .doc) files only",
        )

    # Resolve a clean MIME type to store regardless of what the browser sent.
    stored_mime = "application/pdf" if is_pdf else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Signed result is always a PDF — use a .pdf suffix for the stored filename.
    original_name = file.filename or ("to-sign.pdf" if is_pdf else "to-sign.docx")
    signed_name = (
        original_name.rsplit(".", 1)[0] + ".pdf"
        if not original_name.lower().endswith(".pdf")
        else original_name
    )

    # Persist the original so we always have it.
    original = local_save_bytes(content, original_name, folder="to-sign")
    doc = await Document.create(
        user_id=user.id,
        file_name=original["key"],
        original_file_name=original_name,
        file_url=original["url"],
        file_mime_type=stored_mime,
        file_size=len(content),
        document_type=DocumentType.FORM,
        status=DocumentStatus.PROCESSING,
        processing_mode=ProcessingMode.AUTO,
        metadata={"purpose": "auto_sign", "source_format": "docx" if is_docx else "pdf"},
    )

    try:
        # Pass `stored_mime`, not the browser's `content_type` — browsers send
        # "application/octet-stream" for .docx often enough that trusting it
        # would fail the format check we just resolved from filename + magic
        # bytes.
        stamped_bytes, report = await auto_affix_document_bytes(
            content, stored_mime, user.id
        )
    except RuntimeError as exc:
        # LibreOffice not installed — surface as 501
        logger.warning(f"auto-sign: LibreOffice missing: {exc}")
        doc.status = DocumentStatus.FAILED
        doc.metadata = {**(doc.metadata or {}), "auto_sign_error": str(exc)}
        await doc.save()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("auto-sign failed")
        doc.status = DocumentStatus.FAILED
        doc.metadata = {**(doc.metadata or {}), "auto_sign_error": str(exc)}
        await doc.save()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"auto-sign failed: {exc}",
        ) from exc

    stored = local_save_bytes(
        stamped_bytes,
        f"signed-{signed_name}",
        folder="signed",
    )

    doc.completed_file_url = stored["url"]
    doc.completed_at = datetime.now(timezone.utc)
    doc.status = (
        DocumentStatus.COMPLETED if not report.error else DocumentStatus.FAILED
    )
    doc.field_placements = [
        # Editor-save shape with `kind` so the editor's normaliser picks them
        # up directly as text-kind overlays on next open. Font props are
        # whatever auto-affix sniffed from the surrounding doc text.
        {
            "kind": "text",
            "field_name": f.field_name,
            "value": f.value,
            "page": f.page,
            "x": f.x,
            "y": f.y,
            "fontsize": f.fontsize,
            "font_family": f.font_family,
            "bold": f.bold,
            "italic": f.italic,
            "color": f.color,
            "width": 160,
            "height": max(f.fontsize + 4, 18),
            "confidence": f.match_confidence,
        }
        for f in report.fields_filled
    ] + [
        # Signature / photo placements so the editor reloads them as
        # draggable overlays AND a re-save preserves them via /restamp.
        {
            "kind": img.kind,
            "page": img.page,
            "x": img.x,
            "y": img.y,
            "width": img.width,
            "height": img.height,
        }
        for img in report.image_placements
    ]
    doc.metadata = {
        **(doc.metadata or {}),
        "auto_sign_report": {
            "fields_filled_count": len(report.fields_filled),
            "labels_unmatched": report.labels_unmatched,
            "signatures_placed": report.signatures_placed,
            "initials_placed": report.initials_placed,
            "photos_placed": report.photos_placed,
        },
    }
    await doc.save()

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SIGNED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Auto-signed via vault",
        metadata={
            "fields_filled": len(report.fields_filled),
            "signatures_placed": report.signatures_placed,
            "photos_placed": report.photos_placed,
        },
    )

    return AutoSignOut(
        document_id=doc.id,
        download_url=_resolve_download_url(
            stored["url"],
            filename=f"signed-{signed_name}",
        ),
        report=AutoSignReport(
            fields_filled=[
                StampedFieldOut(
                    label=f.label,
                    field_name=f.field_name,
                    segment=f.segment.value,
                    value=f.value,
                    page=f.page,
                    x=f.x,
                    y=f.y,
                    match_confidence=f.match_confidence,
                )
                for f in report.fields_filled
            ],
            labels_unmatched=report.labels_unmatched,
            signatures_placed=report.signatures_placed,
            initials_placed=report.initials_placed,
            photos_placed=report.photos_placed,
            pages=report.pages,
            error=report.error,
        ),
    )


@router.get("/{document_id}/file")
async def stream_document_file(
    document_id: UUID,
    user: User = Depends(get_current_user),
):
    """Stream the signed (or original) PDF bytes for a document the user owns.

    Prefers `completed_file_url` (signed version) when present, falls back to the
    original upload. Only `local://` URLs are served — S3 docs should use the
    presigned-URL endpoint instead.
    """
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    target = doc.completed_file_url or doc.file_url
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No file available for this document",
        )

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        entity_type="document",
        entity_id=str(doc.id),
    )

    # Proxy bytes through the backend instead of redirecting.
    # Cloudinary private_download_url redirects route through api.cloudinary.com
    # which does not send CORS headers the browser needs, so a direct redirect
    # causes the frontend fetch to fail. Fetching server-side avoids that.
    file_bytes = await fetch_file_bytes(target)
    safe_name = (doc.original_file_name or "document.pdf").replace('"', "")
    return Response(
        content=file_bytes,
        media_type=doc.file_mime_type or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


# ---- Authoring: blank documents + page operations --------------------------


def _document_metadata(doc: Document) -> dict:
    """`Document.metadata` as a dict.

    The column is a JSONField, so it can legitimately hold a list. Every caller
    here wants key/value access, and treating a stray list as "no metadata" is
    safer than raising mid-request.
    """
    meta = doc.metadata
    return meta if isinstance(meta, dict) else {}


def _document_assets(doc: Document) -> dict[str, str]:
    """asset_id → storage URL for images uploaded to this document.

    This is the only source of URLs the renderer is allowed to use. Clients send
    asset ids; resolving them here means a crafted placement cannot point the
    server at an internal address or a path outside the uploads root.
    """
    raw = _document_metadata(doc).get("assets")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for aid, entry in raw.items():
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            out[str(aid)] = entry["url"]
    return out


# Images we accept as document assets. Deliberately a closed set of raster
# formats PyMuPDF can embed — notably excluding SVG, which is an XML document
# with script and external-entity surface, not an image.
_ASSET_MIME_BY_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
]
_ASSET_MAX_BYTES = 10 * 1024 * 1024
_ASSET_MAX_PER_DOC = 50


@router.post("/{document_id}/assets", response_model=AssetOut)
async def upload_document_asset(
    document_id: UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> AssetOut:
    """Upload an image for use in an `image` placement.

    Returns an `asset_id`. The client puts that id on a placement; it never
    handles or supplies a URL.

    The format is decided by magic bytes rather than the declared content type,
    since the declared type is attacker-controlled and we hand these bytes to an
    image decoder.
    """
    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")
    if len(content) > _ASSET_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Images must be {_ASSET_MAX_BYTES // (1024 * 1024)}MB or smaller.",
        )

    mime = next(
        (m for magic, m in _ASSET_MIME_BY_MAGIC if content.startswith(magic)), None
    )
    if not mime:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported image. Use PNG, JPEG, GIF or BMP.",
        )

    existing = _document_assets(doc)
    if len(existing) >= _ASSET_MAX_PER_DOC:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This document already has {_ASSET_MAX_PER_DOC} images.",
        )

    # Confirm it actually decodes before we accept it — a file with the right
    # magic bytes but corrupt content would otherwise fail later, during a
    # render, where the error is far less obvious.
    width = height = None
    try:
        from PIL import Image as _Image

        with _Image.open(io.BytesIO(content)) as im:
            width, height = im.size
            im.verify()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"That image could not be read: {exc}",
        ) from exc

    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/bmp": "bmp"}[mime]
    stored = local_save_bytes(content, f"asset.{ext}", folder="assets")
    asset_id = uuid4().hex

    meta = _document_metadata(doc)
    assets = dict(meta.get("assets") or {})
    assets[asset_id] = {
        "url": stored["url"],
        "mime_type": mime,
        "size": len(content),
        "width": width,
        "height": height,
    }
    doc.metadata = {**meta, "assets": assets}
    await doc.save()

    return AssetOut(
        asset_id=asset_id, mime_type=mime, size=len(content),
        width=width, height=height,
    )


@router.post("/{document_id}/protect")
async def protect_document(
    document_id: UUID,
    dto: ProtectDto,
    user: User = Depends(get_current_user),
):
    """Return the document encrypted with AES-256, password-protected.

    Streams the encrypted bytes back rather than storing them: the password is
    the user's, and persisting either it or a copy the platform can't open adds
    risk without adding value.
    """
    from app.common.services.manual_stamp import protect_pdf

    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    url = doc.completed_file_url or doc.file_url
    if not url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No file available for this document"
        )
    source = await fetch_file_bytes(url)

    try:
        out_bytes = protect_pdf(
            source,
            user_password=dto.password,
            owner_password=dto.owner_password,
            allow_printing=dto.allow_printing,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("protect failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not protect the document: {exc}",
        ) from exc

    base = (doc.original_file_name or "document").rsplit(".", 1)[0]
    safe_base = "".join(c for c in base if c not in '"\\\r\n') or "document"
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Downloaded password-protected copy",
    )
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_base}-protected.pdf"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.post("/blank", response_model=DocumentOut)
async def create_blank_document(
    dto: CreateBlankDto,
    user: User = Depends(get_current_user),
) -> DocumentOut:
    """Start a document from a blank page instead of an upload.

    The result is an ordinary Document row, so it flows straight into the
    existing placement editor, signing and audit paths — the only difference is
    `metadata.origin`, which records that the user authored it rather than
    uploaded it.
    """
    from app.common.services.pdf_authoring import create_blank_pdf

    try:
        pdf_bytes = create_blank_pdf(
            page_size=dto.page_size, pages=dto.pages, landscape=dto.landscape
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    title = (dto.title or "Untitled document").strip() or "Untitled document"
    file_name = f"{title}.pdf" if not title.lower().endswith(".pdf") else title
    stored = local_save_bytes(pdf_bytes, file_name, folder="authored")

    doc = await Document.create(
        user_id=user.id,
        file_name=stored["key"],
        original_file_name=file_name,
        file_url=stored["url"],
        file_mime_type="application/pdf",
        file_size=len(pdf_bytes),
        document_type=dto.document_type,
        # DRAFT, not UPLOADED: there is nothing to extract from a blank page,
        # and the Documents page's Drafts filter is where the user will look
        # for something they started but haven't finished.
        status=DocumentStatus.DRAFT,
        processing_mode=ProcessingMode.MANUAL,
        field_placements=[],
        metadata={
            "origin": "created",
            "page_size": dto.page_size,
            "landscape": dto.landscape,
            "page_count": dto.pages,
        },
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_UPLOADED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Created blank document ({dto.pages} × {dto.page_size})",
    )
    return _to_out(doc)


@router.post("/{document_id}/pages", response_model=PageOpOut)
async def edit_document_pages(
    document_id: UUID,
    dto: PageOpDto,
    user: User = Depends(get_current_user),
) -> PageOpOut:
    """Add, duplicate, delete, reorder or rotate pages.

    Placements are migrated through the operation's index map, so anything the
    user has already positioned follows its page. Placements on a deleted page
    are dropped — nothing else can be done with them — and the count is returned
    so the editor can tell the user what happened.
    """
    from app.common.services import pdf_authoring as pa

    doc = await Document.get_or_none(id=document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Operate on the working file. A signed/completed render is a finished
    # artifact; re-paginating under it would invalidate the signature layout.
    if doc.completed_file_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This document has already been rendered. Duplicate it to keep editing pages.",
        )
    if not doc.file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No file available for this document"
        )

    source = await fetch_file_bytes(doc.file_url)
    before = list(doc.field_placements or [])

    try:
        if dto.op == "add":
            out_bytes, index_map = pa.add_pages(
                source, at=dto.at, count=dto.count,
                page_size=dto.page_size, landscape=dto.landscape,
            )
        elif dto.op == "duplicate":
            if dto.index is None:
                raise ValueError("duplicate needs an `index`.")
            out_bytes, index_map = pa.duplicate_page(source, dto.index)
        elif dto.op == "delete":
            if dto.index is None:
                raise ValueError("delete needs an `index`.")
            out_bytes, index_map = pa.delete_page(source, dto.index)
        elif dto.op == "reorder":
            if not dto.order:
                raise ValueError("reorder needs an `order` list.")
            out_bytes, index_map = pa.reorder_pages(source, dto.order)
        else:  # rotate
            if dto.index is None:
                raise ValueError("rotate needs an `index`.")
            out_bytes, index_map = pa.rotate_page(source, dto.index, dto.degrees)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("page operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Page operation failed: {exc}",
        ) from exc

    after = pa.remap_placements(before, index_map)

    stored = local_save_bytes(out_bytes, doc.original_file_name or "document.pdf", folder="authored")
    doc.file_url = stored["url"]
    doc.file_name = stored["key"]
    doc.file_size = len(out_bytes)
    doc.field_placements = after
    doc.status = DocumentStatus.DRAFT
    pages_now = pa.page_count(out_bytes)
    doc.metadata = {**(doc.metadata or {}), "page_count": pages_now}
    await doc.save()

    return PageOpOut(
        document_id=doc.id,
        page_count=pages_now,
        placements_kept=len(after),
        placements_dropped=max(0, len(before) - len(after)),
    )


# ---- Document processing layer (Phase B): format conversion + extraction ---


@router.post("/convert")
async def convert_uploaded_document(
    file: UploadFile = File(...),
    target_format: str = Form(...),
    user: User = Depends(get_current_user),
):
    """One-shot, stateless format conversion.

    Unlike `GET /{document_id}/download/{target_format}`, this endpoint takes
    the file directly in the request and streams the converted bytes straight
    back — no Document row is created and nothing is persisted. The source
    format is detected from the upload's filename + MIME type.

    Available to any logged-in user (no `require_active_subscription`): format
    conversion is a utility, not a metered signing operation.
    """
    from app.common.services.document_processing import (
        MIME_BY_FORMAT,
        DocFormat,
        convert_document,
        detect_format,
    )
    from app.core.config import settings as _settings

    target = (target_format or "").lower().strip()
    if target not in {f.value for f in DocFormat}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported target format: {target_format!r}. Use pdf, docx, txt, or md.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    max_bytes = _settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {_settings.MAX_FILE_SIZE_MB}MB limit",
        )

    src = detect_format(file.filename, file.content_type)
    if src is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Could not detect the source format. Supported inputs: "
                ".pdf, .docx, .doc, .txt, .md"
            ),
        )
    if src.value == target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Source and target are both {target} — nothing to convert.",
        )

    try:
        out_bytes = convert_document(content, src.value, target)  # type: ignore[arg-type]
    except RuntimeError as exc:
        # External-tool dependency missing (LibreOffice for docx → pdf).
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("standalone conversion failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conversion failed: {exc}",
        ) from exc

    base = (file.filename or "document").rsplit(".", 1)[0] or "document"
    # Strip characters that would break the Content-Disposition header.
    safe_base = "".join(c for c in base if c not in '"\\\r\n')
    download_name = f"{safe_base}.{target}"

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        entity_type="conversion",
        description=f"Converted {src.value} → {target}",
        metadata={"from": src.value, "to": target, "size": len(out_bytes)},
    )

    return Response(
        content=out_bytes,
        media_type=MIME_BY_FORMAT.get(target, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            # Lets the browser read the filename off a fetch/XHR response.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/{document_id}/download/{target_format}")
async def download_document_as(
    document_id: UUID,
    target_format: str,
    user: User = Depends(get_current_user),
):
    """Download the document converted to a different format.

    `target_format` is one of: `pdf`, `docx`, `txt`, `md`. The source format
    is detected from the stored mime type / filename. Conversion runs
    on-the-fly (no caching) — for very large documents this may take a few
    seconds; we cap at 2 minutes via the LibreOffice timeout.
    """
    from app.common.services.document_processing import (
        DocFormat,
        MIME_BY_FORMAT,
        convert_document,
        detect_format,
    )

    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    target = (target_format or "").lower().strip()
    if target not in {f.value for f in DocFormat}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {target_format!r}. Use pdf, docx, txt, or md.",
        )

    # Resolve the source bytes — prefer the completed/signed version if any.
    url = doc.completed_file_url or doc.file_url
    if not url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No file available for this document",
        )
    source_bytes = await fetch_file_bytes(url)

    src = detect_format(doc.original_file_name, doc.file_mime_type)
    if src is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not detect source format from filename/mime type",
        )

    try:
        out_bytes = convert_document(source_bytes, src.value, target)  # type: ignore[arg-type]
    except RuntimeError as exc:
        # External-tool dependency missing (LibreOffice for docx → pdf).
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("conversion failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conversion failed: {exc}",
        ) from exc

    # Replace the source extension with the target one for the download name.
    base = (doc.original_file_name or "document").rsplit(".", 1)[0]
    download_name = f"{base}.{target}"

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Converted to {target}",
        metadata={"from": src.value, "to": target, "size": len(out_bytes)},
    )

    return Response(
        content=out_bytes,
        media_type=MIME_BY_FORMAT.get(target, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
        },
    )


@router.get("/{document_id}/text")
async def extract_document_text(
    document_id: UUID,
    user: User = Depends(get_current_user),
) -> dict:
    """Return the document's plain text, with OCR fallback if needed.

    Useful for: indexing, search, workflow extraction, collaboration preview.
    Response: `{ text: str, source_format: str, ocr_used: bool }`.
    """
    from app.common.services.document_processing import (
        detect_format,
        extract_text_from_docx,
        extract_text_from_pdf,
    )

    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    url = doc.completed_file_url or doc.file_url
    if not url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No file available"
        )
    file_bytes = await fetch_file_bytes(url)

    src = detect_format(doc.original_file_name, doc.file_mime_type)
    text = ""
    if src and src.value == "pdf":
        text = extract_text_from_pdf(file_bytes, ocr_fallback=True)
    elif src and src.value == "docx":
        text = extract_text_from_docx(file_bytes)
    elif src and src.value in ("txt", "md"):
        text = file_bytes.decode("utf-8", errors="replace")
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported source format for text extraction",
        )
    return {
        "text": text,
        "source_format": src.value if src else "unknown",
        "char_count": len(text),
    }


# ---- Live editor: serve the un-stamped original + restamp endpoint --------


@router.get("/{document_id}/file/original")
async def stream_document_original(
    document_id: UUID,
    user: User = Depends(get_current_user),
):
    """Stream the *original* (un-stamped) PDF so the editor can render it.

    The editor overlays its draggable placements on top of this clean version
    so re-editing is non-destructive — each save recomputes from the original
    and the latest placements list.
    """
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not doc.file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No file available"
        )
    return serve_file(
        doc.file_url,
        media_type="application/pdf",
        filename=doc.original_file_name or "document.pdf",
    )


@router.post("/{document_id}/restamp", response_model=RestampOut)
async def restamp_document(
    document_id: UUID,
    payload: RestampDto,
    user: User = Depends(require_active_subscription),
) -> RestampOut:
    """Re-stamp the document with the editor's full placement list.

    Replaces the previously-completed file. The original (`file_url`) is the
    source of truth so repeated edits don't accumulate artifacts.
    """
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not doc.file_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Original PDF unavailable"
        )
    pdf_bytes = await fetch_file_bytes(doc.file_url)

    placements = [
        Placement(
            kind=p.kind,  # type: ignore[arg-type]
            page=p.page,
            x=p.x,
            y=p.y,
            value=p.value,
            field_name=p.field_name,
            fontsize=p.fontsize,
            width=p.width,
            height=p.height,
            font_family=p.font_family,
            bold=p.bold,
            italic=p.italic,
            color=p.color,
            asset_id=p.asset_id,
            url=p.url,
            html=p.html,
            rows=p.rows,
            header=p.header,
            col_widths=p.col_widths,
            border_color=p.border_color,
            align=p.align,
            keep_proportion=p.keep_proportion,
        )
        for p in payload.placements
    ]

    # The asset map comes from the server's own record for THIS document, never
    # from the request — see restamp_pdf's docstring on why that matters.
    assets = _document_assets(doc)
    stamped_bytes, outcome = await restamp_pdf(
        pdf_bytes, placements, user.id, assets=assets
    )

    stored = local_save_bytes(
        stamped_bytes,
        f"signed-{doc.original_file_name or 'document.pdf'}",
        folder="signed",
    )

    doc.completed_file_url = stored["url"]
    doc.completed_at = datetime.now(timezone.utc)
    doc.status = DocumentStatus.COMPLETED if not outcome.errors else doc.status
    # Persist the placement list so subsequent edits start from the same state.
    doc.field_placements = [p.model_dump() for p in payload.placements]
    doc.metadata = {
        **(doc.metadata or {}),
        "last_restamp": {
            "placed": outcome.placed,
            "failed": outcome.failed,
            "errors": outcome.errors,
        },
    }
    await doc.save()

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SIGNED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Re-stamped via editor",
        metadata={"placed": outcome.placed, "failed": outcome.failed},
    )

    return RestampOut(
        document_id=doc.id,
        download_url=_resolve_download_url(
            stored["url"],
            filename=f"signed-{doc.original_file_name or 'document.pdf'}",
        ),
        placed=outcome.placed,
        failed=outcome.failed,
        errors=outcome.errors,
    )


# ---- Draft auto-save -------------------------------------------------------


@router.put("/{document_id}/placements", response_model=SaveDraftOut)
async def save_placements_draft(
    document_id: UUID,
    payload: SaveDraftDto,
    user: User = Depends(get_current_user),
) -> SaveDraftOut:
    """Persist the editor's placement list WITHOUT re-rendering the PDF.

    Used by the editor's debounced auto-save so a browser/PC crash mid-edit
    doesn't lose work. On next open the editor pre-loads these placements.

    Side-effect: flips DocumentStatus to DRAFT so the row shows up in the
    Drafts filter on the Documents page with a "Resume editing" CTA. We do
    NOT flip terminal states (ARCHIVED) — those are intentionally locked.
    Going COMPLETED → DRAFT is allowed because the user opened the editor
    and made new changes that aren't reflected in the rendered PDF yet —
    the doc is genuinely back in draft.
    """
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if doc.status == DocumentStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is archived — restore it before editing",
        )

    doc.field_placements = [p.model_dump() for p in payload.placements]
    doc.status = DocumentStatus.DRAFT
    doc.metadata = {
        **(doc.metadata or {}),
        "draft_saved_at": datetime.now(timezone.utc).isoformat(),
    }
    await doc.save()

    return SaveDraftOut(
        saved_at=datetime.now(timezone.utc).isoformat(),
        placement_count=len(payload.placements),
    )


# ---- Email signed document ------------------------------------------------


@router.post("/{document_id}/email", response_model=EmailDocumentOut)
async def email_document(
    document_id: UUID,
    payload: EmailDocumentDto,
    user: User = Depends(get_current_user),
) -> EmailDocumentOut:
    """Email the signed PDF to a recipient as an attachment."""
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    target = doc.completed_file_url or doc.file_url
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document file isn't available to send",
        )
    pdf_bytes = await fetch_file_bytes(target)
    document_name = doc.original_file_name or "document.pdf"
    sender_name = (
        " ".join(filter(None, [user.first_name, user.last_name])).strip()
        or user.email
    )

    try:
        await send_signed_document_email(
            to=payload.to,
            document_name=document_name,
            pdf_bytes=pdf_bytes,
            sender_name=sender_name,
            subject=payload.subject,
            message=payload.message,
        )
    except Exception as exc:
        logger.exception("email send failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send email: {exc}",
        ) from exc

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_SHARED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Emailed to {payload.to}",
        metadata={"to": payload.to},
    )

    return EmailDocumentOut(sent_to=payload.to, document_name=document_name)

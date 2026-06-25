"""Data Vault routes — segment-oriented predefined fields.

Endpoints:
- GET  /schema                          → field registry (forms metadata)
- GET  /segments                        → all segments + their values
- GET  /segments/{segment}              → one segment's values
- PUT  /segments/{segment}              → bulk upsert {field_name: value}
- DELETE /segments/{segment}/{field}    → clear one field
- DELETE /segments/{segment}            → clear an entire segment
- GET  /records                         → flat list of all rows (advanced)
- POST /extract                         → upload doc, get OCR'd field preview
- POST /extract/apply                   → save extracted fields to the vault
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.common.deps import get_current_user, require_active_subscription
from app.common.services.audit_service import log_audit
from app.common.services.document_extraction import extract_vault_preview
from app.common.services.local_storage import save_bytes
from app.common.vault_schema import (
    FIELD_REGISTRY,
    SEGMENT_LABELS,
    VaultSegment,
    field_names_for,
    is_valid_field,
    serialize_registry,
)
from app.core.encryption import decrypt, encrypt
from app.db.models.audit_log import AuditAction
from app.db.models.data_vault import DataSource, DataVault
from app.db.models.document import Document, DocumentStatus, DocumentType, ProcessingMode
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.data_vault_schemas import (
    ApplyExtractedDto,
    ExtractPreviewOut,
    FieldValueOut,
    SegmentOut,
    SegmentRegistryEntry,
    SegmentUpsertDto,
    VaultRecordOut,
)

router = APIRouter()


def _row_to_value(record: DataVault) -> FieldValueOut:
    return FieldValueOut(
        value=decrypt(record.encrypted_value),
        source=record.source,
        source_document_id=record.source_document_id,
        confidence_score=record.confidence_score,
        is_verified=record.is_verified,
        updated_at=record.updated_at,
    )


def _row_to_record(record: DataVault) -> VaultRecordOut:
    return VaultRecordOut(
        id=record.id,
        user_id=record.user_id,
        segment=VaultSegment(record.segment),
        field_name=record.field_name,
        value=decrypt(record.encrypted_value),
        source=record.source,
        source_document_id=record.source_document_id,
        confidence_score=record.confidence_score,
        is_verified=record.is_verified,
        metadata=record.metadata,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/schema", response_model=list[SegmentRegistryEntry])
async def get_schema(_: User = Depends(get_current_user)) -> list:
    """Return the predefined field registry the frontend should render."""
    return serialize_registry()


@router.get("/segments", response_model=list[SegmentOut])
async def get_all_segments(user: User = Depends(get_current_user)) -> list[SegmentOut]:
    rows = await DataVault.filter(user_id=user.id, is_active=True, deleted_at=None)
    by_segment: dict[str, list[DataVault]] = {s.value: [] for s in VaultSegment}
    for row in rows:
        by_segment.setdefault(row.segment, []).append(row)

    out: list[SegmentOut] = []
    for segment in VaultSegment:
        values: dict[str, FieldValueOut] = {}
        for row in by_segment.get(segment.value, []):
            try:
                values[row.field_name] = _row_to_value(row)
            except Exception:
                continue
        out.append(SegmentOut(segment=segment, label=SEGMENT_LABELS[segment], fields=values))
    return out


@router.get("/segments/{segment}", response_model=SegmentOut)
async def get_segment(
    segment: VaultSegment, user: User = Depends(get_current_user)
) -> SegmentOut:
    rows = await DataVault.filter(
        user_id=user.id, segment=segment.value, is_active=True, deleted_at=None
    )
    values: dict[str, FieldValueOut] = {}
    for row in rows:
        try:
            values[row.field_name] = _row_to_value(row)
        except Exception:
            continue
    await log_audit(
        user_id=user.id,
        action=AuditAction.DATA_ACCESSED,
        entity_type="vault_segment",
        entity_id=segment.value,
    )
    return SegmentOut(segment=segment, label=SEGMENT_LABELS[segment], fields=values)


@router.put("/segments/{segment}", response_model=SegmentOut)
async def upsert_segment(
    segment: VaultSegment,
    payload: SegmentUpsertDto,
    user: User = Depends(get_current_user),
) -> SegmentOut:
    """Bulk upsert. Unknown field names raise 400. `null` values clear the field."""
    allowed = field_names_for(segment)
    unknown = [name for name in payload.fields if name not in allowed]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown fields for {segment.value}: {unknown}",
        )

    now = datetime.now(timezone.utc)
    changed: list[str] = []
    cleared: list[str] = []

    for field_name, value in payload.fields.items():
        existing = await DataVault.get_or_none(
            user_id=user.id, segment=segment.value, field_name=field_name
        )

        if value is None:
            if existing and existing.is_active:
                existing.is_active = False
                existing.deleted_at = now
                await existing.save()
                cleared.append(field_name)
            continue

        if existing:
            existing.encrypted_value = encrypt(value)
            existing.source = payload.source
            existing.source_document_id = payload.source_document_id
            existing.is_active = True
            existing.deleted_at = None
            # Confidence + verification reset on edit; preserve only if the
            # incoming source is identical to the stored one.
            if existing.source != payload.source:
                existing.is_verified = False
                existing.verified_at = None
                existing.verified_by = None
                existing.confidence_score = None
            await existing.save()
        else:
            await DataVault.create(
                user_id=user.id,
                segment=segment.value,
                field_name=field_name,
                encrypted_value=encrypt(value),
                source=payload.source,
                source_document_id=payload.source_document_id,
            )
        changed.append(field_name)

    await log_audit(
        user_id=user.id,
        action=AuditAction.DATA_UPDATED,
        entity_type="vault_segment",
        entity_id=segment.value,
        description=f"Upserted {len(changed)}, cleared {len(cleared)}",
        metadata={"changed": changed, "cleared": cleared, "source": payload.source.value},
    )

    return await get_segment(segment, user)  # return fresh state


@router.delete("/segments/{segment}/{field_name}", response_model=MessageOut)
async def clear_field(
    segment: VaultSegment,
    field_name: str,
    user: User = Depends(get_current_user),
) -> MessageOut:
    if not is_valid_field(segment, field_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is not a valid field of {segment.value}",
        )
    record = await DataVault.get_or_none(
        user_id=user.id, segment=segment.value, field_name=field_name, is_active=True
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not set")
    record.is_active = False
    record.deleted_at = datetime.now(timezone.utc)
    await record.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.DATA_DELETED,
        entity_type="vault_field",
        entity_id=f"{segment.value}.{field_name}",
    )
    return MessageOut(message="Field cleared")


@router.delete("/segments/{segment}", response_model=MessageOut)
async def clear_segment(
    segment: VaultSegment, user: User = Depends(get_current_user)
) -> MessageOut:
    now = datetime.now(timezone.utc)
    cleared = await DataVault.filter(
        user_id=user.id, segment=segment.value, is_active=True
    ).update(is_active=False, deleted_at=now)
    await log_audit(
        user_id=user.id,
        action=AuditAction.DATA_DELETED,
        entity_type="vault_segment",
        entity_id=segment.value,
        description=f"Cleared {cleared} fields",
    )
    return MessageOut(message=f"Cleared {cleared} fields")


@router.get("/records", response_model=list[VaultRecordOut])
async def list_records(user: User = Depends(get_current_user)) -> list[VaultRecordOut]:
    """Diagnostic flat-row view across all segments."""
    rows = await DataVault.filter(user_id=user.id, is_active=True, deleted_at=None).order_by(
        "segment", "field_name"
    )
    out: list[VaultRecordOut] = []
    for row in rows:
        try:
            out.append(_row_to_record(row))
        except Exception:
            continue
    return out


# ---- Phase 3: document extraction ----


@router.post("/extract", response_model=ExtractPreviewOut)
async def extract_from_document(
    file: UploadFile = File(...),
    segment: VaultSegment | None = Form(default=None),
    document_type: DocumentType = Form(default=DocumentType.OTHER),
    user: User = Depends(require_active_subscription),
) -> ExtractPreviewOut:
    """Upload a document, OCR it, return mapped vault fields for review.

    The result is NOT saved. The frontend renders the preview, the user edits
    as needed, then calls PUT /segments/{segment} (or POST /extract/apply)
    with source=document_extraction to persist.

    Optional `segment` form field narrows the matching to one segment, which
    improves accuracy when the user knows what they're uploading (e.g.
    'extract from file' button inside the Address section).
    """
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    # Persist locally so we have a stable document_id and file_url
    stored = save_bytes(content, file.filename or "upload.bin", folder="vault-extracts")
    doc = await Document.create(
        user_id=user.id,
        file_name=stored["key"],
        original_file_name=file.filename or "upload.bin",
        file_url=stored["url"],
        file_mime_type=file.content_type or "application/octet-stream",
        file_size=len(content),
        document_type=document_type,
        status=DocumentStatus.PROCESSING,
        processing_mode=ProcessingMode.AUTO,
        metadata={"purpose": "vault_extraction"},
    )

    preview = await extract_vault_preview(
        data=content,
        mime_type=file.content_type,
        segment_hint=segment,
    )

    # Persist the structured result on the Document for later reference / Phase 4 reuse.
    doc.extracted_fields = preview
    doc.overall_confidence_score = _avg_segment_confidence(preview["segments"])
    doc.status = DocumentStatus.EXTRACTED if not preview.get("error") else DocumentStatus.FAILED
    await doc.save()

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_PROCESSED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Vault extraction preview",
        metadata={
            "matched_segments": list(preview["segments"].keys()),
            "unmatched_count": len(preview["unmatched"]),
        },
    )

    return ExtractPreviewOut(
        document_id=doc.id,
        segments=preview["segments"],
        unmatched=preview["unmatched"],
        raw_text_preview=preview["raw_text"],
        error=preview.get("error"),
    )


def _avg_segment_confidence(segments: dict[str, dict[str, dict]]) -> float | None:
    """Mean confidence across every extracted field, or None if nothing matched."""
    scores: list[float] = []
    for fields in segments.values():
        for entry in fields.values():
            value = entry.get("confidence")
            if isinstance(value, (int, float)):
                scores.append(float(value))
    return round(sum(scores) / len(scores), 3) if scores else None


@router.post("/extract/apply", response_model=list[SegmentOut])
async def apply_extracted(
    payload: ApplyExtractedDto,
    user: User = Depends(require_active_subscription),
) -> list[SegmentOut]:
    """Persist extracted (and possibly user-edited) fields to the vault in one call.

    Each segment in the payload is upserted with source=document_extraction and
    source_document_id=payload.document_id. Unknown field names → 400. `null`
    values clear the field (matching the segment-PUT contract).
    """
    # Verify the document belongs to the current user
    doc = await Document.get_or_none(id=payload.document_id, user_id=user.id, deleted_at=None)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Source document not found"
        )

    now = datetime.now(timezone.utc)
    applied_segments: list[VaultSegment] = []

    for seg_value, fields_map in payload.segments.items():
        try:
            segment = VaultSegment(seg_value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown segment: {seg_value}",
            )

        allowed = field_names_for(segment)
        unknown = [name for name in fields_map if name not in allowed]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown fields for {segment.value}: {unknown}",
            )

        for field_name, value in fields_map.items():
            existing = await DataVault.get_or_none(
                user_id=user.id, segment=segment.value, field_name=field_name
            )
            if value is None:
                if existing and existing.is_active:
                    existing.is_active = False
                    existing.deleted_at = now
                    await existing.save()
                continue
            if existing:
                existing.encrypted_value = encrypt(value)
                existing.source = DataSource.DOCUMENT_EXTRACTION
                existing.source_document_id = payload.document_id
                existing.is_active = True
                existing.deleted_at = None
                existing.is_verified = False
                existing.verified_at = None
                existing.verified_by = None
                await existing.save()
            else:
                await DataVault.create(
                    user_id=user.id,
                    segment=segment.value,
                    field_name=field_name,
                    encrypted_value=encrypt(value),
                    source=DataSource.DOCUMENT_EXTRACTION,
                    source_document_id=payload.document_id,
                )

        applied_segments.append(segment)

    await log_audit(
        user_id=user.id,
        action=AuditAction.DATA_UPDATED,
        entity_type="document_extraction",
        entity_id=str(payload.document_id),
        metadata={"segments": [s.value for s in applied_segments]},
    )

    # Return the fresh state of the segments the caller touched.
    return [await get_segment(seg, user) for seg in applied_segments]

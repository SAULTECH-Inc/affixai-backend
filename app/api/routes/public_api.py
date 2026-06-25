"""Public API for enterprise integrations. Auth: X-API-Key per enterprise.

Endpoints:
  GET  /users/{user_id}/vault         — read a tenant user's vault snapshot
  GET  /documents/{id}                — fetch Document metadata
  GET  /documents/{id}/file           — download the signed PDF bytes
  POST /sign                          — single document auto-sign (multipart)
  POST /sign/batch                    — batch auto-sign (JSON + base64)

The sign endpoints feed the same auto-affix engine the consumer flow uses,
but with the vault dict supplied inline rather than fetched from the DB. This
is the "send user data alongside each user document as JSON" path the spec
called for.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from loguru import logger

from app.common.deps import get_current_enterprise
from app.common.services.audit_service import log_audit
from app.common.services.auto_affix import auto_affix_with_data
from app.common.services.local_storage import UPLOADS_ROOT, save_bytes as local_save_bytes
from app.common.vault_schema import FIELD_REGISTRY, SEGMENT_LABELS, VaultSegment
from app.core.encryption import decrypt
from app.db.models.api_key import ApiKey
from app.db.models.audit_log import AuditAction
from app.db.models.data_vault import DataVault
from app.db.models.document import Document, DocumentStatus, DocumentType, ProcessingMode
from app.db.models.enterprise import Enterprise
from app.db.models.user import User
from app.models.document_schemas import (
    AutoSignReport,
    DocumentOut,
    StampedFieldOut,
)
from app.models.public_api_schemas import (
    BatchSignDto,
    BatchSignItem,
    BatchSignOut,
    BatchSignResultItem,
    EnterpriseSignOut,
)

router = APIRouter()


# Set of all valid vault field names across every segment — used to filter
# enterprise-supplied user_data so unknown keys don't silently slip through.
_ALL_FIELD_NAMES: set[str] = {
    f["name"] for fields in FIELD_REGISTRY.values() for f in fields
}


def _filter_user_data(user_data: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Split incoming user_data into (known_fields, ignored_keys)."""
    known: dict[str, str] = {}
    ignored: list[str] = []
    for key, value in user_data.items():
        if not isinstance(value, str) or not value.strip():
            ignored.append(key)
            continue
        if key in _ALL_FIELD_NAMES:
            known[key] = value
        else:
            ignored.append(key)
    return known, ignored


def _report_to_out(report) -> AutoSignReport:
    return AutoSignReport(
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
    )


# ---- Reads -----------------------------------------------------------------


@router.get("/users/{user_id}/vault")
async def get_user_vault(
    user_id: UUID,
    auth: tuple[Enterprise, ApiKey] = Depends(get_current_enterprise),
) -> dict:
    """Return the user's predefined-segment vault snapshot."""
    enterprise, _ = auth
    user = await User.get_or_none(id=user_id, enterprise_id=enterprise.id, deleted_at=None)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    rows = await DataVault.filter(user_id=user.id, is_active=True, deleted_at=None)
    by_segment: dict[str, dict[str, str]] = {s.value: {} for s in VaultSegment}
    for row in rows:
        try:
            by_segment.setdefault(row.segment, {})[row.field_name] = decrypt(row.encrypted_value)
        except Exception:
            continue

    return {
        "user_id": str(user.id),
        "segments": [
            {
                "segment": segment.value,
                "label": SEGMENT_LABELS[segment],
                "fields": by_segment.get(segment.value, {}),
            }
            for segment in VaultSegment
        ],
    }


def _enterprise_owns_document(doc: Document, enterprise: Enterprise) -> bool:
    """A Document belongs to the enterprise if its enterprise_id matches OR if
    its owner user is a member of the enterprise. Defends against API keys
    being used to fetch documents that don't belong to that tenant."""
    if doc.enterprise_id == enterprise.id:
        return True
    return False  # user-membership case is checked async; see callers


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID,
    auth: tuple[Enterprise, ApiKey] = Depends(get_current_enterprise),
) -> DocumentOut:
    enterprise, _ = auth
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if _enterprise_owns_document(doc, enterprise):
        return DocumentOut.model_validate(doc, from_attributes=True)

    # Fall back: the doc may belong to a user inside this enterprise.
    if doc.user_id is not None:
        owner = await User.get_or_none(
            id=doc.user_id, enterprise_id=enterprise.id, deleted_at=None
        )
        if owner:
            return DocumentOut.model_validate(doc, from_attributes=True)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


@router.get("/documents/{document_id}/file")
async def download_document_file(
    document_id: UUID,
    auth: tuple[Enterprise, ApiKey] = Depends(get_current_enterprise),
):
    """Stream the signed PDF bytes for an enterprise-owned document."""
    enterprise, _ = auth
    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    is_owner = _enterprise_owns_document(doc, enterprise)
    if not is_owner and doc.user_id is not None:
        owner = await User.get_or_none(
            id=doc.user_id, enterprise_id=enterprise.id, deleted_at=None
        )
        is_owner = owner is not None
    if not is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Prefer the signed/completed version if present; otherwise return the original.
    target_url = doc.completed_file_url or doc.file_url
    if not target_url or not target_url.startswith("local://"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document file is not locally available",
        )
    key = target_url.replace("local://", "", 1)
    path = UPLOADS_ROOT / key
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document file missing"
        )

    return FileResponse(
        str(path),
        media_type=doc.file_mime_type or "application/pdf",
        filename=doc.original_file_name or "document.pdf",
    )


# ---- Sign endpoints --------------------------------------------------------


async def _sign_one(
    *,
    enterprise: Enterprise,
    pdf_bytes: bytes,
    user_data: dict[str, str],
    signature_bytes: bytes | None,
    signature_mime: str | None,
    photo_bytes: bytes | None,
    filename: str,
) -> tuple[Document, AutoSignReport, list[str]]:
    """Core: persist the original, run the engine, persist the result."""
    known, ignored = _filter_user_data(user_data)

    if pdf_bytes[:4] != b"%PDF":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{filename}: not a PDF",
        )

    original = local_save_bytes(pdf_bytes, filename, folder="enterprise-original")
    doc = await Document.create(
        enterprise_id=enterprise.id,
        file_name=original["key"],
        original_file_name=filename,
        file_url=original["url"],
        file_mime_type="application/pdf",
        file_size=len(pdf_bytes),
        document_type=DocumentType.FORM,
        status=DocumentStatus.PROCESSING,
        processing_mode=ProcessingMode.AUTO,
        metadata={"purpose": "enterprise_sign", "ignored_keys": ignored},
    )

    signature_image = (
        (signature_bytes, signature_mime or "image/png") if signature_bytes else None
    )
    try:
        stamped_bytes, report = auto_affix_with_data(
            pdf_bytes=pdf_bytes,
            vault=known,
            signature_image=signature_image,
            photo_bytes=photo_bytes,
        )
    except Exception as exc:
        logger.exception(f"enterprise sign failed for {filename}")
        doc.status = DocumentStatus.FAILED
        doc.metadata = {**(doc.metadata or {}), "sign_error": str(exc)}
        await doc.save()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"sign failed for {filename}: {exc}",
        ) from exc

    stored = local_save_bytes(
        stamped_bytes, f"signed-{filename}", folder="enterprise-signed"
    )
    doc.completed_file_url = stored["url"]
    doc.completed_at = datetime.now(timezone.utc)
    doc.status = DocumentStatus.COMPLETED if not report.error else DocumentStatus.FAILED
    doc.field_placements = [
        {
            "field_name": f.field_name,
            "value": f.value,
            "page": f.page,
            "x": f.x,
            "y": f.y,
            "confidence": f.match_confidence,
        }
        for f in report.fields_filled
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
        enterprise_id=enterprise.id,
        action=AuditAction.DOCUMENT_SIGNED,
        entity_type="document",
        entity_id=str(doc.id),
        description="Enterprise auto-sign",
        metadata={
            "fields_filled": len(report.fields_filled),
            "signatures_placed": report.signatures_placed,
            "photos_placed": report.photos_placed,
            "ignored_keys": ignored,
        },
    )
    return doc, _report_to_out(report), ignored


@router.post("/sign", response_model=EnterpriseSignOut)
async def enterprise_sign(
    file: UploadFile = File(..., description="The PDF to sign."),
    user_data: str = Form(
        ...,
        description=(
            "JSON object mapping vault field names → values. "
            "Use `GET /api/v1/data-vault/schema` for the allowed keys."
        ),
    ),
    signature: UploadFile | None = File(
        default=None,
        description="Optional PNG/JPG signature image to stamp on signature slots.",
    ),
    photo: UploadFile | None = File(
        default=None,
        description="Optional PNG/JPG passport photograph to stamp on PHOTO slots.",
    ),
    auth: tuple[Enterprise, ApiKey] = Depends(get_current_enterprise),
) -> EnterpriseSignOut:
    """Sign one document with the supplied user_data and (optional) signature.

    Returns the persisted Document id and a `download_url` the caller fetches
    via `GET /api/v1/public/documents/{id}/file`.
    """
    enterprise, _ = auth

    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    try:
        parsed = json.loads(user_data)
        if not isinstance(parsed, dict):
            raise ValueError("user_data must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user_data JSON: {exc}",
        ) from exc

    sig_bytes: bytes | None = None
    sig_mime: str | None = None
    if signature is not None:
        sig_bytes = await signature.read()
        sig_mime = signature.content_type

    photo_bytes: bytes | None = None
    if photo is not None:
        photo_bytes = await photo.read()

    doc, report, ignored = await _sign_one(
        enterprise=enterprise,
        pdf_bytes=raw,
        user_data=parsed,
        signature_bytes=sig_bytes,
        signature_mime=sig_mime,
        photo_bytes=photo_bytes,
        filename=file.filename or "document.pdf",
    )

    return EnterpriseSignOut(
        document_id=doc.id,
        download_url=f"/api/v1/public/documents/{doc.id}/file",
        report=report,
        ignored_keys=ignored,
    )


@router.post("/sign/batch", response_model=BatchSignOut)
async def enterprise_sign_batch(
    payload: BatchSignDto,
    auth: tuple[Enterprise, ApiKey] = Depends(get_current_enterprise),
) -> BatchSignOut:
    """Sign multiple documents in one request. Each item is processed
    independently — a failure on one doesn't abort the rest.

    Payload format:
        {
          "items": [
            {
              "filename": "loan-application.pdf",
              "file_base64": "JVBERi0xLjQK...",
              "user_data": {"first_name": "Jane", "last_name": "Doe"},
              "signature_base64": "iVBORw0K...",     // optional
              "reference": "client-1024"             // optional, echoed back
            },
            ...
          ]
        }

    Up to 50 items per request — large batches should be chunked client-side.
    """
    enterprise, _ = auth
    results: list[BatchSignResultItem] = []
    succeeded = failed = 0

    for item in payload.items:
        try:
            pdf_bytes = base64.b64decode(item.file_base64, validate=True)
        except Exception as exc:
            failed += 1
            results.append(
                BatchSignResultItem(
                    reference=item.reference,
                    document_id=None,
                    download_url=None,
                    report=None,
                    error=f"Bad base64 file: {exc}",
                )
            )
            continue

        sig_bytes: bytes | None = None
        if item.signature_base64:
            try:
                sig_bytes = base64.b64decode(item.signature_base64, validate=True)
            except Exception as exc:
                failed += 1
                results.append(
                    BatchSignResultItem(
                        reference=item.reference,
                        error=f"Bad base64 signature: {exc}",
                    )
                )
                continue

        photo_bytes: bytes | None = None
        if item.photo_base64:
            try:
                photo_bytes = base64.b64decode(item.photo_base64, validate=True)
            except Exception as exc:
                failed += 1
                results.append(
                    BatchSignResultItem(
                        reference=item.reference,
                        error=f"Bad base64 photo: {exc}",
                    )
                )
                continue

        try:
            doc, report, ignored = await _sign_one(
                enterprise=enterprise,
                pdf_bytes=pdf_bytes,
                user_data=item.user_data,
                signature_bytes=sig_bytes,
                signature_mime="image/png" if sig_bytes else None,
                photo_bytes=photo_bytes,
                filename=item.filename,
            )
            results.append(
                BatchSignResultItem(
                    reference=item.reference,
                    document_id=doc.id,
                    download_url=f"/api/v1/public/documents/{doc.id}/file",
                    report=report,
                    ignored_keys=ignored,
                )
            )
            succeeded += 1
        except HTTPException as exc:
            failed += 1
            results.append(
                BatchSignResultItem(reference=item.reference, error=str(exc.detail))
            )
        except Exception as exc:
            logger.exception("batch item failed")
            failed += 1
            results.append(
                BatchSignResultItem(reference=item.reference, error=str(exc))
            )

    return BatchSignOut(succeeded=succeeded, failed=failed, items=results)

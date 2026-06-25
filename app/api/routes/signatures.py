"""Signature routes — create (JSON or multipart), list, set default, delete."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.local_storage import save_bytes as local_save_bytes
from app.common.services.signature_processing import remove_signature_background
from app.db.models.audit_log import AuditAction
from app.db.models.signature import Signature, SignatureType
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.signature_schemas import (
    CreateSignatureDto,
    SignatureOut,
    SignatureUrlOut,
    UpdateSignatureDto,
)

router = APIRouter()


def _to_out(sig: Signature) -> SignatureOut:
    return SignatureOut.model_validate(sig, from_attributes=True)


async def _unset_other_defaults(user_id: UUID, except_id: UUID | None = None) -> None:
    query = Signature.filter(user_id=user_id, is_default=True, deleted_at=None)
    if except_id:
        query = query.exclude(id=except_id)
    await query.update(is_default=False)


def _decode_signature_data(signature_data: str) -> tuple[bytes, str]:
    """Accepts either a data URL (`data:image/png;base64,XXX`) or raw base64."""
    match = re.match(r"^data:(image/\w+);base64,(.+)$", signature_data)
    if match:
        return base64.b64decode(match.group(2)), match.group(1)
    return base64.b64decode(signature_data), "image/png"


def _maybe_remove_bg(
    raw: bytes,
    *,
    sig_type: SignatureType,
    remove_background: bool,
    force: bool,
) -> tuple[bytes, bool]:
    """Run bg-removal when appropriate. Returns (bytes, processed?).

    Drawn signatures come from <canvas> with a transparent background already —
    skip the pipeline. Uploaded signatures are the main case. `force` overrides
    the "already has alpha" short-circuit.
    """
    if not remove_background:
        return raw, False
    if sig_type == SignatureType.DRAWN:
        return raw, False

    cleaned = remove_signature_background(raw, force=force)
    if cleaned is None:
        logger.info("bg-removal returned None — using original")
        return raw, False
    return cleaned, True


@router.post("", response_model=SignatureOut, status_code=status.HTTP_201_CREATED)
async def create_signature(
    payload: CreateSignatureDto,
    user: User = Depends(get_current_user),
) -> SignatureOut:
    """Create a signature from base64 data.

    For `type=uploaded`, the bg-removal pipeline runs by default. Pass
    `remove_background: false` to skip it, or `force_background_removal: true`
    to run it even on inputs that already have alpha.
    """
    if not payload.signature_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signature_data is required (use /upload for raw files)",
        )

    raw, _mime = _decode_signature_data(payload.signature_data)
    processed, was_processed = _maybe_remove_bg(
        raw,
        sig_type=payload.type,
        remove_background=payload.remove_background,
        force=payload.force_background_removal,
    )

    stored = local_save_bytes(processed, "signature.png", folder="signatures")

    if payload.is_default:
        await _unset_other_defaults(user.id)

    sig = await Signature.create(
        user_id=user.id,
        type=payload.type,
        signature_url=stored["url"],
        signature_name=payload.signature_name,
        is_default=payload.is_default,
        certificate_id=payload.certificate_id,
        metadata={
            **(payload.metadata or {}),
            "background_removed": was_processed,
        },
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.SIGNATURE_CREATED,
        entity_type="signature",
        entity_id=str(sig.id),
        metadata={"background_removed": was_processed},
    )
    return _to_out(sig)


@router.post("/upload", response_model=SignatureOut, status_code=status.HTTP_201_CREATED)
async def upload_signature(
    file: UploadFile = File(...),
    signature_name: str | None = Form(None),
    is_default: bool = Form(False),
    remove_background: bool = Form(True),
    force_background_removal: bool = Form(False),
    user: User = Depends(get_current_user),
) -> SignatureOut:
    """Upload a raw image file. Always treated as `type=uploaded`."""
    body = await file.read()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    processed, was_processed = _maybe_remove_bg(
        body,
        sig_type=SignatureType.UPLOADED,
        remove_background=remove_background,
        force=force_background_removal,
    )
    stored = local_save_bytes(processed, file.filename or "signature.png", folder="signatures")

    if is_default:
        await _unset_other_defaults(user.id)

    sig = await Signature.create(
        user_id=user.id,
        type=SignatureType.UPLOADED,
        signature_url=stored["url"],
        signature_name=signature_name,
        is_default=is_default,
        metadata={"background_removed": was_processed},
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.SIGNATURE_CREATED,
        entity_type="signature",
        entity_id=str(sig.id),
        metadata={"background_removed": was_processed},
    )
    return _to_out(sig)


@router.post("/preview-bg-removal")
async def preview_bg_removal(
    file: UploadFile = File(...),
    force: bool = Form(False),
    _user: User = Depends(get_current_user),
):
    """Run the bg-removal pipeline without saving — returns the resulting PNG.

    Useful for letting the user see the result and confirm before committing.
    """
    body = await file.read()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )
    processed = remove_signature_background(body, force=force)
    if processed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not isolate a signature from this image",
        )
    return Response(content=processed, media_type="image/png")


@router.get("", response_model=list[SignatureOut])
async def list_signatures(user: User = Depends(get_current_user)) -> list[SignatureOut]:
    rows = await Signature.filter(user_id=user.id, deleted_at=None).order_by(
        "-is_default", "-created_at"
    )
    return [_to_out(s) for s in rows]


@router.get("/default", response_model=SignatureOut)
async def get_default_signature(user: User = Depends(get_current_user)) -> SignatureOut:
    sig = await Signature.get_or_none(
        user_id=user.id, is_default=True, deleted_at=None
    )
    if not sig:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No default")
    return _to_out(sig)


@router.get("/{signature_id}", response_model=SignatureOut)
async def get_signature(
    signature_id: UUID, user: User = Depends(get_current_user)
) -> SignatureOut:
    sig = await Signature.get_or_none(
        id=signature_id, user_id=user.id, deleted_at=None
    )
    if not sig:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _to_out(sig)


@router.get("/{signature_id}/file")
async def get_signature_file(
    signature_id: UUID, user: User = Depends(get_current_user)
):
    """Stream the signature PNG bytes for preview / display in the UI."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from app.common.services.local_storage import UPLOADS_ROOT

    sig = await Signature.get_or_none(
        id=signature_id, user_id=user.id, deleted_at=None
    )
    if not sig or not sig.signature_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not sig.signature_url.startswith("local://"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Signature is not locally stored",
        )
    key = sig.signature_url.replace("local://", "", 1)
    path: Path = UPLOADS_ROOT / key
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Missing on disk")
    return FileResponse(str(path), media_type="image/png")


@router.put("/{signature_id}", response_model=SignatureOut)
async def update_signature(
    signature_id: UUID,
    payload: UpdateSignatureDto,
    user: User = Depends(get_current_user),
) -> SignatureOut:
    sig = await Signature.get_or_none(
        id=signature_id, user_id=user.id, deleted_at=None
    )
    if not sig:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if payload.signature_name is not None:
        sig.signature_name = payload.signature_name
    if payload.metadata is not None:
        sig.metadata = payload.metadata
    if payload.is_default is True:
        await _unset_other_defaults(user.id, except_id=sig.id)
        sig.is_default = True
    elif payload.is_default is False:
        sig.is_default = False

    await sig.save()
    return _to_out(sig)


@router.put("/{signature_id}/set-default", response_model=SignatureOut)
async def set_default_signature(
    signature_id: UUID, user: User = Depends(get_current_user)
) -> SignatureOut:
    sig = await Signature.get_or_none(
        id=signature_id, user_id=user.id, deleted_at=None
    )
    if not sig:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await _unset_other_defaults(user.id, except_id=sig.id)
    sig.is_default = True
    await sig.save()
    return _to_out(sig)


@router.delete("/{signature_id}", response_model=MessageOut)
async def delete_signature(
    signature_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    sig = await Signature.get_or_none(
        id=signature_id, user_id=user.id, deleted_at=None
    )
    if not sig:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    sig.deleted_at = datetime.now(timezone.utc)
    await sig.save()
    await log_audit(
        user_id=user.id,
        action=AuditAction.PERMISSION_REVOKED,
        entity_type="signature",
        entity_id=str(sig.id),
    )
    return MessageOut(message="Signature deleted")

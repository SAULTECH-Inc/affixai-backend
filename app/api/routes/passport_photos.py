"""Passport photo routes — upload (JSON or multipart), list, set default, delete."""
from __future__ import annotations

import base64
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.local_storage import UPLOADS_ROOT, save_bytes as local_save_bytes
from app.db.models.audit_log import AuditAction
from app.db.models.passport_photo import PassportPhoto
from app.db.models.user import User
from app.models.auth_schemas import MessageOut
from app.models.passport_photo_schemas import (
    CreatePassportPhotoDto,
    PassportPhotoOut,
    UpdatePassportPhotoDto,
)

router = APIRouter()


def _to_out(p: PassportPhoto) -> PassportPhotoOut:
    return PassportPhotoOut.model_validate(p, from_attributes=True)


async def _unset_other_defaults(user_id: UUID, except_id: UUID | None = None) -> None:
    q = PassportPhoto.filter(user_id=user_id, is_default=True, deleted_at=None)
    if except_id:
        q = q.exclude(id=except_id)
    await q.update(is_default=False)


def _decode(data: str) -> bytes:
    """Accepts either a data URL or raw base64."""
    m = re.match(r"^data:image/\w+;base64,(.+)$", data)
    return base64.b64decode(m.group(1) if m else data)


def _measure(data: bytes) -> tuple[int | None, int | None]:
    try:
        img = Image.open(io.BytesIO(data))
        return img.width, img.height
    except Exception:
        return None, None


@router.post("", response_model=PassportPhotoOut, status_code=status.HTTP_201_CREATED)
async def create_photo(
    payload: CreatePassportPhotoDto, user: User = Depends(get_current_user)
) -> PassportPhotoOut:
    raw = _decode(payload.photo_data)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty photo data"
        )
    w, h = _measure(raw)
    stored = local_save_bytes(raw, "photo.png", folder="passport-photos")

    if payload.is_default:
        await _unset_other_defaults(user.id)

    photo = await PassportPhoto.create(
        user_id=user.id,
        photo_url=stored["url"],
        name=payload.name,
        is_default=payload.is_default,
        width_px=w,
        height_px=h,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="passport_photo",
        entity_id=str(photo.id),
        description="Passport photo uploaded",
    )
    return _to_out(photo)


@router.post("/upload", response_model=PassportPhotoOut, status_code=status.HTTP_201_CREATED)
async def upload_photo(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    is_default: bool = Form(False),
    user: User = Depends(get_current_user),
) -> PassportPhotoOut:
    body = await file.read()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )
    w, h = _measure(body)
    stored = local_save_bytes(body, file.filename or "photo.png", folder="passport-photos")

    if is_default:
        await _unset_other_defaults(user.id)

    photo = await PassportPhoto.create(
        user_id=user.id,
        photo_url=stored["url"],
        name=name,
        is_default=is_default,
        width_px=w,
        height_px=h,
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.SETTINGS_CHANGED,
        entity_type="passport_photo",
        entity_id=str(photo.id),
        description="Passport photo uploaded",
    )
    return _to_out(photo)


@router.get("", response_model=list[PassportPhotoOut])
async def list_photos(user: User = Depends(get_current_user)) -> list[PassportPhotoOut]:
    rows = await PassportPhoto.filter(user_id=user.id, deleted_at=None).order_by(
        "-is_default", "-created_at"
    )
    return [_to_out(p) for p in rows]


@router.get("/default", response_model=PassportPhotoOut)
async def default_photo(user: User = Depends(get_current_user)) -> PassportPhotoOut:
    photo = await PassportPhoto.get_or_none(
        user_id=user.id, is_default=True, deleted_at=None
    )
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No default")
    return _to_out(photo)


@router.get("/{photo_id}/file")
async def stream_photo(
    photo_id: UUID, user: User = Depends(get_current_user)
):
    photo = await PassportPhoto.get_or_none(
        id=photo_id, user_id=user.id, deleted_at=None
    )
    if not photo or not photo.photo_url.startswith("local://"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    key = photo.photo_url.replace("local://", "", 1)
    path: Path = UPLOADS_ROOT / key
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Missing on disk")
    return FileResponse(str(path))


@router.put("/{photo_id}", response_model=PassportPhotoOut)
async def update_photo(
    photo_id: UUID,
    payload: UpdatePassportPhotoDto,
    user: User = Depends(get_current_user),
) -> PassportPhotoOut:
    photo = await PassportPhoto.get_or_none(
        id=photo_id, user_id=user.id, deleted_at=None
    )
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if payload.name is not None:
        photo.name = payload.name
    if payload.is_default is True:
        await _unset_other_defaults(user.id, except_id=photo.id)
        photo.is_default = True
    elif payload.is_default is False:
        photo.is_default = False
    await photo.save()
    return _to_out(photo)


@router.put("/{photo_id}/set-default", response_model=PassportPhotoOut)
async def set_default(
    photo_id: UUID, user: User = Depends(get_current_user)
) -> PassportPhotoOut:
    photo = await PassportPhoto.get_or_none(
        id=photo_id, user_id=user.id, deleted_at=None
    )
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await _unset_other_defaults(user.id, except_id=photo.id)
    photo.is_default = True
    await photo.save()
    return _to_out(photo)


@router.delete("/{photo_id}", response_model=MessageOut)
async def delete_photo(
    photo_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    photo = await PassportPhoto.get_or_none(
        id=photo_id, user_id=user.id, deleted_at=None
    )
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    photo.deleted_at = datetime.now(timezone.utc)
    await photo.save()
    return MessageOut(message="Photo deleted")

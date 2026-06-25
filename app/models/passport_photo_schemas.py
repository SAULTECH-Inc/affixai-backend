from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreatePassportPhotoDto(BaseModel):
    photo_data: str  # base64 or data-URL
    name: str | None = None
    is_default: bool = False


class UpdatePassportPhotoDto(BaseModel):
    name: str | None = None
    is_default: bool | None = None


class PassportPhotoOut(BaseModel):
    id: UUID
    user_id: UUID
    photo_url: str
    name: str | None
    is_default: bool
    width_px: int | None
    height_px: int | None
    metadata: dict | None
    created_at: datetime
    updated_at: datetime

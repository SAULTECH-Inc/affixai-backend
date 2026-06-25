from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.db.models.signature import SignatureType


class CreateSignatureDto(BaseModel):
    type: SignatureType
    signature_name: str | None = None
    signature_data: str | None = None  # base64 or data-URL
    is_default: bool = False
    certificate_id: str | None = None
    metadata: dict | None = None
    remove_background: bool = True  # only applied when type=uploaded
    force_background_removal: bool = False  # bypass the "already has alpha" guard


class UpdateSignatureDto(BaseModel):
    signature_name: str | None = None
    is_default: bool | None = None
    metadata: dict | None = None


class SignatureOut(BaseModel):
    id: UUID
    user_id: UUID
    type: SignatureType
    signature_url: str
    signature_name: str | None
    is_default: bool
    certificate_id: str | None
    metadata: dict | None
    created_at: datetime
    updated_at: datetime


class SignatureUrlOut(BaseModel):
    url: str

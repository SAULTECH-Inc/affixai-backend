from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.common.vault_schema import VaultSegment
from app.db.models.data_vault import DataSource


class FieldEntry(BaseModel):
    value: str
    source: DataSource = DataSource.USER_INPUT
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    source_document_id: UUID | None = None


class SegmentUpsertDto(BaseModel):
    """Bulk upsert one segment.

    Keys must be valid field names for the segment per the registry. Values are
    plain strings (will be encrypted server-side). Pass `null` for a field to
    clear it.
    """
    fields: dict[str, str | None]
    source: DataSource = DataSource.USER_INPUT
    source_document_id: UUID | None = None


class FieldValueOut(BaseModel):
    value: str
    source: DataSource
    source_document_id: UUID | None
    confidence_score: float | None
    is_verified: bool
    updated_at: datetime


class SegmentOut(BaseModel):
    segment: VaultSegment
    label: str
    fields: dict[str, FieldValueOut]


class FieldRegistryEntry(BaseModel):
    name: str
    label: str
    type: str
    placeholder: str | None = None
    options: list[str] | None = None
    description: str | None = None


class SegmentRegistryEntry(BaseModel):
    segment: VaultSegment
    label: str
    fields: list[FieldRegistryEntry]
    # Multi-entry sections render as a list of cards in the UI (Education,
    # Employment). Single-entry sections render as one form.
    multi_entry: bool = False


class VaultRecordOut(BaseModel):
    """Lower-level row view, for the audit/diagnostic endpoints."""
    id: UUID
    user_id: UUID
    segment: VaultSegment
    field_name: str
    value: str  # decrypted
    source: DataSource
    source_document_id: UUID | None
    confidence_score: float | None
    is_verified: bool
    metadata: dict | None
    created_at: datetime
    updated_at: datetime


# ---- Phase 3: document extraction preview ----


class ExtractedFieldOut(BaseModel):
    """One vault field extracted from a document, with its confidence."""
    value: str
    confidence: float = Field(ge=0, le=1)
    source_label: str


class UnmatchedFieldOut(BaseModel):
    """Key:value pair from OCR that didn't match any vault field — surfaced
    so the user can map it manually in the UI."""
    label: str
    value: str
    confidence: float = Field(ge=0, le=1)


class ExtractPreviewOut(BaseModel):
    """Preview returned by POST /data-vault/extract.

    `document_id` references the saved Document row so the client can include
    it as `source_document_id` when calling PUT /segments/{segment}.

    The preview is NOT saved to the vault — the user reviews values, edits as
    needed, and then PUTs them via the regular save endpoint with
    `source=document_extraction`.
    """
    document_id: UUID
    segments: dict[str, dict[str, ExtractedFieldOut]]
    unmatched: list[UnmatchedFieldOut]
    raw_text_preview: str
    error: str | None = None


class ApplyExtractedDto(BaseModel):
    """Optional convenience save: apply all extracted segments in one call.

    Equivalent to calling PUT /segments/{segment} for each segment with
    source=document_extraction. The client may edit the values before sending.
    """
    document_id: UUID
    segments: dict[str, dict[str, str | None]]  # {segment: {field: value|null}}

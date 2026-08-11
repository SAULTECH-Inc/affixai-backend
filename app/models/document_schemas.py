from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models.document import DocumentStatus, DocumentType, ProcessingMode


class UploadDocumentDto(BaseModel):
    file_name: str
    mime_type: str
    document_type: DocumentType = DocumentType.OTHER
    processing_mode: ProcessingMode = ProcessingMode.AUTO
    is_template: bool = False
    template_name: str | None = None


class PresignedUploadOut(BaseModel):
    upload_url: str
    document_id: UUID


class FieldPlacementDto(BaseModel):
    field_name: str
    x: float
    y: float
    width: float
    height: float
    page: int
    value: str | None = None
    is_locked: bool = False


class UpdateFieldPlacementsDto(BaseModel):
    field_placements: list[FieldPlacementDto]


class SignDocumentDto(BaseModel):
    signature_id: UUID
    placement: dict  # {x, y, page}


class ShareDocumentDto(BaseModel):
    email: str | None = None
    expiry_hours: int = Field(default=24, ge=1, le=720)


class ShareDocumentOut(BaseModel):
    share_url: str


class DownloadUrlOut(BaseModel):
    download_url: str


class DocumentOut(BaseModel):
    id: UUID
    user_id: UUID | None = None        # null for enterprise-direct documents
    enterprise_id: UUID | None = None  # null for consumer-flow documents
    file_name: str
    original_file_name: str
    file_url: str
    file_mime_type: str
    file_size: int
    document_type: DocumentType
    status: DocumentStatus
    processing_mode: ProcessingMode
    template_id: UUID | None
    extracted_fields: dict | list | None
    field_placements: list | None
    overall_confidence_score: float | None
    is_template: bool
    template_name: str | None
    version: int
    parent_document_id: UUID | None
    signature_data: list | None
    completed_file_url: str | None
    completed_at: datetime | None
    shareable_link: str | None
    shareable_link_expiry: datetime | None
    metadata: dict | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class DocumentStatsOut(BaseModel):
    total: int
    completed: int
    processing: int
    completion_rate: float


# ---- Phase 4: auto-affix engine ----


class StampedFieldOut(BaseModel):
    label: str
    field_name: str
    segment: str
    value: str
    page: int
    x: float
    y: float
    match_confidence: float


class AutoSignReport(BaseModel):
    fields_filled: list[StampedFieldOut]
    labels_unmatched: list[str]
    signatures_placed: int
    initials_placed: int
    photos_placed: int = 0
    pages: int
    error: str | None = None


class AutoSignOut(BaseModel):
    document_id: UUID
    download_url: str       # `local://...` (or presigned S3 URL later)
    report: AutoSignReport


# ---- Phase: live placement editor ----


class PlacementDto(BaseModel):
    """One placement the editor wants stamped onto the PDF."""
    kind: str = Field(
        default="text",
        description=(
            "text | number | date | time | initials | signature | photo "
            "| image | link | richtext | table"
        ),
    )
    page: int = Field(ge=0)
    x: float
    y: float
    # Text-ish placements
    value: str | None = None       # if set, overrides field_name lookup
    field_name: str | None = None  # vault field to pull value from
    fontsize: float = 10.0
    # Image-ish placements
    width: float = 180.0
    height: float = 36.0
    # Font customization (text only — ignored for signature/photo)
    font_family: str = Field(default="helv", description="helv | tiro | cour or alias")
    bold: bool = False
    italic: bool = False
    color: str = Field(default="#000000", description="hex like #1a2b3c")

    # ---- Authoring kinds -------------------------------------------------
    # `asset_id` is an id the server issued from the document's asset upload.
    # Deliberately not a URL: the renderer resolves it against the server's own
    # asset record, so a placement can't aim it at an arbitrary address.
    asset_id: str | None = None
    url: str | None = Field(default=None, max_length=2048)   # kind="link"
    html: str | None = Field(default=None, max_length=20000)  # kind="richtext"
    rows: list[list[str]] | None = None                       # kind="table"
    header: bool = True
    col_widths: list[float] | None = None
    border_color: str = "#333333"
    align: str = Field(default="left", description="left | right | center | justify")
    keep_proportion: bool = True


class RestampDto(BaseModel):
    placements: list[PlacementDto] = Field(default_factory=list)


class RestampOut(BaseModel):
    document_id: UUID
    download_url: str
    placed: int
    failed: int
    errors: list[str] = Field(default_factory=list)


class SaveDraftDto(BaseModel):
    """Saves the editor's placement list to Document.field_placements without
    re-rendering the PDF. Used by the editor's debounced auto-save."""
    placements: list[PlacementDto] = Field(default_factory=list)


class SaveDraftOut(BaseModel):
    saved_at: str
    placement_count: int


class EmailDocumentDto(BaseModel):
    to: EmailStr
    subject: str | None = None
    message: str | None = None


class EmailDocumentOut(BaseModel):
    sent_to: str
    document_name: str


# ---- Authoring (blank documents + page operations) -------------------------


class CreateBlankDto(BaseModel):
    """Start a document from a blank page rather than an upload."""
    title: str | None = Field(default=None, max_length=200)
    page_size: str = "a4"
    pages: int = Field(default=1, ge=1, le=200)
    landscape: bool = False
    document_type: DocumentType = DocumentType.OTHER


class PageOpDto(BaseModel):
    """A single page-level edit.

    `op` decides which of the optional fields apply:
      add       — at (None = append), count, page_size, landscape
      duplicate — index
      delete    — index
      reorder   — order (a full permutation of the current page indices)
      rotate    — index, degrees
    """
    op: Literal["add", "duplicate", "delete", "reorder", "rotate"]
    index: int | None = Field(default=None, ge=0)
    at: int | None = Field(default=None, ge=0)
    count: int = Field(default=1, ge=1, le=200)
    page_size: str | None = None
    landscape: bool = False
    order: list[int] | None = None
    degrees: int = 90


class PageOpOut(BaseModel):
    document_id: UUID
    page_count: int
    placements_kept: int
    placements_dropped: int


class AssetOut(BaseModel):
    """An image the server has accepted for use in a document.

    The client references `asset_id` from an image placement. It never supplies
    a URL — the server resolves the id against its own record of what was
    uploaded, so a placement can't point the renderer at an arbitrary address.
    """
    asset_id: str
    mime_type: str
    size: int
    width: int | None = None
    height: int | None = None


class ProtectDto(BaseModel):
    password: str = Field(min_length=4, max_length=128)
    owner_password: str | None = Field(default=None, max_length=128)
    allow_printing: bool = True

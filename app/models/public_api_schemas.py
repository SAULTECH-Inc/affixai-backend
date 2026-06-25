"""Pydantic models for the enterprise public API (Phase 6).

Schemas are deliberately kept narrow — keys passed in `user_data` are filtered
against the vault registry before stamping so unknown labels are returned in
`ignored_keys` rather than silently lost.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.models.document_schemas import AutoSignReport


class EnterpriseSignOut(BaseModel):
    document_id: UUID
    download_url: str
    report: AutoSignReport
    ignored_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Keys present in user_data that don't match any predefined vault "
            "field. Fetch the field registry from GET /api/v1/data-vault/schema "
            "to see the allowed names."
        ),
    )


class BatchSignItem(BaseModel):
    """One document to sign inside a batch request."""
    filename: str
    file_base64: str = Field(description="Base64-encoded PDF bytes.")
    user_data: dict[str, str]
    signature_base64: str | None = Field(
        default=None,
        description="Optional base64-encoded PNG signature image.",
    )
    photo_base64: str | None = Field(
        default=None,
        description="Optional base64-encoded PNG/JPG passport photograph.",
    )
    reference: str | None = Field(
        default=None,
        description="Caller-supplied reference echoed back in the response — "
                    "useful for correlating batch results.",
    )


class BatchSignDto(BaseModel):
    items: list[BatchSignItem] = Field(min_length=1, max_length=50)


class BatchSignResultItem(BaseModel):
    reference: str | None
    document_id: UUID | None
    download_url: str | None
    report: AutoSignReport | None
    ignored_keys: list[str] = Field(default_factory=list)
    error: str | None = None


class BatchSignOut(BaseModel):
    succeeded: int
    failed: int
    items: list[BatchSignResultItem]

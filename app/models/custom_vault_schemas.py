from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models.custom_vault import CustomFieldType, CustomScope


class CustomSectionCreateDto(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    icon: str | None = None
    display_order: int | None = None
    # Default to USER. Enterprise scope requires the caller to be an admin
    # of an enterprise — the route enforces this.
    scope: CustomScope | None = CustomScope.USER


class CustomSectionUpdateDto(BaseModel):
    name: str | None = None
    icon: str | None = None
    display_order: int | None = None


class CustomSectionOut(BaseModel):
    id: UUID
    name: str
    key: str
    icon: str | None
    display_order: int
    scope: CustomScope
    enterprise_id: UUID | None = None
    # True iff the current user can rename/delete this section + add/edit
    # fields in it. Frontend hides the edit affordances when false.
    can_edit: bool = True
    created_at: datetime


class CustomFieldCreateDto(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    field_type: CustomFieldType = CustomFieldType.TEXT
    aliases: list[str] | None = None
    placeholder: str | None = None
    required: bool | None = False
    display_order: int | None = None


class CustomFieldUpdateDto(BaseModel):
    name: str | None = None
    field_type: CustomFieldType | None = None
    aliases: list[str] | None = None
    placeholder: str | None = None
    required: bool | None = None
    display_order: int | None = None


class CustomFieldOut(BaseModel):
    id: UUID
    section_id: UUID
    name: str
    key: str
    field_type: CustomFieldType
    aliases: list | None
    placeholder: str | None
    required: bool
    display_order: int
    created_at: datetime
    # Populated by the list endpoint when the user has saved a value.
    value: str | None = None
    has_value: bool = False
    # Only set for field_type == file
    file_download_url: str | None = None


class CustomSectionWithFields(CustomSectionOut):
    fields: list[CustomFieldOut] = Field(default_factory=list)


class FileUploadOut(BaseModel):
    field_id: UUID
    original_filename: str
    size_bytes: int
    mime_type: str
    download_url: str

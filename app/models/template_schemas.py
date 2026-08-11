"""Schemas for the ready-made template gallery."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TemplateSummaryOut(BaseModel):
    slug: str
    title: str
    category: str
    summary: str
    placeholder_count: int


class TemplateListOut(BaseModel):
    templates: list[TemplateSummaryOut]
    # Surfaced with the list so the UI can't show templates without also showing
    # that they aren't legal advice.
    disclaimer: str


class TemplatePlaceholderOut(BaseModel):
    key: str
    label: str
    kind: str = "text"
    required: bool = True
    help: str | None = None
    # What we'd use if the user submitted now — vault value, default, or today's
    # date. Lets the form render prefilled.
    value: str | None = None
    prefilled_from_vault: bool = False


class TemplateDetailOut(BaseModel):
    slug: str
    title: str
    category: str
    summary: str
    disclaimer: str
    placeholders: list[TemplatePlaceholderOut]
    # Which required fields still have no value, so the UI can mark them before
    # the user submits and gets a 400.
    missing_required: list[str] = Field(default_factory=list)


class UseTemplateDto(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    title: str | None = Field(default=None, max_length=200)
    page_size: str = "a4"

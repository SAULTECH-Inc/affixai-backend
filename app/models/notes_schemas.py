from __future__ import annotations

from typing import List

from pydantic import BaseModel


class PageTranscriptOut(BaseModel):
    page: int
    text: str
    provider: str


class HandwrittenNoteOut(BaseModel):
    mode: str
    note_markdown: str
    # What the model read off each page, before any rewriting. Shown next
    # to the note so the teacher can check one against the other.
    pages: List[PageTranscriptOut]
    providers_used: List[str]
    illegible_count: int
    warnings: List[str]


class VoiceNoteOut(BaseModel):
    mode: str
    note_markdown: str
    # What the speech model heard, before any rewriting.
    transcript: str
    providers_used: List[str]
    unsure_count: int
    warnings: List[str]

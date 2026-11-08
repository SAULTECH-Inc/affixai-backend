"""AI notes: turn photos of handwritten handouts, or voice recordings, into lesson notes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger

from app.common.deps import require_active_subscription
from app.common.services import lesson_notes, stt
from app.common.services.audit_service import log_audit
from app.common.services.llm import AllProvidersFailed, build_default_chain
from app.core.config import settings
from app.db.models.audit_log import AuditAction
from app.db.models.user import User
from app.models.notes_schemas import HandwrittenNoteOut, PageTranscriptOut, VoiceNoteOut

router = APIRouter()

MAX_TEXT_FIELD = 120


@router.post("/handwritten", response_model=HandwrittenNoteOut)
async def handwritten_to_note(
    files: List[UploadFile] = File(...),
    mode: str = Form(lesson_notes.NoteMode.REWRITE.value),
    subject: str = Form(""),
    class_level: str = Form(""),
    user: User = Depends(require_active_subscription),
):
    """Read photos (or a PDF) of handwritten notes and return a lesson note.

    Stateless: nothing is stored. The response has the note as Markdown
    plus the raw per-page transcript; export goes through
    `POST /documents/convert` with the Markdown as a `.md` file.

    Paid feature (`require_active_subscription`): every page is a vision
    model call, which costs real money.
    """
    try:
        note_mode = lesson_notes.NoteMode(mode.lower().strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be one of: transcribe, rewrite, expand",
        )

    chain = build_default_chain()
    if not chain.any_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI notes aren't set up on this server yet.",
        )

    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    uploads: list[lesson_notes.UploadedFile] = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{f.filename} is over the {settings.MAX_FILE_SIZE_MB}MB limit.",
            )
        uploads.append(
            lesson_notes.UploadedFile(f.filename or "upload", f.content_type or "", data)
        )

    try:
        pages = lesson_notes.load_pages(uploads, settings.NOTES_MAX_PAGES)
        result = await lesson_notes.generate_note(
            chain,
            pages,
            note_mode,
            subject=subject.strip()[:MAX_TEXT_FIELD],
            class_level=class_level.strip()[:MAX_TEXT_FIELD],
        )
    except lesson_notes.NoteInputError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AllProvidersFailed as exc:
        # Provider reasons (out of credits, bad key...) are for us, not users.
        logger.error("handwritten notes: every provider failed: {}", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't reach an AI model to read your notes. Please try again in a few minutes.",
        ) from exc

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_PROCESSED,
        entity_type="handwritten_note",
        description=f"Handwritten notes → {note_mode.value} ({len(pages)} page(s))",
        metadata={
            "mode": note_mode.value,
            "pages": len(pages),
            "providers": result.providers_used,
        },
    )

    return HandwrittenNoteOut(
        mode=result.mode.value,
        note_markdown=result.note_markdown,
        pages=[PageTranscriptOut(page=p.page, text=p.text, provider=p.provider) for p in result.pages],
        providers_used=result.providers_used,
        illegible_count=result.illegible_count,
        warnings=result.warnings,
    )


@router.post("/voice", response_model=VoiceNoteOut)
async def voice_to_note(
    file: UploadFile = File(...),
    mode: str = Form(lesson_notes.NoteMode.REWRITE.value),
    language: str = Form(""),
    subject: str = Form(""),
    class_level: str = Form(""),
    user: User = Depends(require_active_subscription),
):
    """Transcribe a recording of a teacher and return a lesson note.

    Same modes and statelessness as `/handwritten`. Speech recognition runs
    on its own providers (Claude doesn't take audio); the rewrite runs on
    the usual LLM chain. `language` pins the spoken language (en, yo, ha,
    ig); empty lets the model detect it, which is also the right choice for
    Pidgin or mixed speech.
    """
    try:
        note_mode = lesson_notes.NoteMode(mode.lower().strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be one of: transcribe, rewrite, expand",
        )
    lang = language.lower().strip()
    if lang and lang not in stt.LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="language must be one of: en, yo, ha, ig, or empty to auto-detect",
        )

    speech_chain = stt.build_default_speech_chain()
    llm_chain = build_default_chain()
    needs_llm = note_mode is not lesson_notes.NoteMode.TRANSCRIBE
    if not speech_chain.any_configured() or (needs_llm and not llm_chain.any_configured()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice notes aren't set up on this server yet.",
        )

    # Read one byte past the limit so an oversized upload is caught without
    # holding more of it in memory than necessary.
    data = await file.read(stt.MAX_AUDIO_BYTES + 1)
    audio = stt.AudioInput(file.filename or "recording", file.content_type or "", data)

    try:
        stt.validate_audio(audio)
        result = await lesson_notes.generate_voice_note(
            speech_chain,
            llm_chain,
            audio,
            note_mode,
            language=lang,
            subject=subject.strip()[:MAX_TEXT_FIELD],
            class_level=class_level.strip()[:MAX_TEXT_FIELD],
        )
    except stt.SpeechInputError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AllProvidersFailed as exc:
        logger.error("voice notes: every provider failed: {}", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't reach an AI model to process your recording. Please try again in a few minutes.",
        ) from exc

    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_PROCESSED,
        entity_type="voice_note",
        description=f"Voice recording → {note_mode.value}",
        metadata={
            "mode": note_mode.value,
            "bytes": len(data),
            "language": lang or "auto",
            "providers": result.providers_used,
        },
    )

    return VoiceNoteOut(
        mode=result.mode.value,
        note_markdown=result.note_markdown,
        transcript=result.transcript,
        providers_used=result.providers_used,
        unsure_count=result.unsure_count,
        warnings=result.warnings,
    )

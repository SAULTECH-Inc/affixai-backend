"""Handwritten handouts → lesson notes.

Two stages, kept separate on purpose:

1. **Transcribe**: each page photo goes to a vision model, which writes
   down exactly what's on the page. The transcript is returned to the
   caller as-is, so the teacher can always compare it with the AI's note.
2. **Rewrite**: a text model turns the transcript into a note in one of
   three modes. `transcribe` skips this stage entirely.

Both stages run through a `ProviderChain`, so each call falls back from
Claude to our own model server independently.
"""
from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from app.common.services.llm import ImageInput, ProviderChain

# Long edge, in pixels, that page images are scaled down to. Vision models
# downsample anything larger anyway; sending less keeps requests small and
# fast. 1568px is where Claude starts downsampling.
MAX_IMAGE_EDGE = 1568
PDF_RENDER_DPI = 150
# Pages transcribed at the same time. Enough to make a 10-page upload quick
# without tripping rate limits.
TRANSCRIBE_CONCURRENCY = 4

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

ILLEGIBLE_RE = re.compile(r"\[illegible\]", re.IGNORECASE)


class NoteMode(str, Enum):
    TRANSCRIBE = "transcribe"  # the transcript, tidied into one document
    REWRITE = "rewrite"  # fix language and layout; no new content
    EXPAND = "expand"  # full lesson note: objectives, examples, exercises


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class PageTranscript:
    page: int
    text: str
    provider: str


@dataclass
class NoteResult:
    mode: NoteMode
    note_markdown: str
    pages: list[PageTranscript]
    providers_used: list[str]
    illegible_count: int
    warnings: list[str] = field(default_factory=list)


class NoteInputError(ValueError):
    """The upload can't be processed (bad type, unreadable, too many pages)."""


# ---- Prompts ---------------------------------------------------------------

TRANSCRIBE_SYSTEM = """\
You transcribe photos of handwritten teaching material (lesson notes, \
handouts, board work) into Markdown.

Faithfulness comes first: write down what is on the page, in the order it \
appears. Don't correct facts, fill gaps, or add anything. Spelling and \
grammar stay as written; a later step tidies them, and it needs to see what \
the teacher actually wrote.

- Use Markdown headings, lists and tables only where the page itself shows \
that structure (an underlined title, numbered points, a ruled table).
- Write mathematics and chemical equations in LaTeX: $...$ inline, $$...$$ \
on their own line.
- When you can't read a word or passage with confidence, write [illegible]. \
If you have a likely reading, write it followed by [?], like photosynthesis[?].
- For a diagram, sketch or figure, write [Diagram: what it shows, including \
its labels].
- If the page has no writing on it, reply with exactly: [blank page]

Reply with the transcription only, no preamble or commentary."""

_HANDWRITING_PREAMBLE = """\
You turn a teacher's handwritten notes into a lesson note in Markdown. The \
notes have been transcribed from photos: [illegible] marks words that \
couldn't be read, a trailing [?] marks a guessed word, and [Diagram: ...] \
describes a figure on the original page."""

_SPEECH_PREAMBLE = """\
You turn a recording of a teacher speaking (a dictated note or a recorded \
lesson) into a lesson note in Markdown. The text came from speech \
recognition, so expect filler words, repetition, false starts, remarks to \
the class that aren't lesson content, and some mis-heard words, especially \
technical terms and names. The teacher may speak Nigerian English and mix \
in Pidgin, Yoruba, Hausa or Igbo.

Leave out filler and classroom management ("keep quiet", "copy this \
down"). Write the note in English. Keep local-language terms that matter \
to the lesson, with the English meaning in brackets."""

_REVIEW_RULES = """\
The teacher reviews your note against their original before anyone else \
sees it, so make that review easy:
- Keep the teacher's meaning. If something looks factually wrong, keep it \
and add a line straight after it: > **Check:** what looks wrong and why.
- Keep mathematics in LaTeX ($...$ and $$...$$)."""

_HANDWRITING_RULES = """\
- Replace an [illegible] or [?] word only when the surrounding text makes \
the word certain. Otherwise leave the marker in place.
- Keep every [Diagram: ...] marker where it is; the teacher adds the image."""

_SPEECH_RULES = """\
- When a word looks mis-heard and the context makes the intended word \
certain, write the intended word. When it doesn't, write your best reading \
followed by [?], like photosynthesis[?], so the teacher can check it."""

_FOOTER = "Start with a # title. Reply with the Markdown note only, no preamble."


class NoteSource(str, Enum):
    HANDWRITING = "handwriting"
    SPEECH = "speech"


REWRITE_SYSTEMS = {
    NoteSource.HANDWRITING: "\n\n".join(
        [_HANDWRITING_PREAMBLE, _REVIEW_RULES + "\n" + _HANDWRITING_RULES, _FOOTER]
    ),
    NoteSource.SPEECH: "\n\n".join(
        [_SPEECH_PREAMBLE, _REVIEW_RULES + "\n" + _SPEECH_RULES, _FOOTER]
    ),
}

REWRITE_MODE_INSTRUCTIONS = {
    NoteMode.REWRITE: """\
Correct spelling, grammar and punctuation, and organise the content with \
headings, numbered points and tables where they help. Don't add content: \
every point in your note should come from the teacher's notes.""",
    NoteMode.EXPAND: """\
Write a complete lesson note a student could learn from on their own:
1. ## Learning objectives
2. The content, with the teacher's points explained more fully
3. Worked examples, where the topic allows
4. ## Summary
5. ## Exercises: 5 to 10 questions
6. ## Answers to the exercises

Everything you add beyond the teacher's notes must be correct and suit the \
class level. Put *(added)* at the end of the heading of any section that \
you wrote rather than took from the notes, so the teacher knows what to \
check.""",
}


# ---- Stage 0: files → page images -------------------------------------------


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _normalise_image(data: bytes) -> ImageInput:
    """Decode an upload, fix phone-camera rotation, shrink it, and re-encode
    as JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        # Phone photos are often stored sideways with an EXIF rotation flag;
        # vision models read the raw pixels, so apply the rotation first.
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise NoteInputError("One of the images couldn't be read.") from exc
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return ImageInput(media_type="image/jpeg", data=out.getvalue())


def _pdf_pages(data: bytes) -> list[bytes]:
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise NoteInputError("The PDF couldn't be opened.") from exc
    with doc:
        return [page.get_pixmap(dpi=PDF_RENDER_DPI).tobytes("png") for page in doc]


def load_pages(files: Sequence[UploadedFile], max_pages: int) -> list[ImageInput]:
    """Turn uploads (photos and/or PDFs) into one image per page, in upload
    order."""
    raw: list[bytes] = []
    for f in files:
        ext = _ext(f.filename)
        ctype = (f.content_type or "").lower()
        if ext == "pdf" or ctype == "application/pdf":
            raw.extend(_pdf_pages(f.data))
        elif ext in IMAGE_EXTENSIONS or ctype in IMAGE_TYPES:
            raw.append(f.data)
        elif ext in ("heic", "heif") or "heic" in ctype or "heif" in ctype:
            raise NoteInputError(
                f"{f.filename} is a HEIC photo. Set your camera to JPEG, or "
                "export the photo as JPEG, and upload it again."
            )
        else:
            raise NoteInputError(
                f"{f.filename} isn't supported. Upload JPEG, PNG or WebP photos, or a PDF."
            )
        if len(raw) > max_pages:
            raise NoteInputError(f"That's more than {max_pages} pages. Upload at most {max_pages}.")
    if not raw:
        raise NoteInputError("No pages found in the upload.")
    return [_normalise_image(r) for r in raw]


# ---- Stage 1: transcribe ------------------------------------------------------


async def transcribe_pages(chain: ProviderChain, pages: Sequence[ImageInput]) -> list[PageTranscript]:
    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)

    async def one(index: int, image: ImageInput) -> PageTranscript:
        async with sem:
            result = await chain.complete(
                system=TRANSCRIBE_SYSTEM,
                prompt=f"Transcribe page {index + 1}.",
                images=[image],
                max_tokens=8000,
            )
        return PageTranscript(page=index + 1, text=result.text, provider=result.provider)

    return list(await asyncio.gather(*(one(i, p) for i, p in enumerate(pages))))


# ---- Stage 2: rewrite -----------------------------------------------------------


def _joined_transcript(pages: Sequence[PageTranscript]) -> str:
    return "\n\n".join(f"<page number=\"{p.page}\">\n{p.text}\n</page>" for p in pages)


def _transcript_document(pages: Sequence[PageTranscript]) -> str:
    """Transcribe mode: the pages as one Markdown document, unchanged."""
    if len(pages) == 1:
        return pages[0].text
    return "\n\n---\n\n".join(f"*Page {p.page}*\n\n{p.text}" for p in pages)


async def rewrite(
    chain: ProviderChain,
    notes_text: str,
    mode: NoteMode,
    *,
    source: NoteSource,
    subject: str = "",
    class_level: str = "",
):
    """Turn transcribed notes (from photos or a recording) into a note."""
    context = []
    if subject:
        context.append(f"Subject: {subject}")
    if class_level:
        context.append(f"Class level: {class_level}")
    prompt = "\n".join(
        [
            *context,
            "",
            REWRITE_MODE_INSTRUCTIONS[mode],
            "",
            "The teacher's notes:",
            notes_text,
        ]
    ).strip()
    return await chain.complete(
        system=REWRITE_SYSTEMS[source],
        prompt=prompt,
        max_tokens=32000 if mode is NoteMode.EXPAND else 16000,
    )


# ---- Whole pipeline -------------------------------------------------------------


async def generate_note(
    chain: ProviderChain,
    pages: Sequence[ImageInput],
    mode: NoteMode,
    *,
    subject: str = "",
    class_level: str = "",
) -> NoteResult:
    transcripts = await transcribe_pages(chain, pages)
    warnings: list[str] = []

    blank = [t.page for t in transcripts if t.text.strip() == "[blank page]"]
    if blank:
        warnings.append(f"No writing found on page(s) {', '.join(map(str, blank))}.")
    readable = [t for t in transcripts if t.page not in blank]
    if not readable:
        warnings.append("No writing was found on any page.")
        return NoteResult(mode, "", transcripts, _providers(transcripts, []), 0, warnings)

    if mode is NoteMode.TRANSCRIBE:
        note = _transcript_document(readable)
        rewrite_provider: list[str] = []
    else:
        result = await rewrite(
            chain,
            _joined_transcript(readable),
            mode,
            source=NoteSource.HANDWRITING,
            subject=subject,
            class_level=class_level,
        )
        note = result.text
        rewrite_provider = [result.provider]
        if result.truncated:
            warnings.append(
                "The note was cut off because it got too long. Try uploading fewer pages at a time."
            )

    if chain.fell_back:
        warnings.append(
            "Our main AI model was unavailable, so a backup model produced some or all "
            "of this note. Check it more carefully than usual."
        )

    return NoteResult(
        mode=mode,
        note_markdown=note,
        pages=transcripts,
        providers_used=_providers(transcripts, rewrite_provider),
        illegible_count=len(ILLEGIBLE_RE.findall(note)),
        warnings=warnings,
    )


def _providers(transcripts: Sequence[PageTranscript], extra: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for name in [*(t.provider for t in transcripts), *extra]:
        if name not in seen:
            seen.append(name)
    return seen


# ---- Voice notes ----------------------------------------------------------------

UNSURE_RE = re.compile(r"\[\?\]")


@dataclass
class VoiceNoteResult:
    mode: NoteMode
    note_markdown: str
    transcript: str
    providers_used: list[str]
    # Words the rewrite marked with [?] because they may have been mis-heard.
    unsure_count: int
    warnings: list[str] = field(default_factory=list)


def speech_hint(subject: str = "") -> str:
    """Short context for the speech model. Telling it who is speaking and
    about what measurably helps with accents and subject vocabulary."""
    hint = "A Nigerian teacher speaking to a class"
    return f"{hint} about {subject}." if subject else f"{hint}."


async def generate_voice_note(
    speech_chain,
    llm_chain: ProviderChain,
    audio,
    mode: NoteMode,
    *,
    language: str = "",
    subject: str = "",
    class_level: str = "",
) -> VoiceNoteResult:
    """Recording → transcript → (optionally) lesson note.

    `speech_chain` is an `stt.SpeechChain` and `audio` an `stt.AudioInput`;
    left untyped to keep this module free of an import cycle.
    """
    transcript = await speech_chain.transcribe(
        audio, language=language, prompt=speech_hint(subject)
    )
    providers = [f"speech:{transcript.provider}"]
    warnings: list[str] = []
    if speech_chain.fell_back:
        warnings.append(
            "Our main speech model was unavailable, so a backup model transcribed this "
            "recording. Check names and technical terms carefully."
        )

    text = transcript.text
    if not text:
        warnings.append("No speech was found in the recording.")
        return VoiceNoteResult(mode, "", "", providers, 0, warnings)

    if mode is NoteMode.TRANSCRIBE:
        note = text
    else:
        result = await rewrite(
            llm_chain,
            text,
            mode,
            source=NoteSource.SPEECH,
            subject=subject,
            class_level=class_level,
        )
        note = result.text
        providers.append(result.provider)
        if result.truncated:
            warnings.append(
                "The note was cut off because it got too long. Try a shorter recording."
            )
        if llm_chain.fell_back:
            warnings.append(
                "Our main AI model was unavailable, so a backup model wrote this note. "
                "Check it more carefully than usual."
            )

    return VoiceNoteResult(
        mode=mode,
        note_markdown=note,
        transcript=text,
        providers_used=providers,
        unsure_count=len(UNSURE_RE.findall(note)),
        warnings=warnings,
    )

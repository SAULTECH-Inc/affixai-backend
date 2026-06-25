"""OCR + label-to-vault-field extraction for Phase 3.

Self-contained: takes raw bytes, OCRs the document, parses labels-and-values
out of the OCR text, then maps each label to a vault (segment, field) via the
alias registry. Returns a structured preview the user can review before saving.

Parsing handles three layouts that ID cards and forms actually use:

  1. Inline    — "Label: Value"  or  "Label   Value"
  2. Stacked   — label on one line, value on the next
                 (typical of phone-photo IDs like the Nigerian NIN slip)
  3. Two-col   — two labels on one line separated by big spaces,
                 two values on the next line in the same columns

A short image-preprocessing pass (grayscale + upscale-if-small + CLAHE) helps
OCR on real-world phone photos before tesseract sees the bitmap.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from loguru import logger

from app.common.vault_schema import VaultSegment, match_label_to_field
from app.core.config import settings


@dataclass
class RawField:
    label: str
    value: str
    confidence: float  # 0–1


@dataclass
class MappedField:
    segment: VaultSegment
    field_name: str
    value: str
    confidence: float
    source_label: str


# ---------- noise & line-shape heuristics ----------------------------------

_NOISE_PREFIXES = (
    "page ",
    "issued by",
    "valid for",
    "this document",
    "specimen",
    "federal republic",  # NIN slip / passport headers
    "digital nin slip",
)


def _digit_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(c.isdigit() for c in text) / len(text)


def _looks_like_field_line(label: str, value: str) -> bool:
    if not label or not value:
        return False
    if len(label) > 60 or len(value) > 250:
        return False
    if any(label.lower().startswith(p) for p in _NOISE_PREFIXES):
        return False
    # Labels that are mostly digits are usually misread values.
    if _digit_ratio(label) > 0.5:
        return False
    return True


def _looks_like_label(text: str) -> bool:
    """True if a line could plausibly be a field label (not a value).

    Used by the stacked-line parser to decide whether to take the next line
    as the paired value.
    """
    s = text.strip()
    if not s or len(s) > 80:
        return False
    if any(s.lower().startswith(p) for p in _NOISE_PREFIXES):
        return False
    # Has at least one letter
    if not any(c.isalpha() for c in s):
        return False
    # Labels tend to be word-y, not digit-heavy
    if _digit_ratio(s) > 0.3:
        return False
    return True


# ---------- OCR ------------------------------------------------------------


def _preprocess_for_ocr(img):
    """Light preprocessing to help tesseract on phone photos of IDs.

    Grayscale, upscale if the image is small, CLAHE for contrast on uneven
    lighting (green watermarks on the NIN slip, etc.). Falls back to the
    original PIL image if OpenCV isn't available.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return img

    try:
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

        h, w = gray.shape
        # Upscale small images — tesseract likes ~300dpi equivalent text height.
        if max(h, w) < 1500:
            scale = 1500.0 / max(h, w)
            gray = cv2.resize(
                gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
            )

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        return Image.fromarray(gray)
    except Exception as exc:
        logger.warning(f"OCR preprocessing failed, using original: {exc}")
        return img


def extract_text_from_bytes(data: bytes, mime_type: str | None) -> str:
    """OCR a document. Supports PDF and common image formats."""
    import pytesseract
    from PIL import Image

    mt = (mime_type or "").lower()
    is_pdf = "pdf" in mt or data[:4] == b"%PDF"
    pages: list[str] = []

    if is_pdf:
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(data, dpi=settings.OCR_DPI)
        for img in images[: settings.MAX_PAGES_PER_DOCUMENT]:
            prepped = _preprocess_for_ocr(img)
            pages.append(pytesseract.image_to_string(prepped, lang=settings.OCR_LANGUAGE))
    else:
        img = Image.open(io.BytesIO(data))
        prepped = _preprocess_for_ocr(img)
        pages.append(pytesseract.image_to_string(prepped, lang=settings.OCR_LANGUAGE))

    return "\n".join(pages)


# ---------- parsers --------------------------------------------------------

# Patterns that match label/value on the SAME line.
_KV_PATTERNS = [
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./'\-/]{1,58}?)\s*[:\-]\s+(.+?)\s*$"),
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./'\-/]{1,58}?)\s{2,}(.+?)\s*$"),
]


def _try_inline(line: str) -> RawField | None:
    """Match `Label: Value` on a single line.

    Bails out if the line looks like a *two-column label header* (two
    label-shaped tokens separated by a big gap, e.g. 'DATE OF BIRTH   SEX/SEXE')
    — those should be handled by the two-column pass, not consumed inline.
    """
    cols = _split_two_columns(line)
    if cols and len(cols) == 2 and all(_looks_like_label(c) for c in cols):
        return None

    for pat in _KV_PATTERNS:
        m = pat.match(line)
        if not m:
            continue
        label = m.group(1).strip().rstrip(":")
        value = m.group(2).strip()
        if _looks_like_field_line(label, value):
            return RawField(label=label, value=value, confidence=0.7)
    return None


def _split_two_columns(line: str) -> list[str] | None:
    """Split a line on >=3 consecutive spaces (column separator). Returns the
    parts if there are exactly two non-empty ones, otherwise None.
    """
    parts = [p.strip() for p in re.split(r"\s{3,}", line) if p.strip()]
    if len(parts) == 2:
        return parts
    return None


def parse_key_value_lines(text: str) -> list[RawField]:
    """Extract candidate (label, value) pairs from OCR text.

    Strategy:
      Pass 1: walk lines looking for inline `Label: Value` matches.
      Pass 2: for any remaining label-shaped lines, try to pair with the
              next line as a value (stacked layout), or split both lines on
              column gaps and pair side-by-side (two-column layout).
    """
    raw_lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in raw_lines if ln and len(ln) <= 300]

    out: list[RawField] = []
    consumed: set[int] = set()

    # Pass 1: inline
    for idx, line in enumerate(lines):
        rf = _try_inline(line)
        if rf:
            out.append(rf)
            consumed.add(idx)

    # Pass 2: stacked + two-column
    for idx in range(len(lines) - 1):
        if idx in consumed or (idx + 1) in consumed:
            continue
        label_line = lines[idx]
        value_line = lines[idx + 1]

        if not _looks_like_label(label_line):
            continue
        # If the "value" line itself looks like a label, this isn't a pair —
        # most likely two consecutive label lines (rare; we just skip).
        if _looks_like_label(value_line) and not any(c.isdigit() for c in value_line):
            # Allow it if the next line is short and looks like a single token
            # ("ADAMU", "SHADRACH" — names). Names look label-shaped but are
            # short and single-word. Filter on word count.
            if len(value_line.split()) > 3 and _digit_ratio(value_line) < 0.05:
                continue

        # Two-column attempt first — covers "DATE OF BIRTH   SEX/SEXE"
        cols_label = _split_two_columns(label_line)
        cols_value = _split_two_columns(value_line)
        if cols_label and cols_value and len(cols_label) == len(cols_value):
            placed = False
            for lp, vp in zip(cols_label, cols_value):
                if _looks_like_field_line(lp, vp):
                    out.append(RawField(label=lp, value=vp, confidence=0.55))
                    placed = True
            if placed:
                consumed.add(idx)
                consumed.add(idx + 1)
                continue

        # Single-column stacked label/value pair.
        if _looks_like_field_line(label_line, value_line):
            # Only emit if the label fuzzy-matches a known vault field —
            # otherwise we'd flood `unmatched` with header noise that just
            # happened to be followed by a single-word value.
            if match_label_to_field(label_line, fuzzy_threshold=0.75) is not None:
                out.append(RawField(label=label_line, value=value_line, confidence=0.65))
                consumed.add(idx)
                consumed.add(idx + 1)

    return out


def map_to_vault_fields(
    raw_fields: list[RawField],
    segment_hint: VaultSegment | None = None,
    fuzzy_threshold: float = 0.85,
) -> tuple[dict[str, dict[str, MappedField]], list[RawField]]:
    """Match raw OCR labels to (segment, field_name) pairs.

    Returns:
      mapped: {segment_value: {field_name: MappedField}}  — best match per field
      unmatched: list of RawField entries we couldn't confidently place
    """
    mapped: dict[str, dict[str, MappedField]] = {}
    unmatched: list[RawField] = []

    for raw in raw_fields:
        hit = match_label_to_field(
            raw.label, segment=segment_hint, fuzzy_threshold=fuzzy_threshold
        )
        if not hit:
            unmatched.append(raw)
            continue
        seg, field_name, label_match_conf = hit
        combined = label_match_conf * raw.confidence
        existing = mapped.setdefault(seg.value, {}).get(field_name)
        if existing is None or combined > existing.confidence:
            mapped[seg.value][field_name] = MappedField(
                segment=seg,
                field_name=field_name,
                value=raw.value,
                confidence=combined,
                source_label=raw.label,
            )

    return mapped, unmatched


async def extract_vault_preview(
    data: bytes,
    mime_type: str | None,
    segment_hint: VaultSegment | None = None,
) -> dict:
    """One-shot helper used by the route: bytes → preview payload."""
    try:
        text = extract_text_from_bytes(data, mime_type)
    except Exception as exc:
        logger.warning(f"OCR failed: {exc}")
        return {
            "raw_text": "",
            "segments": {},
            "unmatched": [],
            "error": f"OCR failed: {exc}",
        }

    raw_fields = parse_key_value_lines(text)
    mapped, unmatched = map_to_vault_fields(raw_fields, segment_hint=segment_hint)

    return {
        "raw_text": text[:5000],
        "segments": {
            seg: {
                fname: {
                    "value": mf.value,
                    "confidence": round(mf.confidence, 3),
                    "source_label": mf.source_label,
                }
                for fname, mf in fields.items()
            }
            for seg, fields in mapped.items()
        },
        "unmatched": [
            {"label": rf.label, "value": rf.value, "confidence": round(rf.confidence, 3)}
            for rf in unmatched
        ],
    }

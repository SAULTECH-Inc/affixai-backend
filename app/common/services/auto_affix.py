"""Phase 4: Auto-affix engine.

Given a PDF the user wants to sign, detect labelled fields ("First Name:",
"DOB:", "Sign here:") with their page positions, match each label against the
user's vault via the same registry/aliases used by Phase 3, and stamp the
values onto the PDF in place. If the user has a default signature, stamp it
anywhere the document says "signature" / "sign here".

Only digital PDFs (with extractable text) are supported in this iteration —
scanned PDFs would need positional OCR (pytesseract image_to_data) wired in,
which is straightforward to add later but kept out for now.
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from contextvars import ContextVar

from loguru import logger

from app.common.services.local_storage import fetch_file_bytes
from app.common.vault_schema import VaultSegment, match_label_to_field as _raw_match
from app.core.encryption import decrypt
from app.db.models.data_vault import DataVault
from app.db.models.passport_photo import PassportPhoto
from app.db.models.signature import Signature


# Per-request custom-field aliases. The top-level entry point fetches the
# user's custom fields once, sets this ContextVar, and `match_label_to_field`
# (the wrapper below) merges them into every match call without each call
# site needing to pass them through.
_custom_fields_ctx: ContextVar[list[tuple[str, str, list[str]]]] = ContextVar(
    "custom_field_aliases", default=[]
)


def match_label_to_field(
    label: str,
    segment: VaultSegment | None = None,
    fuzzy_threshold: float = 0.85,
):
    """Wrapper around the schema matcher that also considers per-request
    user-defined custom fields. The ContextVar is set by `auto_affix_pdf_bytes`
    at the start of each request.
    """
    return _raw_match(
        label,
        segment=segment,
        fuzzy_threshold=fuzzy_threshold,
        extra_fields=_custom_fields_ctx.get() or None,
    )


# Phrases that mean "put a signature here" rather than text.
#
# DELIBERATELY NARROW. We previously matched the bare verb "signed" but it
# triggers all over contract bodies ("...this Agreement signed by both parties",
# "Each party has signed..."), causing 3–4 signature stamps to land on top of
# random body text. The keyword pass now requires a label-shaped trigger;
# the line-shape gate in `_line_looks_like_sig_label` rejects any matches that
# happen inside prose anyway.
_SIGNATURE_KEYWORDS = re.compile(
    r"\bsignature\b|"
    r"\bsign\s+here\b|"
    r"\bsigned\s+by\s*:|"           # only with a trailing colon = label form
    r"\bsign\s+and\s+date\b",
    re.IGNORECASE,
)


# Approximate cap on how many signatures we'll auto-stamp via the dict pass.
# Real signature blocks rarely need more than 2 (e.g. Disclosing/Receiving on
# an NDA, or Employee/Employer). 4 is a generous ceiling — once we hit it we
# stop placing more, letting the user drag any extras in the editor.
_MAX_AUTO_SIGNATURES = 4

# Phrases for initials.
_INITIALS_KEYWORDS = re.compile(r"\binitial(s)?\b", re.IGNORECASE)

# Phrases that mean "attach a passport-style photograph here". We require
# either an action verb (affix/paste/attach) OR the word "passport" to qualify,
# so document body text mentioning "photo" doesn't trigger false stamping.
_PHOTO_KEYWORDS = re.compile(
    r"\b("
    r"(affix|paste|attach)\s+(your\s+)?(passport[-\s]+)?(photograph|photo)|"
    r"passport\s+(size\s+)?(photograph|photo)|"
    r"applicant'?s\s+(photograph|photo)|"
    r"photograph\s+here|photo\s+here"
    r")\b",
    re.IGNORECASE,
)

# Standard passport photo: 3.5 cm × 4.5 cm. At 72dpi (PDF default) that's
# ~99 × 128 points. We use 100×130 as a clean default.
_PHOTO_WIDTH_PT = 100.0
_PHOTO_HEIGHT_PT = 130.0

# Pattern helpers for the underscore-field detector (Phase 4-bis).
_NUMBER_PREFIX = re.compile(r"^\d{1,3}\.?$")  # "1." "2." "11."
_UNDERSCORE_RUN_MIN = 3                       # at least 3 `_` count as a fillable run


def _resolve_special_label_value(label: str, vault: dict[str, str]) -> str | None:
    """Labels that aren't a vault field but have an obvious auto-value:

      "Date" / "Today's Date"           → today, formatted DD/MM/YYYY
      "Place" / "Signed at"             → user's city (fallback country)

    Every application form has these at the signature block. Auto-filling
    them removes the most common manual placement step. Returns None for
    labels that should go through normal vault matching.
    """
    from datetime import date as _date

    cleaned = label.strip().rstrip(":.-—").strip().lower()
    if cleaned in {"date", "today's date", "today", "current date",
                   "date of application", "application date",
                   # NDA / contract phrasings — the label is sometimes the
                   # whole prose preamble leading up to the underscore.
                   "entered into on the date of",
                   "dated this", "on this", "this day of",
                   "effective date", "agreement date", "execution date"}:
        return _date.today().strftime("%d/%m/%Y")
    # Looser fuzzy match for prose-y date prompts ("...has been entered into
    # on the date of ___"). If the cleaned label ENDS in "date of" or
    # contains "date" and is short, treat it as a date prompt.
    if (
        cleaned.endswith("date of")
        or cleaned.endswith("date")
        and len(cleaned.split()) <= 6
        and "of birth" not in cleaned
        and "issue" not in cleaned
        and "expir" not in cleaned
    ):
        return _date.today().strftime("%d/%m/%Y")
    if cleaned in {"place", "signed at", "place of signing", "location",
                   "from", "city"}:
        return vault.get("city") or vault.get("country") or None
    return None


def _erase_under(page: Any, x: float, y: float, w: float, h: float) -> bool:
    """Paint a rectangle of the local page background over (x, y, w, h).

    Used to remove underscore strokes / placeholder dashes that would
    otherwise show through under our stamped text — visually noisy when we
    write `Name` directly on top of `_______`.

    Background-aware: we sample a few pixels JUST ABOVE the erase region
    (rather than blindly painting white) so documents with colored
    backgrounds, watermarks, or templates aren't punched with a white hole.
    If the sampled color is too dark / patterned, we abort and let the
    underscores stay — clutter is better than ruining the document.

    Returns True if we erased, False if we abstained.
    """
    if w <= 0 or h <= 0:
        return False
    import fitz

    # Sample a 8x4-ish strip ABOVE the erase region — the underscore strokes
    # themselves sit at the baseline, so a few px above is "clean" background.
    sample_y = max(0.0, y - 8.0)
    sample_h = max(2.0, min(4.0, y - sample_y - 1.0))
    if sample_h < 1.0:
        return False
    sample_rect = fitz.Rect(x, sample_y, x + w, sample_y + sample_h)
    try:
        pix = page.get_pixmap(clip=sample_rect, dpi=72)
    except Exception as exc:
        logger.debug(f"erase sample failed: {exc}")
        return False
    if pix.width == 0 or pix.height == 0:
        return False

    # Average RGB across the sample. Stride = width * n where n is channels.
    samples = pix.samples
    n = pix.n  # 1 (gray), 3 (RGB), 4 (RGBA)
    total = pix.width * pix.height
    if total == 0 or n < 1:
        return False
    if n == 1:
        avg = sum(samples) / total
        r = g = b = avg
    else:
        # Read only RGB channels — ignore alpha if present.
        r = sum(samples[i] for i in range(0, len(samples), n)) / total
        g = sum(samples[i + 1] for i in range(0, len(samples), n)) / total
        b = sum(samples[i + 2] for i in range(0, len(samples), n)) / total

    # Guard: only erase if background is overwhelmingly light + uniform-ish.
    # 240/255 ≈ 94%. Lower than that risks erasing real content.
    if min(r, g, b) < 235:
        return False
    # Variance proxy — if R/G/B differ by more than 8 the area is probably
    # tinted (light blue forms, faint watermarks). Skip to be safe.
    if max(r, g, b) - min(r, g, b) > 8:
        return False

    # Paint with the SAMPLED color (preserves slightly-off-white templates).
    bg = (r / 255.0, g / 255.0, b / 255.0)
    try:
        page.draw_rect(
            fitz.Rect(x, y, x + w, y + h),
            color=bg, fill=bg, width=0, overlay=True,
        )
        return True
    except Exception as exc:
        logger.debug(f"erase draw failed: {exc}")
        return False


def _sniff_font_for_y(page: Any, y_target: float, tolerance: float = 6.0) -> dict[str, Any]:
    """Find the dominant label-bearing span near a y position and return its
    font properties.

    Used by the underscore-pass (and any other stamp site that doesn't have a
    direct span reference) so stamped text inherits the form's actual font
    instead of always being hardcoded helv 10pt.

    Skips spans whose text is only punctuation/underscores (those are the
    blank fill lines, not the form's body text).
    """
    try:
        text_dict = page.get_text("dict")
    except Exception:
        return {}
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox") or (0, 0, 0, 0)
                span_y_mid = (float(bbox[1]) + float(bbox[3])) / 2
                if abs(span_y_mid - y_target) > tolerance:
                    continue
                stripped = (span.get("text") or "").strip()
                if not stripped:
                    continue
                # Skip "pure-blank" spans like ":-_____" or "____" which carry
                # the form's font but aren't a useful label baseline.
                if all(c in "_- :." for c in stripped):
                    continue
                return _sniff_font_from_span(span)
    return {}


def _sniff_font_from_span(span: dict[str, Any] | None) -> dict[str, Any]:
    """Read font properties off a PyMuPDF span dict — used so auto-affixed
    text inherits the document's actual font, size, weight and color rather
    than always being 10pt black Helvetica.

    PyMuPDF returns:
      span["font"]   — font name like 'Helvetica-Bold', 'TimesNewRomanPS-ItalicMT'
      span["size"]   — fontsize in points
      span["color"]  — color as int 0xRRGGBB
      span["flags"]  — bitmask: 1=superscript, 2=italic, 4=serifed, 8=mono, 16=bold

    We collapse the font name to one of our three family buckets and read
    bold/italic from flags.
    """
    if not span:
        return {}
    name = (span.get("font") or "").lower()
    flags = int(span.get("flags") or 0)
    size = float(span.get("size") or 10.0)
    color_int = span.get("color")

    # Family detection: prefer the flags bits, fall back to name substring.
    if (flags & 8) or "courier" in name or "mono" in name:
        family = "cour"
    elif (flags & 4) or "times" in name or "serif" in name or "roman" in name:
        family = "tiro"
    else:
        family = "helv"

    bold = bool(flags & 16) or "bold" in name or "black" in name or "heavy" in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name

    if isinstance(color_int, int):
        color = f"#{color_int & 0xFFFFFF:06x}"
    else:
        color = "#000000"

    return {
        "fontsize": size,
        "font_family": family,
        "bold": bold,
        "italic": italic,
        "color": color,
    }


@dataclass
class StampedField:
    label: str           # the label we saw, e.g. "Date of Birth"
    field_name: str      # vault field, e.g. "date_of_birth"
    segment: VaultSegment
    value: str
    page: int            # 0-indexed
    x: float
    y: float
    match_confidence: float
    # Font properties sniffed from the surrounding document text (so a
    # subsequent edit/re-stamp uses the same font as the form).
    fontsize: float = 10.0
    font_family: str = "helv"
    bold: bool = False
    italic: bool = False
    color: str = "#000000"


@dataclass
class StampedImage:
    """An image (signature/photo) placement with position so the editor can
    re-load it as a draggable overlay and re-stamping preserves it."""
    kind: str  # "signature" | "photo"
    page: int
    x: float
    y: float
    width: float
    height: float


@dataclass
class StampReport:
    fields_filled: list[StampedField] = field(default_factory=list)
    image_placements: list[StampedImage] = field(default_factory=list)
    labels_unmatched: list[str] = field(default_factory=list)
    signatures_placed: int = 0
    initials_placed: int = 0
    photos_placed: int = 0
    pages: int = 0
    error: str | None = None


# ---- Vault & signature helpers ----------------------------------------------


async def get_user_vault_dict(user_id: UUID) -> dict[str, str]:
    """Flat {field_name: decrypted_value} across all active vault rows for the user.

    Names are unique per (user, segment, field_name); we collapse to field_name
    because labels in target documents don't tell us which segment they came
    from. If two segments share a field name (they don't currently), the last
    one queried wins — acceptable for now.

    Also computes a few *derived* fields when the user hasn't filled them
    directly:
      - `full_legal_name`: composed from first+middle+last when not explicit
      - `street_address_line_1`: stays as-is, but a `full_address` is also
        composed so labels like "Address" with no specific line variant can
        match
    """
    rows = await DataVault.filter(user_id=user_id, is_active=True, deleted_at=None)
    result: dict[str, str] = {}
    for row in rows:
        try:
            result[row.field_name] = decrypt(row.encrypted_value)
        except Exception as exc:
            logger.warning(f"vault decrypt failed for row {row.id}: {exc}")

    # ---- Derived / fallback fields ----------------------------------------
    # Compose full_legal_name from name parts when not explicitly set. This is
    # the common case — a user who fills "First / Middle / Last" on signup
    # shouldn't have to also retype the full name.
    if not result.get("full_legal_name"):
        parts = [
            result.get("first_name"),
            result.get("middle_name"),
            result.get("last_name"),
        ]
        composed = " ".join(p.strip() for p in parts if p and p.strip())
        if composed:
            result["full_legal_name"] = composed
            logger.info(f"derived full_legal_name={composed!r} for user {user_id}")

    # Same fallback for first_name from full_legal_name's first token, so
    # forms that ask for "First Name" but the user only saved full_legal_name
    # still match.
    if not result.get("first_name") and result.get("full_legal_name"):
        tokens = result["full_legal_name"].split()
        if tokens:
            result["first_name"] = tokens[0]

    if not result.get("last_name") and result.get("full_legal_name"):
        tokens = result["full_legal_name"].split()
        if len(tokens) > 1:
            result["last_name"] = tokens[-1]

    # Compute current age from date_of_birth. Some forms ask "Age" instead of
    # (or in addition to) DOB — auto-fill keeps the user from having to do
    # mental arithmetic. We don't store age in the DB because it goes stale;
    # we recompute on every vault read.
    if not result.get("age") and result.get("date_of_birth"):
        age = _compute_age(result["date_of_birth"])
        if age is not None:
            result["age"] = str(age)
            logger.info(f"derived age={age} for user {user_id}")

    # Multi-entry sections (Education / Employment) — flatten the "current"
    # entry's fields into the result dict so existing label matchers like
    # "Employer Name" and "Institution Name" continue to work.
    #
    # Resolution order per section:
    #   1. is_current=True entry, if any
    #   2. otherwise the one with the latest start_date (most-recent role/degree)
    #   3. otherwise the first row by sort_order
    #
    # If a single-entry vault row already supplied a value (e.g. an old
    # employment row that hadn't been migrated), we DON'T overwrite it — the
    # legacy data wins. Newly-saved VaultEntry data lives separately.
    try:
        from app.common.vault_schema import MULTI_ENTRY_SEGMENTS
        from app.db.models.vault_entry import VaultEntry
        import json as _json

        for seg in MULTI_ENTRY_SEGMENTS:
            rows = await VaultEntry.filter(
                user_id=user_id, section=seg.value, deleted_at=None
            ).order_by("-is_current", "sort_order", "-created_at")
            if not rows:
                continue
            best: dict[str, str] | None = None
            best_start: str = ""
            for r in rows:
                try:
                    fields = _json.loads(decrypt(r.encrypted_payload))
                except Exception:
                    continue
                if not isinstance(fields, dict):
                    continue
                if r.is_current:
                    best = fields
                    break
                start = str(fields.get("start_date") or fields.get("employment_start_date") or "")
                if best is None or start > best_start:
                    best = fields
                    best_start = start
            if best:
                for k, v in best.items():
                    if v and not result.get(k):
                        result[k] = str(v)
    except Exception as exc:
        logger.warning(f"multi-entry flatten failed for user {user_id}: {exc}")

    return result


async def get_user_custom_field_aliases(
    user_id: UUID,
) -> list[tuple[str, str, list[str]]]:
    """Return `(segment_key, field_name, aliases_inc_display_name)` tuples for
    every custom field VISIBLE to this user.

    "Visible" = user-scope fields owned by `user_id` PLUS every enterprise-
    scope field belonging to the user's enterprise (if they're in one). Both
    flavors of field get the same treatment in label matching — they sit
    alongside built-in vault fields with no priority difference.

    `aliases_inc_display_name` always includes:
      - the field's display name (e.g. "Policy Number")
      - the slug-style key (e.g. "policy_number" rendered as "policy number")
      - any user-supplied aliases (CustomVaultField.aliases)
    """
    from tortoise.expressions import Q

    from app.db.models.custom_vault import CustomVaultField
    from app.db.models.user import User as _User

    user = await _User.get_or_none(id=user_id, deleted_at=None)
    enterprise_id = user.enterprise_id if user else None

    # CustomVaultField denormalizes user_id / enterprise_id from its parent
    # section — `scope` lives on the section, not the field. Visibility =
    # user-owned field OR a field belonging to the user's enterprise.
    scope_q = Q(user_id=user_id)
    if enterprise_id is not None:
        scope_q |= Q(enterprise_id=enterprise_id)

    rows = await CustomVaultField.filter(scope_q, deleted_at=None)
    out: list[tuple[str, str, list[str]]] = []
    for f in rows:
        names = {f.name, f.key.replace("_", " ")}
        if f.aliases:
            for a in f.aliases:
                if a:
                    names.add(a)
        out.append((f"custom:{f.section_id}", f.key, sorted(names)))
    return out


def _compute_age(dob_raw: str) -> int | None:
    """Parse a DOB string and return years since that date, or None if we
    can't parse it.

    We accept a generous set of common date formats — users (and the forms
    they fill in) write dates in many ways:
      - 1995-04-12 / 1995/04/12 (ISO)
      - 12/04/1995, 12-04-1995 (DMY — typical NG/UK)
      - 04/12/1995 (MDY — typical US — but we prefer DMY for ambiguous)
      - 12 Apr 1995, April 12, 1995
    """
    from datetime import date as _date, datetime as _dt

    s = (dob_raw or "").strip()
    if not s:
        return None

    parsed: _date | None = None
    # Try a handful of explicit formats. Order matters — list least-ambiguous
    # formats (those with year first or named months) before ambiguous ones.
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",   # DMY (preferred for NG)
        "%m/%d/%Y", "%m-%d-%Y",                # MDY (US)
    ):
        try:
            parsed = _dt.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        # Last-ditch: try datetime.fromisoformat (handles 2009-04-12 etc.).
        try:
            parsed = _date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None

    today = _date.today()
    years = today.year - parsed.year
    # Subtract a year if their birthday hasn't happened yet this year.
    if (today.month, today.day) < (parsed.month, parsed.day):
        years -= 1
    if years < 0 or years > 130:
        # Sanity check — almost certainly a parse/format mistake.
        return None
    return years


async def get_user_default_signature(user_id: UUID) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime_type) for the user's default signature, or None.

    Sources, in order of preference:
      1. Inline base64 in `signature_data`
      2. Local file via `signature_url` (`local://` prefix or bare path)
    """
    sig = await Signature.get_or_none(
        user_id=user_id, is_default=True, deleted_at=None
    )
    if not sig:
        sig = await Signature.filter(user_id=user_id, deleted_at=None).order_by(
            "-created_at"
        ).first()
    if not sig:
        logger.info(f"no signature found for user {user_id}")
        return None

    if sig.signature_data:
        try:
            data = sig.signature_data
            if data.startswith("data:"):
                head, _, b64 = data.partition(",")
                mime = head.split(";")[0].replace("data:", "") or "image/png"
                return base64.b64decode(b64), mime
            return base64.b64decode(data), "image/png"
        except Exception as exc:
            logger.warning(f"signature base64 decode failed: {exc}")

    if sig.signature_url:
        try:
            data = await fetch_file_bytes(sig.signature_url)
            logger.info(
                f"loaded signature for user {user_id} from {sig.signature_url!r} ({len(data)} bytes)"
            )
            return data, "image/png"
        except Exception as exc:
            logger.warning(f"signature file read failed for {sig.signature_url!r}: {exc}")

    return None


async def get_user_default_photo(user_id: UUID) -> bytes | None:
    """Return raw bytes of the user's default passport photo, or None."""
    photo = await PassportPhoto.get_or_none(
        user_id=user_id, is_default=True, deleted_at=None
    )
    if not photo:
        photo = await PassportPhoto.filter(
            user_id=user_id, deleted_at=None
        ).order_by("-created_at").first()
    if not photo or not photo.photo_url:
        logger.info(f"no passport photo found for user {user_id}")
        return None
    try:
        data = await fetch_file_bytes(photo.photo_url)
        logger.info(
            f"loaded passport photo for user {user_id} from {photo.photo_url!r} ({len(data)} bytes)"
        )
        return data
    except Exception as exc:
        logger.warning(f"photo file read failed for {photo.photo_url!r}: {exc}")
    return None


# ---- PDF parsing & stamping --------------------------------------------------


def _line_text(spans: list[dict[str, Any]]) -> str:
    return "".join(s.get("text", "") for s in spans)


def _spans_up_to(spans: list[dict[str, Any]], cutoff_index: int) -> str:
    return "".join(s.get("text", "") for s in spans[: cutoff_index + 1])


def _find_colon_span(spans: list[dict[str, Any]]) -> int | None:
    for i, span in enumerate(spans):
        if ":" in span.get("text", ""):
            return i
    return None


def _line_looks_like_sig_label(line_text: str) -> bool:
    """True iff `line_text` is plausibly a signature LABEL (not body prose).

    A sig label is short, often ends in a colon, and frequently has an
    underscore run right after it. Body sentences that happen to mention
    "signature" (e.g. "The parties' signature shall be witnessed") are long
    prose and should NOT trigger a stamp.

    Heuristic — pass if ANY of:
      * the line has ≤ 8 words AND contains `:` or `___`, OR
      * the line has ≤ 4 words total (label-only like "Signature of candidate"),
        OR
      * the line ends with `signature` followed by optional `:` (label tail).
    """
    s = line_text.strip()
    if not s:
        return False
    word_count = len(s.split())
    has_colon = ":" in s
    has_underscores = "___" in s
    if word_count <= 8 and (has_colon or has_underscores):
        return True
    if word_count <= 4:
        return True
    # "...sender's signature:" — label at the tail of an otherwise short line.
    if re.search(r"signature\s*:?\s*$", s, re.IGNORECASE) and word_count <= 6:
        return True
    return False


def _detect_and_stamp(
    pdf_bytes: bytes,
    vault: dict[str, str],
    full_legal_name: str | None,
    initials: str | None,
    signature_image: tuple[bytes, str] | None,
    photo_bytes: bytes | None = None,
) -> tuple[bytes, StampReport]:
    """Core engine: open the PDF, walk every text line, stamp matched values.

    The stamping point sits just past the trailing colon of the matched label.
    For signature/initials slots, we stamp at the same horizontal offset (image
    for graphical signature, falling back to the user's typed `full_legal_name`
    or `initials` if no image is available).
    """
    import fitz  # PyMuPDF

    report = StampReport()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        report.error = f"Could not open PDF: {exc}"
        return pdf_bytes, report

    report.pages = len(doc)

    for page_index in range(len(doc)):
        page = doc[page_index]
        # Tracks which vault field names we've already stamped on this page —
        # prevents the colon-pass and underscore-pass from double-stamping the
        # same field when a form has both styles.
        already_stamped: set[str] = set()

        # Detect scanned pages (no extractable text) and route them through
        # the OCR pass instead of the digital colon/underscore passes. Only
        # OCR the first N pages to keep total request latency reasonable.
        if _is_scanned_page(page) and page_index < _SCANNED_MAX_PAGES:
            logger.info(f"page {page_index}: appears to be scanned → OCR pass")
            _ocr_page_label_pass(
                page=page,
                page_index=page_index,
                vault=vault,
                report=report,
                already_stamped=already_stamped,
            )
            continue

        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:  # text only
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = _line_text(spans).strip()
                if not line_text:
                    continue

                # --- Photo detection (check first so "passport photograph"
                # doesn't get matched as a vault field "passport_number"). ---
                if photo_bytes and _PHOTO_KEYWORDS.search(line_text):
                    _stamp_photo(page, page_index, line, photo_bytes, report)
                    continue

                # --- Signature / initials detection (whole-line keyword) ---
                if _SIGNATURE_KEYWORDS.search(line_text):
                    # Two safety gates against over-stamping:
                    # 1. line must LOOK like a label, not body prose
                    # 2. don't exceed the global per-doc cap (defensive)
                    if (
                        _line_looks_like_sig_label(line_text)
                        and report.signatures_placed < _MAX_AUTO_SIGNATURES
                    ):
                        _stamp_signature_or_typed(
                            page, page_index, line, signature_image, full_legal_name, report
                        )
                    continue

                if _INITIALS_KEYWORDS.search(line_text) and initials:
                    _stamp_text_after_label(page, line, initials)
                    report.initials_placed += 1
                    continue

                # --- "Label: value" detection ---
                colon_idx = _find_colon_span(spans)
                if colon_idx is None:
                    continue

                # If the colon-bearing span ALSO contains a long underscore run
                # after the colon (forms that put `Label :- ___________` in a
                # single span), the span's bbox extends past the underscores —
                # stamping at bbox[2] would land in the right margin. Skip the
                # colon-pass and let the word-positional underscore-pass place
                # the value at the correct x.
                colon_span_text = spans[colon_idx].get("text", "")
                after_colon = colon_span_text.split(":", 1)[-1] if ":" in colon_span_text else ""
                if "___" in after_colon:
                    continue

                # SKIP IF FIELD IS ALREADY FILLED.
                #
                # Real-world prose constantly mentions "Resumption date: 12th
                # January 2026" or "Starting Date: 12th January 2026" — i.e.
                # the value is already typed into the document. Stamping today
                # right next to it produces the gross "...12th January 2026
                # 14/06/2026" double-date we saw on offer letters.
                #
                # Heuristic: combine the colon-bearing span tail with everything
                # in spans AFTER it. If the result has any meaningful non-blank,
                # non-underscore characters, treat the field as already filled
                # and skip.
                tail = (after_colon or "").strip()
                for tail_span in spans[colon_idx + 1:]:
                    tail += " " + (tail_span.get("text") or "")
                tail_meaningful = "".join(
                    ch for ch in tail
                    if ch.strip() and ch not in {"_", "-", "—", "–"}
                )
                # 2+ chars of real content = the value is there; don't overwrite.
                if len(tail_meaningful) >= 2:
                    continue

                # Label = text up to and including the colon-bearing span, before the colon char.
                label_raw = _spans_up_to(spans, colon_idx)
                label = label_raw.split(":")[0].strip()
                if not label or len(label) > 80:
                    continue

                # Resolve the value: vault match first, then the
                # Date/Place "specials" fallback for forms that ask for those
                # at the signature block.
                value: str | None = None
                field_name: str = "_special"
                segment = VaultSegment.PERSONAL
                confidence = 1.0

                hit = match_label_to_field(label)
                if hit:
                    segment, field_name, confidence = hit
                    value = vault.get(field_name)
                if not value:
                    special = _resolve_special_label_value(label, vault)
                    if special:
                        value = special
                        field_name = (
                            "_today_date" if "date" in label.lower()
                            else "_signing_place"
                        )

                if not value:
                    if not hit:
                        report.labels_unmatched.append(label)
                    continue

                if field_name in already_stamped:
                    continue

                colon_span = spans[colon_idx]
                bbox = colon_span.get("bbox") or (0, 0, 0, 0)
                stamp_x = float(bbox[2]) + 3.0           # just past the colon

                # Inherit the document's font/size/color so the stamp blends
                # in instead of looking pasted.
                font_props = _sniff_font_from_span(colon_span)
                fontsize = float(font_props.get("fontsize") or 10)
                # Baseline = bbox bottom minus a clearance scaled to fontsize.
                # `bbox[3]` includes the underscore stroke's bottom; subtracting
                # ~fontsize*0.35 lifts descenders (g, j, p, q, y) clear of the
                # underscore line so text doesn't visually overlap it.
                stamp_y_baseline = float(bbox[3]) - max(2.0, fontsize * 0.35)
                try:
                    # Best-effort erase of any underscore strokes under where
                    # our text will land. Background-aware: skips if the page
                    # isn't plain-white-ish (avoids punching white holes in
                    # documents with watermarks / colored fields).
                    erase_w = max(40.0, fontsize * len(value) * 0.55)
                    _erase_under(
                        page,
                        stamp_x,
                        stamp_y_baseline - fontsize * 0.15,  # strip = around the underscore baseline
                        erase_w,
                        fontsize * 0.5,
                    )
                    page.insert_text(
                        (stamp_x, stamp_y_baseline),
                        value,
                        fontsize=fontsize,
                        color=(0, 0, 0),
                    )
                    # Storage convention: placement.y is the TOP of the visual
                    # box (matches manual_stamp + editor overlay). Convert from
                    # baseline → top by subtracting one fontsize.
                    stamp_y_top = stamp_y_baseline - fontsize
                    report.fields_filled.append(
                        StampedField(
                            label=label,
                            field_name=field_name,
                            segment=segment,
                            value=value,
                            page=page_index,
                            x=stamp_x,
                            y=stamp_y_top,
                            match_confidence=confidence,
                            fontsize=fontsize,
                            font_family=font_props.get("font_family", "helv"),
                            bold=font_props.get("bold", False),
                            italic=font_props.get("italic", False),
                            color=font_props.get("color", "#000000"),
                        )
                    )
                    already_stamped.add(field_name)
                except Exception as exc:
                    logger.warning(f"insert_text failed on page {page_index}: {exc}")

        # After the colon-based dict pass, run the word-positional underscore
        # detector to catch label patterns the dict pass can't see.
        _detect_underscore_fields(
            page=page,
            page_index=page_index,
            vault=vault,
            report=report,
            already_stamped=already_stamped,
        )

    # Tier 2: heuristic fallbacks for forms that don't label their signature
    # / photo slots in text. These only run when nothing was placed label-wise.
    _try_heuristic_signature(
        doc=doc,
        signature_image=signature_image,
        fallback_typed_name=full_legal_name,
        report=report,
    )
    _try_heuristic_photo(
        doc=doc,
        photo_bytes=photo_bytes,
        report=report,
    )

    # Tier 3: last-resort default position for SIGNATURES ONLY. Most documents
    # we encounter (NDAs, contracts, application forms) need a signature, so
    # dropping one at the bottom-right of the last page is a reasonable
    # fallback the user can drag in the editor.
    #
    # We do NOT do the equivalent for passport photos — many contracts (NDAs,
    # leases, employment offers) have no photo slot at all, and stamping a
    # photo on them is jarring. Photo placement now requires either an
    # explicit label match or a heuristic drawn-rectangle match.
    _try_default_signature(
        doc=doc,
        signature_image=signature_image,
        fallback_typed_name=full_legal_name,
        report=report,
    )

    out_buf = io.BytesIO()
    doc.save(out_buf, deflate=True)
    doc.close()
    return out_buf.getvalue(), report


def _stamp_text_after_label(page: Any, line: dict[str, Any], text: str) -> None:
    """Stamp `text` just past the colon of the line (or at the line's right edge)."""
    spans = line.get("spans", [])
    colon_idx = _find_colon_span(spans)
    if colon_idx is not None:
        bbox = spans[colon_idx].get("bbox") or (0, 0, 0, 0)
        fontsize = float(spans[colon_idx].get("size") or 10)
        x = float(bbox[2]) + 3.0
        y = float(bbox[3]) - max(2.0, fontsize * 0.35)
    else:
        bbox = line.get("bbox") or (0, 0, 0, 0)
        fontsize = 10.0
        x = float(bbox[2]) + 5.0
        y = float(bbox[3]) - max(2.0, fontsize * 0.35)
        fontsize = 10.0
    page.insert_text((x, y), text, fontsize=fontsize, color=(0, 0, 0))


_SCANNED_MAX_PAGES = 10
"""Cap how many pages we OCR — image_to_data is ~2-5s per page even at 200dpi.
For most forms, fields are on the first page or two anyway."""


def _is_scanned_page(page: Any) -> bool:
    """A page is 'scanned' for our purposes if `page.get_text()` returns no
    meaningful text. That covers image-only PDFs, phone scans, and PDFs
    exported from imaging software."""
    try:
        text = page.get_text() or ""
        return len(text.strip()) < 30
    except Exception:
        return False


def _ocr_page_label_pass(
    page: Any,
    page_index: int,
    vault: dict[str, str],
    report: StampReport,
    already_stamped: set[str],
) -> None:
    """For pages with no extractable text, render the page, OCR with
    pytesseract.image_to_data, group words into lines, and try to match each
    line's label to a vault field. Stamp the value at the right edge of the
    label.

    Works best on form templates that have `Name _________` patterns —
    tesseract reliably picks up the label words. Underscore lines often OCR
    as dashes or get dropped, but that's OK; we anchor on the LABEL words
    and stamp just past them.
    """
    try:
        import pytesseract
        from PIL import Image as _PILImage
    except ImportError:
        logger.warning("pytesseract / Pillow not available — skipping OCR pass")
        return

    import io as _io
    import fitz

    # Render to image. 200dpi is a good balance between OCR accuracy and speed.
    try:
        pix = page.get_pixmap(dpi=200, alpha=False)
        img_bytes = pix.tobytes("png")
        img = _PILImage.open(_io.BytesIO(img_bytes))
    except Exception as exc:
        logger.warning(f"page {page_index}: render-for-OCR failed: {exc}")
        return

    try:
        data = pytesseract.image_to_data(
            img, output_type=pytesseract.Output.DICT, lang="eng"
        )
    except Exception as exc:
        logger.warning(f"page {page_index}: pytesseract failed: {exc}")
        return

    page_w = float(page.rect.width)
    img_w = float(pix.width)
    if img_w <= 0:
        return
    px_to_pt = page_w / img_w  # convert pixel coords → PDF points

    # Group recognized words by (block, par, line) to reconstruct lines.
    n = len(data.get("text", []))
    lines: dict[tuple[int, int, int], list[dict]] = {}
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if conf >= 0 and conf < 35:
            # Skip very-low-confidence noise from tesseract.
            continue
        key = (
            int(data["block_num"][i]),
            int(data["par_num"][i]),
            int(data["line_num"][i]),
        )
        lines.setdefault(key, []).append(
            {
                "text": text,
                "x": float(data["left"][i]) * px_to_pt,
                "y": float(data["top"][i]) * px_to_pt,
                "w": float(data["width"][i]) * px_to_pt,
                "h": float(data["height"][i]) * px_to_pt,
            }
        )

    placed_count = 0
    for words in lines.values():
        words.sort(key=lambda w: w["x"])
        line_text = " ".join(w["text"] for w in words).strip()
        if not line_text:
            continue

        # Strip a leading "1." / "2." numbering. Also strip OCR'd underscore /
        # dash runs that often come back from tesseract.
        cleaned = re.sub(r"^\s*\d{1,3}\.?\s*", "", line_text)
        # Take the label as everything before the first colon, OR everything
        # except trailing dash/underscore runs.
        if ":" in cleaned:
            label = cleaned.split(":")[0].strip()
        else:
            # Drop tokens that are 5+ underscores/dashes (OCR'd line markers).
            tokens = [
                t
                for t in cleaned.split()
                if not re.fullmatch(r"[-_~—–]{3,}", t)
            ]
            label = " ".join(tokens).strip()

        if not label or len(label) > 80:
            continue

        hit = match_label_to_field(label, fuzzy_threshold=0.78)
        if not hit:
            report.labels_unmatched.append(label)
            continue
        seg, field_name, confidence = hit
        if field_name in already_stamped:
            continue
        value = vault.get(field_name)
        if not value:
            continue

        # Anchor the stamp just past the rightmost word in the label cluster
        # (excludes the dash/underscore "tail" tokens we filtered above).
        label_words = [
            w for w in words
            if not re.fullmatch(r"[-_~—–]{3,}", w["text"])
        ]
        anchor = label_words[-1] if label_words else words[-1]
        stamp_x = anchor["x"] + anchor["w"] + 4.0
        fontsize = 10.0
        baseline_y = anchor["y"] + anchor["h"] - max(2.0, fontsize * 0.35)

        # Clamp to page rect.
        if stamp_x + 40 > page_w:
            stamp_x = max(2.0, page_w - 200)

        try:
            page.insert_text(
                (stamp_x, baseline_y),
                value,
                fontsize=fontsize,
                color=(0, 0, 0),
            )
            report.fields_filled.append(
                StampedField(
                    label=label,
                    field_name=field_name,
                    segment=seg,
                    value=value,
                    page=page_index,
                    x=stamp_x,
                    y=baseline_y - fontsize,
                    match_confidence=confidence,
                    fontsize=fontsize,
                    font_family="helv",
                    bold=False,
                    italic=False,
                    color="#000000",
                )
            )
            already_stamped.add(field_name)
            placed_count += 1
        except Exception as exc:
            logger.warning(f"OCR-pass stamp failed: {exc}")

    logger.info(
        f"OCR pass on page {page_index}: placed={placed_count} unmatched_lines={len(report.labels_unmatched)}"
    )


def _split_word_segments(text: str) -> list[tuple[bool, str]]:
    """Split a 'word' like '_______Category' into alternating segments.

    Returns list of (is_underscore_run, text). A run is contiguous '_' chars
    of length >= `_UNDERSCORE_RUN_MIN`. Shorter `_` sequences (rare typos)
    stay attached to surrounding text.
    """
    if not text:
        return []
    segments: list[tuple[bool, str]] = []
    pattern = re.compile(rf"_{{{_UNDERSCORE_RUN_MIN},}}")
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            segments.append((False, text[last:m.start()]))
        segments.append((True, m.group(0)))
        last = m.end()
    if last < len(text):
        segments.append((False, text[last:]))
    return segments


def _stamp_field_at(
    page: Any,
    label: str,
    x: float,
    y_baseline: float,
    fontsize: float,
    vault: dict[str, str],
    report: StampReport,
    page_index: int,
    already_stamped: set[str],
) -> bool:
    """Try to match `label` → vault field and stamp at the given position.

    Returns True if a stamp was placed. Records the unmatched label in the
    report when matching fails (so the UI can surface it).
    """
    cleaned = label.strip().rstrip(":-—").strip()
    if not cleaned or len(cleaned) > 80:
        return False

    # Vault match first.
    value: str | None = None
    field_name: str = "_special"
    seg: VaultSegment = VaultSegment.PERSONAL
    confidence: float = 1.0

    hit = match_label_to_field(cleaned)
    if hit:
        seg, field_name, confidence = hit
        if field_name in already_stamped:
            return False
        value = vault.get(field_name)

    # Special-label fallback (Date → today, Place → city) when no vault value.
    if not value:
        special = _resolve_special_label_value(cleaned, vault)
        if special:
            value = special
            field_name = (
                "_today_date" if "date" in cleaned.lower() else "_signing_place"
            )

    if not value:
        if not hit:
            report.labels_unmatched.append(cleaned)
        return False  # nothing to write

    # Inherit the form's font (family, size, weight, color) so the stamp
    # blends in. Deferred imports avoid the circular dependency with
    # manual_stamp.py (which imports vault helpers from this module).
    from app.common.services.manual_stamp import resolve_fontname, parse_color

    font_props = _sniff_font_for_y(page, y_baseline)
    if font_props:
        fontsize = float(font_props.get("fontsize") or fontsize)
    font_family = font_props.get("font_family", "helv")
    bold = bool(font_props.get("bold", False))
    italic = bool(font_props.get("italic", False))
    color_hex = font_props.get("color", "#000000")

    fontname = resolve_fontname(font_family, bold=bold, italic=italic)
    color_rgb = parse_color(color_hex)

    try:
        # Background-aware erase of underscores that would otherwise show
        # through under the stamped value.
        erase_w = max(40.0, fontsize * len(value) * 0.55)
        _erase_under(
            page,
            x,
            y_baseline - fontsize * 0.15,
            erase_w,
            fontsize * 0.5,
        )
        page.insert_text(
            (x, y_baseline),
            value,
            fontsize=fontsize,
            fontname=fontname,
            color=color_rgb,
        )
        # Storage convention: y is the TOP of the placement box. The
        # underscore-pass stamps at the baseline, so subtract one fontsize.
        report.fields_filled.append(
            StampedField(
                label=cleaned,
                field_name=field_name,
                segment=seg,
                value=value,
                page=page_index,
                x=x,
                y=y_baseline - fontsize,
                match_confidence=confidence,
                fontsize=fontsize,
                font_family=font_family,
                bold=bold,
                italic=italic,
                color=color_hex,
            )
        )
        already_stamped.add(field_name)
        return True
    except Exception as exc:
        logger.warning(f"underscore-field stamp failed: {exc}")
        return False


_SELF_REFERENCE_SUFFIXES = re.compile(
    # Phrases that follow an underscore blank and signal "this blank is the
    # signer's own name". Legal / oath / acknowledgment / affidavit prose.
    r"\b("
    r"the\s+undersigned"
    r"|hereby\s+(acknowledge|declare|certify|agree|consent|swear)"
    r"|do\s+(solemnly|hereby)\s+(swear|declare|affirm)"
    r"|of\s+full\s+age"
    r"|residing\s+at"
    r"|of\s+lawful\s+age"
    r"|being\s+duly\s+sworn"
    r"|swear\s+(and|that)"
    r"|am\s+the\s+(undersigned|applicant|petitioner|affiant)"
    r")\b",
    re.IGNORECASE,
)


def _might_be_self_reference_prefix(prefix: str) -> bool:
    """Cheap pre-filter for the queue.

    The full check (`_looks_like_self_reference`) needs both prefix AND
    suffix, but we only have the prefix at the moment a blank is encountered.
    Queue-time filter: keep blanks where the prefix could plausibly be the
    "I" of an "I, ___ the undersigned" pattern. Saves us from queueing every
    label-bearing underscore in a typical form (>100 per page sometimes).
    """
    pre = prefix.strip().rstrip(",").rstrip(":").strip().lower()
    if not pre:
        return True
    words = pre.split()
    return bool(words) and words[-1] == "i"


def _looks_like_self_reference(prefix: str, suffix: str) -> bool:
    """Heuristic for `I, ____ <self-reference phrase>` patterns.

    `prefix` = words immediately before the underscore run on the line.
    `suffix` = words immediately after the underscore run on the line.

    Triggers when prefix is essentially "I" / "I," / "that I,..." AND suffix
    contains a known self-reference phrase. Filling here uses full_legal_name.
    """
    pre = prefix.strip().rstrip(",").rstrip(":").strip().lower()
    if not pre:
        # Bare underscore line — we can't tell who it's for without suffix.
        return bool(_SELF_REFERENCE_SUFFIXES.search(suffix or ""))
    # Acceptable prefixes: "I", "that I", "Be it known that I", "Know all men ... I"
    prefix_words = pre.split()
    if not prefix_words:
        return False
    if prefix_words[-1] not in {"i"}:
        return False
    # Suffix must carry one of the trigger phrases — otherwise "I,___" might
    # just be a stray formatting artifact we shouldn't fill.
    return bool(_SELF_REFERENCE_SUFFIXES.search(suffix or ""))


def _detect_underscore_fields(
    page: Any,
    page_index: int,
    vault: dict[str, str],
    report: StampReport,
    already_stamped: set[str],
) -> None:
    """Scan a page for `Label ______` patterns using word positions.

    Handles three real-world cases:
      1. Inline: "Name" + "_____" both on the same visual y.
      2. Mixed-word: "_______Category" where the first segment is underscores
         filling field A and the trailing word is the label for field B.
      3. Multi-line: label sits alone on one y-row, underscores on the next.
      4. NEW: "I, ______ the undersigned" / "I, ______ hereby acknowledge"
         (and similar oath/declaration prose). The blank refers to the signer's
         own legal name even though the literal prefix is just "I,".

    For each underscore run found, the accumulated text words to its left
    (within the same y-row or from the previous row if the current row starts
    with underscores) are taken as the label.
    """
    words = page.get_text("words")
    if not words:
        return

    # Group words by visual y. PyMuPDF's `line_n` doesn't always reflect
    # what a human sees as one row (forms often place a label and its
    # underscores at the same baseline but in different `line_n`).
    y_groups: dict[int, list[tuple]] = {}
    for w in words:
        _, y0, *_ = w
        bucket = int(round(y0))
        y_groups.setdefault(bucket, []).append(w)

    # Merge buckets within 3 vertical points of each other.
    sorted_ys = sorted(y_groups.keys())
    rows: list[tuple[float, list[tuple]]] = []
    for y in sorted_ys:
        if rows and y - rows[-1][0] <= 3:
            rows[-1][1].extend(y_groups[y])
        else:
            rows.append((y, list(y_groups[y])))

    pending_label: str | None = None  # multi-line label awaiting underscores

    for _y, row_words in rows:
        row = sorted(row_words, key=lambda w: w[0])
        # Drop leading section numbers ("1.", "11.", "12.")
        while row and _NUMBER_PREFIX.match(row[0][4]):
            row = row[1:]
        if not row:
            continue

        label_buffer: list[str] = []
        # Position to use for the NEXT underscore stamp if we hit one here.
        any_underscore_in_row = False
        # Snapshot of recently-seen underscore stamps that may still need a
        # SUFFIX self-reference check (we don't know what comes after the
        # underscore until we finish the row). Each entry is the kwargs we'd
        # pass to _stamp_field_at IF the suffix turns out to confirm a name.
        pending_self_ref_blanks: list[dict] = []

        for word in row:
            x0, _y0, x1, y1, text, *_rest = word
            segments = _split_word_segments(text)

            # Pure label-only word — accumulate.
            if len(segments) == 1 and not segments[0][0]:
                label_buffer.append(text)
                # If we have an unresolved self-ref blank in this row and we
                # haven't matched yet, this word becomes part of its suffix.
                continue

            # Interpolate x positions within the word's bbox by character ratio.
            char_idx = 0
            word_chars = max(len(text), 1)
            for is_us, seg_text in segments:
                seg_x0 = x0 + (char_idx / word_chars) * (x1 - x0)

                if is_us:
                    any_underscore_in_row = True
                    label = " ".join(label_buffer).strip()

                    # If we have no inline label, try the pending multi-line one.
                    if not label and pending_label:
                        label = pending_label
                        pending_label = None

                    # Capture the prefix BEFORE we resolve the pending
                    # multi-line label — the self-ref pre-filter wants the
                    # in-row prefix, not the multi-line one (multi-line labels
                    # like "Full Legal Name" should never be self-ref'd anyway).
                    prefix_for_self_ref = " ".join(label_buffer).strip()

                    if label:
                        _fs = 10.0
                        _stamp_field_at(
                            page=page,
                            label=label,
                            x=seg_x0 + 1.0,
                            y_baseline=y1 - max(2.0, _fs * 0.35),
                            fontsize=_fs,
                            vault=vault,
                            report=report,
                            page_index=page_index,
                            already_stamped=already_stamped,
                        )

                    # Independently of whether we just stamped via the label,
                    # ALSO queue for the suffix-based self-reference check —
                    # for "I, ___ the undersigned" the prefix is "I," which
                    # _stamp_field_at can't match against the vault, so the
                    # blank stays empty without this fallback.
                    #
                    # Pre-filter keeps the queue small (only candidates that
                    # could plausibly be self-refs).
                    if _might_be_self_reference_prefix(prefix_for_self_ref):
                        pending_self_ref_blanks.append({
                            "prefix": prefix_for_self_ref,
                            "x": seg_x0 + 1.0,
                            "y_baseline": y1 - max(2.0, 10.0 * 0.35),
                            "fontsize": 10.0,
                        })
                    label_buffer = []
                else:
                    # Trailing label fragment within a mixed word — buffer it
                    # as the next field's label.
                    label_buffer.append(seg_text)

                char_idx += len(seg_text)

        # End of row — resolve any "I, ___ <self-ref>" blanks now that we know
        # what suffix appeared on this row (label_buffer holds the trailing
        # words). If the trailing text confirms self-reference, fill with the
        # user's full legal name.
        suffix_text = " ".join(label_buffer).strip()
        if pending_self_ref_blanks and (full_name := vault.get("full_legal_name")):
            # Deferred import to avoid the circular dep with manual_stamp.
            from app.common.services.manual_stamp import resolve_fontname, parse_color

            for blank in pending_self_ref_blanks:
                if _looks_like_self_reference(blank["prefix"], suffix_text):
                    if "full_legal_name" in already_stamped:
                        continue

                    # Sniff the font of the SURROUNDING line text so our fill
                    # blends in instead of looking pasted in default helv 10pt.
                    # We use the baseline's y as the target; _sniff_font_for_y
                    # rejects spans that are only "_-:." so it finds the
                    # neighbouring body text (e.g. "the undersigned, hereby...").
                    font_props = _sniff_font_for_y(page, blank["y_baseline"])
                    fontsize = float(font_props.get("fontsize") or blank["fontsize"])
                    font_family = font_props.get("font_family", "helv")
                    bold = bool(font_props.get("bold", False))
                    italic = bool(font_props.get("italic", False))
                    color_hex = font_props.get("color", "#000000")
                    fontname = resolve_fontname(font_family, bold=bold, italic=italic)
                    color_rgb = parse_color(color_hex)

                    # Erase the underscores we're about to write over.
                    erase_w = max(40.0, fontsize * len(full_name) * 0.55)
                    _erase_under(
                        page,
                        blank["x"],
                        blank["y_baseline"] - fontsize * 0.15,
                        erase_w,
                        fontsize * 0.5,
                    )

                    page.insert_text(
                        (blank["x"], blank["y_baseline"]),
                        full_name,
                        fontsize=fontsize,
                        fontname=fontname,
                        color=color_rgb,
                    )
                    report.fields_filled.append(StampedField(
                        label="I, ____ <undersigned>",
                        field_name="full_legal_name",
                        segment=VaultSegment.PERSONAL,
                        value=full_name,
                        page=page_index,
                        x=blank["x"],
                        y=blank["y_baseline"] - fontsize,
                        match_confidence=0.95,
                        fontsize=fontsize,
                        font_family=font_family,
                        bold=bold,
                        italic=italic,
                        color=color_hex,
                    ))
                    already_stamped.add("full_legal_name")
                    logger.info(
                        f"self-reference fill on page {page_index}: prefix={blank['prefix']!r} "
                        f"suffix={suffix_text!r} → {full_name} "
                        f"(font={font_family} {fontsize:.0f}pt)"
                    )

        if not any_underscore_in_row and label_buffer:
            # Row was label-only — remember it for the next row's underscores.
            candidate = " ".join(label_buffer).strip()
            if candidate and match_label_to_field(candidate, fuzzy_threshold=0.85):
                pending_label = candidate
            else:
                pending_label = None
        elif not label_buffer:
            pending_label = None
        # else: row had both underscores and trailing labels — pending_label
        # was already consumed where it mattered.


def _try_heuristic_signature(
    doc: Any,
    signature_image: tuple[bytes, str] | None,
    fallback_typed_name: str | None,
    report: StampReport,
) -> None:
    """Fallback signature placement when no `Signature:` label was found.

    Scans the last page for long underscore runs in the bottom 40% of the
    page — those are almost always a signature line on bio-data / job
    application / contract forms that don't bother labelling it.
    """
    if report.signatures_placed > 0:
        return
    if not signature_image and not fallback_typed_name:
        return
    if len(doc) == 0:
        return

    import fitz  # PyMuPDF

    page_index = len(doc) - 1
    page = doc[page_index]
    page_height = page.rect.height
    bottom_threshold = page_height * 0.55  # bottom ~45%

    words = page.get_text("words")
    candidates: list[tuple[float, float, float, float]] = []
    for w in words:
        x0, y0, x1, y1, text, *_rest = w
        if y0 < bottom_threshold:
            continue
        stripped = text.strip()
        # Pure underscore run, at least 12 chars and 150pt wide
        if not stripped or set(stripped) != {"_"} or len(stripped) < 12:
            continue
        if (x1 - x0) < 150:
            continue
        candidates.append((y0, x0, x1, y1))

    if not candidates:
        return

    # Pick the topmost candidate in the bottom section — most signature lines
    # sit above any "Date" / "Place" lines below them on forms.
    candidates.sort()
    y0, x0, x1, y1 = candidates[0]

    sig_width = min(180.0, max(120.0, x1 - x0))
    rect = fitz.Rect(x0, y0 - 25, x0 + sig_width, y0 + 5)

    last_page_index = len(doc) - 1
    try:
        placed = False
        if signature_image is not None:
            sig_bytes, _ = signature_image
            page.insert_image(rect, stream=sig_bytes, keep_proportion=True)
            report.signatures_placed += 1
            placed = True
            logger.info("heuristic signature placed on last page")
        elif fallback_typed_name:
            page.insert_text(
                (x0, y0 - 4), fallback_typed_name, fontsize=14, color=(0, 0, 0)
            )
            report.signatures_placed += 1
            placed = True
            logger.info("heuristic typed-name signature placed on last page")
        if placed:
            report.image_placements.append(StampedImage(
                kind="signature", page=last_page_index,
                x=float(rect.x0), y=float(rect.y0),
                width=float(rect.width), height=float(rect.height),
            ))
    except Exception as exc:
        logger.warning(f"heuristic signature placement failed: {exc}")


def _try_heuristic_photo(
    doc: Any,
    photo_bytes: bytes | None,
    report: StampReport,
) -> None:
    """Fallback photo placement when no PHOTOGRAPH label was found.

    Looks at page 1 for a roughly portrait-shaped rectangle drawn in the
    top-right area (the classic 'paste passport photo here' empty box).
    """
    if report.photos_placed > 0 or not photo_bytes or len(doc) == 0:
        return

    import fitz  # PyMuPDF

    page = doc[0]
    page_w = page.rect.width
    page_h = page.rect.height

    try:
        drawings = page.get_drawings()
    except Exception:
        return

    best: tuple[float, fitz.Rect] | None = None  # (area, rect)
    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        rw = rect.width
        rh = rect.height
        # Reasonable passport-box size: 60–250 pt wide, similar height.
        if rw < 60 or rw > 260 or rh < 60 or rh > 320:
            continue
        # Aspect close to square or portrait (passport is 3:4).
        aspect = rw / rh
        if aspect < 0.55 or aspect > 1.4:
            continue
        # Must sit in the top half AND right half of the page.
        if rect.y0 > page_h * 0.55:
            continue
        if rect.x1 < page_w * 0.45:
            continue
        area = rw * rh
        if best is None or area > best[0]:
            best = (area, rect)

    if best is None:
        return

    _, rect = best
    try:
        page.insert_image(rect, stream=photo_bytes, keep_proportion=True)
        report.photos_placed += 1
        report.image_placements.append(StampedImage(
            kind="photo", page=0,
            x=float(rect.x0), y=float(rect.y0),
            width=float(rect.width), height=float(rect.height),
        ))
        logger.info("heuristic photo placed in top-right box on page 1")
    except Exception as exc:
        logger.warning(f"heuristic photo placement failed: {exc}")


def _try_default_signature(
    doc: Any,
    signature_image: tuple[bytes, str] | None,
    fallback_typed_name: str | None,
    report: StampReport,
) -> None:
    """Tier-3 last-resort: drop the signature at the bottom-right of the last
    page when nothing else placed it. The user can drag it to a better spot in
    the editor — but this guarantees they at least *get* a signature on the
    document when they have one saved.
    """
    if report.signatures_placed > 0:
        return
    if not signature_image and not fallback_typed_name:
        return
    if len(doc) == 0:
        return

    import fitz

    page = doc[len(doc) - 1]
    page_w = page.rect.width
    page_h = page.rect.height
    sig_w, sig_h = 180.0, 36.0

    # Bottom-right corner, with breathing room for margins.
    x0 = page_w - sig_w - 50
    y0 = page_h - sig_h - 70
    x1 = x0 + sig_w
    y1 = y0 + sig_h
    rect = fitz.Rect(x0, y0, x1, y1)

    last_page_index = len(doc) - 1
    try:
        placed = False
        if signature_image is not None:
            sig_bytes, _ = signature_image
            page.insert_image(rect, stream=sig_bytes, keep_proportion=True)
            report.signatures_placed += 1
            placed = True
            logger.info(
                f"default-position signature placed at bottom-right of page "
                f"{last_page_index}: ({x0:.0f},{y0:.0f})→({x1:.0f},{y1:.0f})"
            )
        elif fallback_typed_name:
            page.insert_text(
                (x0, y1 - 4), fallback_typed_name, fontsize=14, color=(0, 0, 0)
            )
            report.signatures_placed += 1
            placed = True
            logger.info("default-position typed-name signature placed")
        if placed:
            report.image_placements.append(StampedImage(
                kind="signature", page=last_page_index,
                x=x0, y=y0, width=sig_w, height=sig_h,
            ))
    except Exception as exc:
        logger.warning(f"default signature placement failed: {exc}")


def _try_default_photo(
    doc: Any,
    photo_bytes: bytes | None,
    report: StampReport,
) -> None:
    """Tier-3 last-resort: drop the passport photo at the top-right of page 1
    when no labeled slot and no drawn rectangle was found. User can reposition.
    """
    if report.photos_placed > 0 or not photo_bytes or len(doc) == 0:
        return

    import fitz

    page = doc[0]
    page_w = page.rect.width
    photo_w, photo_h = 100.0, 130.0

    x0 = page_w - photo_w - 45
    y0 = 50.0
    x1 = x0 + photo_w
    y1 = y0 + photo_h
    rect = fitz.Rect(x0, y0, x1, y1)

    try:
        page.insert_image(rect, stream=photo_bytes, keep_proportion=True)
        report.photos_placed += 1
        report.image_placements.append(StampedImage(
            kind="photo", page=0,
            x=x0, y=y0, width=photo_w, height=photo_h,
        ))
        logger.info(
            f"default-position photo placed at top-right of page 1: "
            f"({x0:.0f},{y0:.0f})→({x1:.0f},{y1:.0f})"
        )
    except Exception as exc:
        logger.warning(f"default photo placement failed: {exc}")


def _stamp_photo(
    page: Any,
    page_index: int,
    line: dict[str, Any],
    photo_bytes: bytes,
    report: StampReport,
) -> None:
    """Stamp a standard passport-sized photo near the keyword line."""
    import fitz

    bbox = line.get("bbox") or (0, 0, 0, 0)
    x0 = float(bbox[0])
    y0 = float(bbox[3]) + 4.0
    x1 = x0 + _PHOTO_WIDTH_PT
    y1 = y0 + _PHOTO_HEIGHT_PT

    try:
        page.insert_image(fitz.Rect(x0, y0, x1, y1), stream=photo_bytes, keep_proportion=True)
        report.photos_placed += 1
        report.image_placements.append(StampedImage(
            kind="photo", page=page_index,
            x=x0, y=y0, width=x1 - x0, height=y1 - y0,
        ))
    except Exception as exc:
        logger.warning(f"photo stamp failed: {exc}")


def _stamp_signature_or_typed(
    page: Any,
    page_index: int,
    line: dict[str, Any],
    signature_image: tuple[bytes, str] | None,
    fallback_typed_name: str | None,
    report: StampReport,
) -> None:
    """Stamp a signature image (or fallback typed name) anchored on the span
    that contains the signature keyword.

    Form conventions vary:
      A) "Sign here: __________"  → sign ON the line, to the right of the colon
      B) "______________________
          Signature of candidate"  → sign ABOVE the label

    Using the full line bbox is wrong on multi-column rows like
    "Date ____  Signature of candidate" — the line right edge would push the
    stamp off-page. We anchor on the keyword span and place the rectangle
    above the label (with a fallback to below if it'd clip the top edge).
    """
    import fitz

    spans = line.get("spans", [])
    page_w = page.rect.width
    page_h = page.rect.height
    line_text = _line_text(spans)

    # Find the span that actually matched the keyword. Skip if no match
    # (defensive — the caller already verified the line text matched).
    keyword_span: dict[str, Any] | None = None
    for span in spans:
        if _SIGNATURE_KEYWORDS.search(span.get("text", "")):
            keyword_span = span
            break
    anchor_bbox = (keyword_span or {}).get("bbox") or line.get("bbox") or (0, 0, 0, 0)
    label_x0 = float(anchor_bbox[0])
    label_y0 = float(anchor_bbox[1])
    label_y1 = float(anchor_bbox[3])

    sig_w = 180.0
    sig_h = 36.0

    # Decide WHERE on the y-axis to put the sig. Two conventions exist:
    #
    #   (A) `Signature: __________________`  → write ON the underscore line,
    #       to the right of the colon. This is the NDA / contract convention.
    #   (B) `__________________
    #        Signature of candidate`         → write ABOVE the label (the line
    #       above the label IS the signing line). Common on bio-data forms.
    #
    # Heuristic: if the line itself has a colon followed by underscores, we
    # are in case A and should write past the colon. Otherwise case B.
    colon_idx = _find_colon_span(spans)
    text_after_colon = ""
    if colon_idx is not None:
        s = spans[colon_idx].get("text", "")
        text_after_colon = s.split(":", 1)[-1] if ":" in s else ""
    # Also count underscores anywhere later on the line (split-span case).
    has_underscore_after_colon = (
        "___" in text_after_colon
        or any("___" in (s.get("text", "")) for s in spans[(colon_idx or 0) + 1:])
    )

    if colon_idx is not None and has_underscore_after_colon:
        # CASE A: write ON the underscore line itself.
        #
        # We anchor at the LEFT edge of the underscore run, not at the right
        # edge of the colon-bearing span. Reason: PyMuPDF sometimes coalesces
        # "Signature: ___________" into one span — its bbox[2] is then the
        # right edge of the WHOLE run (past all the underscores), so
        # `colon_right + 4` lands beyond the signing line entirely.
        #
        # Resolution order:
        #   1. Underscores live in a LATER span → take that span's bbox.x0
        #      (cleanest case — colon and underscores in separate spans).
        #   2. Underscores live in the SAME span as the colon → interpolate
        #      the underscore's start x by character position within the span.
        #   3. Fall back to `colon_right + 4` (older behavior).
        label_x0_candidate: float | None = None
        for s in spans[(colon_idx + 1):]:
            stxt = s.get("text", "") or ""
            if "___" in stxt:
                bb = s.get("bbox") or (0, 0, 0, 0)
                # Find where the underscore run starts within this span and
                # offset accordingly (covers spans that lead with whitespace).
                us_start = stxt.find("___")
                span_w = max(1.0, float(bb[2]) - float(bb[0]))
                offset = (us_start / max(len(stxt), 1)) * span_w
                label_x0_candidate = float(bb[0]) + offset
                break
        if label_x0_candidate is None and "___" in text_after_colon:
            # Same-span case — interpolate within the colon span.
            colon_span = spans[colon_idx]
            stxt = colon_span.get("text", "") or ""
            bb = colon_span.get("bbox") or (0, 0, 0, 0)
            us_idx = stxt.find("___")
            if us_idx >= 0:
                span_w = max(1.0, float(bb[2]) - float(bb[0]))
                offset = (us_idx / max(len(stxt), 1)) * span_w
                label_x0_candidate = float(bb[0]) + offset
        if label_x0_candidate is None:
            colon_bbox = spans[colon_idx].get("bbox") or (0, 0, 0, 0)
            label_x0_candidate = float(colon_bbox[2]) + 4.0

        label_x0 = label_x0_candidate
        # Center the sig rect vertically around the line baseline.
        line_h = max(10.0, label_y1 - label_y0)
        y0 = label_y0 - (sig_h - line_h) / 2 - 2.0
        y1 = y0 + sig_h
    else:
        # CASE B: write ABOVE the label.
        y0 = label_y0 - sig_h - 4.0
        y1 = label_y0 - 4.0
        if y0 < 5:
            # No room above — fall back to BELOW the label.
            y0 = label_y1 + 4.0
            y1 = y0 + sig_h

    # Clamp horizontally so we never run off the right edge of the page.
    x0 = max(5.0, min(label_x0, page_w - sig_w - 5))
    x1 = min(x0 + sig_w, page_w - 5)
    _ = line_text  # silence "unused" — kept for future debugging hooks

    rect = fitz.Rect(x0, y0, x1, y1)
    logger.info(
        f"placing signature rect=({x0:.0f},{y0:.0f})→({x1:.0f},{y1:.0f}) "
        f"page={page_w:.0f}x{page_h:.0f} label_at=({label_x0:.0f},{label_y0:.0f})"
    )

    # In CASE A (on-line) the rectangle covers the underscore strokes;
    # erase first so they don't show through transparent areas of a PNG sig.
    # CASE B writes above the line — no underscores to clear there.
    if colon_idx is not None and has_underscore_after_colon:
        _erase_under(page, x0, label_y0 - 2.0, x1 - x0, (label_y1 - label_y0) + 6.0)

    if signature_image is not None:
        try:
            sig_bytes, _ = signature_image
            page.insert_image(rect, stream=sig_bytes, keep_proportion=True)
            report.signatures_placed += 1
            report.image_placements.append(StampedImage(
                kind="signature", page=page_index,
                x=x0, y=y0, width=x1 - x0, height=y1 - y0,
            ))
            return
        except Exception as exc:
            logger.warning(f"signature image stamp failed: {exc}")

    if fallback_typed_name:
        # Same erase logic for typed-name fallback (no sig image available).
        if colon_idx is not None and has_underscore_after_colon:
            _erase_under(page, x0, label_y0 - 2.0, sig_w, (label_y1 - label_y0) + 6.0)
        page.insert_text(
            (x0, y1 - 4.0),
            fallback_typed_name,
            fontsize=14,
            color=(0, 0, 0),
        )
        report.signatures_placed += 1
        # Typed-name fallback: we still record the rect for editor consistency
        # but mark kind=signature so re-stamping uses the user's saved image
        # if they've added one since.
        report.image_placements.append(StampedImage(
            kind="signature", page=page_index,
            x=x0, y=y0, width=x1 - x0, height=y1 - y0,
        ))


# ---- Top-level entry point ---------------------------------------------------


async def auto_affix_pdf_bytes(
    pdf_bytes: bytes,
    user_id: UUID,
) -> tuple[bytes, StampReport]:
    """Consumer flow: pull the user's vault + default signature + photo, stamp the PDF."""
    vault = await get_user_vault_dict(user_id)
    signature_image = await get_user_default_signature(user_id)
    photo_bytes = await get_user_default_photo(user_id)

    # Make this user's custom-vault fields visible to match_label_to_field
    # for the duration of this request. ContextVar scopes to the current
    # asyncio task, so concurrent requests for different users don't see
    # each other's custom fields.
    custom_aliases = await get_user_custom_field_aliases(user_id)
    token = _custom_fields_ctx.set(custom_aliases)
    try:
        return _detect_and_stamp(
            pdf_bytes=pdf_bytes,
            vault=vault,
            full_legal_name=vault.get("full_legal_name"),
            initials=vault.get("initials"),
            signature_image=signature_image,
            photo_bytes=photo_bytes,
        )
    finally:
        _custom_fields_ctx.reset(token)


def auto_affix_with_data(
    pdf_bytes: bytes,
    vault: dict[str, str],
    signature_image: tuple[bytes, str] | None = None,
    photo_bytes: bytes | None = None,
) -> tuple[bytes, StampReport]:
    """Stateless entry point used by the enterprise API.

    Caller passes the field-name → value dict, optionally a signature image,
    and optionally a passport photo. No database lookup happens — useful for
    B2B flows where the enterprise has its own copy of customer data.
    """
    return _detect_and_stamp(
        pdf_bytes=pdf_bytes,
        vault=vault,
        full_legal_name=vault.get("full_legal_name"),
        initials=vault.get("initials"),
        signature_image=signature_image,
        photo_bytes=photo_bytes,
    )

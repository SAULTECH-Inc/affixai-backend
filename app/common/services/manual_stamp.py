"""Manual placement stamping — used by the live editor.

Given the *original* PDF and an explicit list of `Placement` items, render a
stamped PDF. Unlike `auto_affix.py` which detects fields via OCR/text patterns,
this is purely positional — the frontend gives us exact (x, y, page) and we
write the value or image there.

Kinds supported:
  - text       — page.insert_text at (x, y) with optional fontsize/color
  - signature  — page.insert_image into a rect using the user's default signature
  - photo      — page.insert_image into a rect using the user's default photo
  - date       — same as text, value defaults to today in DD/MM/YYYY
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from loguru import logger

from app.common.services.auto_affix import (
    get_user_default_photo,
    get_user_default_signature,
    get_user_vault_dict,
)


PlacementKind = Literal[
    "text", "number", "date", "time", "initials", "signature", "photo"
]
_TEXTUAL_KINDS = {"text", "number", "date", "time", "initials"}


@dataclass
class Placement:
    kind: PlacementKind
    page: int
    x: float
    y: float
    # Text fields use these:
    value: str | None = None             # explicit text (overrides field_name lookup)
    field_name: str | None = None        # look up in vault
    fontsize: float = 10.0
    # Image fields use width/height for the rect:
    width: float = 180.0
    height: float = 36.0
    # Font customization for text placements:
    font_family: str = "helv"  # "helv" | "tiro" | "cour" | aliases (sans/serif/mono)
    bold: bool = False
    italic: bool = False
    color: str = "#000000"  # hex CSS color


# ---- font + color helpers --------------------------------------------------
#
# We support two flavors of font:
#
#   * PyMuPDF Standard-14 ("helv", "tiro", "cour") — built into every PDF
#     reader, zero overhead.
#
#   * Bundled OTF/TTF files under app/static/fonts/ — registered with each
#     page on first use via page.insert_font(). Used for calligraphic /
#     handwriting / signature-style faces that DocuSign-style apps offer.
#     The registered name lives only for the lifetime of that page object,
#     so we re-register every time we open a doc (cheap; PyMuPDF dedupes
#     internally by content hash).
#
# The frontend sends a family alias like "dancing" or "great_vibes"; we
# resolve that to either a Standard-14 fontname or the TTF path + the name
# we'll register it under.


from pathlib import Path

# Lives at app/static/fonts/. Resolved from this file's location so the
# package works from anywhere on disk.
_FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"


# Bundled custom fonts. Key = family alias the frontend sends.
# Value = (registered_name, filename_in_FONT_DIR, human_label, category)
#
# Category is used by the frontend to group them in the picker. We don't
# offer bold / italic variants for these because the script fonts already
# have a stylized weight — toggling bold/italic on them is undefined.
_CUSTOM_FONTS: dict[str, dict[str, str]] = {
    "dancing": {
        "registered_name": "dancing",
        "file": "DancingScript-Regular.ttf",
        "label": "Dancing Script",
        "category": "script",
    },
    "great_vibes": {
        "registered_name": "greatvb",
        "file": "GreatVibes-Regular.ttf",
        "label": "Great Vibes",
        "category": "calligraphy",
    },
    "caveat": {
        "registered_name": "caveat",
        "file": "Caveat-Regular.ttf",
        "label": "Caveat",
        "category": "handwriting",
    },
    "sacramento": {
        "registered_name": "sacrmt",
        "file": "Sacramento-Regular.ttf",
        "label": "Sacramento",
        "category": "signature",
    },
}


_FAMILY_ALIASES: dict[str, str] = {
    "helv": "helv", "helvetica": "helv", "arial": "helv", "sans": "helv",
    "sans-serif": "helv",
    "tiro": "tiro", "times": "tiro", "times-roman": "tiro", "serif": "tiro",
    "cour": "cour", "courier": "cour", "monospace": "cour", "mono": "cour",
    # Custom font aliases — all collapse to themselves so the resolve path
    # below detects them.
    **{k: k for k in _CUSTOM_FONTS},
}


def is_custom_family(family: str | None) -> bool:
    return (family or "").lower() in _CUSTOM_FONTS


def custom_font_path(family: str) -> str | None:
    """Return the absolute path to the TTF for a custom family, or None."""
    entry = _CUSTOM_FONTS.get(family.lower())
    if not entry:
        return None
    path = _FONT_DIR / entry["file"]
    return str(path) if path.exists() else None


def register_custom_font(page: Any, family: str) -> str | None:
    """Embed `family`'s TTF into `page` and return the registered fontname.

    Safe to call repeatedly — PyMuPDF dedupes by file. Returns None and logs
    if the family is unknown or its TTF is missing on disk (e.g. someone
    deleted the bundled assets).
    """
    entry = _CUSTOM_FONTS.get(family.lower())
    if not entry:
        return None
    path = _FONT_DIR / entry["file"]
    if not path.exists():
        logger.warning(
            f"custom font missing on disk: {family} → {path} (falling back to helv)"
        )
        return None
    try:
        page.insert_font(fontname=entry["registered_name"], fontfile=str(path))
        return entry["registered_name"]
    except Exception as exc:
        logger.warning(f"insert_font({family}) failed: {exc}")
        return None


def list_available_fonts() -> list[dict[str, str]]:
    """Expose the font catalog to the frontend.

    Includes Standard-14 entries even though they're always available,
    so a single endpoint drives the UI picker.
    """
    catalog: list[dict[str, str]] = [
        {"value": "helv", "label": "Helvetica", "category": "sans"},
        {"value": "tiro", "label": "Times", "category": "serif"},
        {"value": "cour", "label": "Courier", "category": "mono"},
    ]
    for alias, entry in _CUSTOM_FONTS.items():
        if (_FONT_DIR / entry["file"]).exists():
            catalog.append({
                "value": alias,
                "label": entry["label"],
                "category": entry["category"],
            })
    return catalog


def resolve_fontname(family: str | None, *, bold: bool, italic: bool) -> str:
    """Map (family, bold, italic) → a fontname that PyMuPDF can use.

    Standard-14 short names follow a fixed pattern — they are NOT
    suffixed with `-b`/`-o`. The canonical 4-letter shortnames per style:

      Helvetica:  helv (reg), hebo (bold), heit (italic), hebi (bold-italic)
      Times:      tiro (reg), tibo (bold), tiit (italic), tibi (bold-italic)
      Courier:    cour (reg), cobo (bold), coit (italic), cobi (bold-italic)

    For custom (bundled) families, bold/italic are ignored — we only ship the
    regular weight. They return the family alias unchanged; the caller MUST
    call `register_custom_font(page, family)` before insert_text() so the
    name resolves on the page.
    """
    fam = (family or "helv").lower()
    if fam in _CUSTOM_FONTS:
        return _CUSTOM_FONTS[fam]["registered_name"]

    base = _FAMILY_ALIASES.get(fam, "helv")
    table: dict[str, dict[tuple[bool, bool], str]] = {
        "helv": {
            (False, False): "helv", (True, False): "hebo",
            (False, True): "heit",  (True, True): "hebi",
        },
        "tiro": {
            (False, False): "tiro", (True, False): "tibo",
            (False, True): "tiit",  (True, True): "tibi",
        },
        "cour": {
            (False, False): "cour", (True, False): "cobo",
            (False, True): "coit",  (True, True): "cobi",
        },
    }
    return table.get(base, table["helv"])[(bold, italic)]


def parse_color(color: str | None) -> tuple[float, float, float]:
    """Hex like '#1a2b3c' → (r, g, b) floats in [0,1]. Defaults to black."""
    if not color:
        return (0.0, 0.0, 0.0)
    s = color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return (0.0, 0.0, 0.0)
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        return (0.0, 0.0, 0.0)


def _clamp_to_page(x: float, y: float, w: float, h: float, page_w: float, page_h: float) -> tuple[float, float]:
    """Clamp a placement's top-left so the rect stays inside the page."""
    x = max(2.0, min(x, page_w - w - 2.0))
    y = max(2.0, min(y, page_h - h - 2.0))
    return x, y


def _fit_fontsize(text: str, fontsize: float, max_width: float, *, floor: float = 7.0) -> float:
    """Shrink fontsize so the rendered text width fits in `max_width`.

    Uses a crude width estimate of `fontsize * len(text) * 0.5` (decent for
    proportional fonts at this granularity). Won't drop below `floor` so the
    text doesn't become unreadable — better to clip slightly than vanish.
    """
    if not text or max_width <= 0:
        return fontsize
    while fontsize > floor and fontsize * len(text) * 0.5 > max_width:
        fontsize -= 1
    return fontsize


def _dedup_placements(placements: list[Placement], *, threshold: float = 30.0) -> list[Placement]:
    """Remove placements where another of the same kind and page sits within
    `threshold` PDF points. Keeps the LAST entry in the input order (so a
    newly-dropped overlay supersedes an older one at the same spot).

    Protects against the user accidentally dropping two photos / signatures
    on top of each other, or auto-affix + manual placement combining badly.
    """
    if not placements:
        return placements
    kept: list[Placement] = []
    for p in reversed(placements):
        if any(
            q.kind == p.kind
            and q.page == p.page
            and abs(q.x - p.x) < threshold
            and abs(q.y - p.y) < threshold
            for q in kept
        ):
            continue
        kept.append(p)
    return list(reversed(kept))


@dataclass
class StampOutcome:
    placed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


async def restamp_pdf(
    pdf_bytes: bytes,
    placements: list[Placement],
    user_id: UUID,
) -> tuple[bytes, StampOutcome]:
    """Re-stamp a PDF using explicit placements provided by the client.

    The vault dict and default signature/photo are pulled once up-front so we
    don't hit the DB per placement.
    """
    import fitz

    vault = await get_user_vault_dict(user_id)
    sig_pair = await get_user_default_signature(user_id)
    photo_bytes = await get_user_default_photo(user_id)
    sig_bytes = sig_pair[0] if sig_pair else None

    outcome = StampOutcome()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        outcome.errors.append(f"Could not open PDF: {exc}")
        return pdf_bytes, outcome

    page_count = len(doc)

    # De-dup placements before stamping: if two photos / signatures / etc. of
    # the same kind sit within 30pt on the same page, keep only the latest.
    before = len(placements)
    placements = _dedup_placements(placements)
    if before != len(placements):
        logger.info(f"deduped placements: {before} → {len(placements)}")

    for p in placements:
        if p.page < 0 or p.page >= page_count:
            outcome.failed += 1
            outcome.errors.append(
                f"placement on page {p.page} is out of range ({page_count} pages)"
            )
            continue

        page = doc[p.page]

        page_w = page.rect.width
        page_h = page.rect.height

        try:
            if p.kind in _TEXTUAL_KINDS:
                text = _resolve_text(p, vault)
                if not text:
                    outcome.failed += 1
                    outcome.errors.append(
                        f"{p.kind} placement on page {p.page} has no value"
                    )
                    continue
                # Auto-shrink fontsize if the text would otherwise run past the
                # right edge of the page. Floor at 7pt so we never go too small.
                available_w = page_w - p.x - 4.0  # 4pt right margin
                effective_fontsize = _fit_fontsize(text, p.fontsize, available_w)
                if effective_fontsize != p.fontsize:
                    logger.info(
                        f"shrunk fontsize {p.fontsize}→{effective_fontsize} for "
                        f"{len(text)}-char text on page {p.page} (avail={available_w:.0f}pt)"
                    )
                # Clamp so a malformed/old placement doesn't stamp off-page.
                est_w = max(40.0, effective_fontsize * len(text) * 0.5)
                cx, cy = _clamp_to_page(p.x, p.y, est_w, effective_fontsize + 4, page_w, page_h)
                # Custom fonts (handwriting/calligraphy) must be embedded on
                # the page before insert_text can reference them. Fall back to
                # Helvetica if registration fails for any reason — better a
                # readable stamp in the wrong style than a broken render.
                fontname = resolve_fontname(p.font_family, bold=p.bold, italic=p.italic)
                if is_custom_family(p.font_family):
                    registered = register_custom_font(page, p.font_family)
                    if registered:
                        fontname = registered
                    else:
                        fontname = "helv"
                color = parse_color(p.color)
                page.insert_text(
                    (cx, cy + effective_fontsize),
                    text,
                    fontsize=effective_fontsize,
                    fontname=fontname,
                    color=color,
                )
                outcome.placed += 1

            elif p.kind == "signature":
                if not sig_bytes:
                    outcome.failed += 1
                    outcome.errors.append(
                        "signature placement requested but no default signature is saved"
                    )
                    continue
                cx, cy = _clamp_to_page(p.x, p.y, p.width, p.height, page_w, page_h)
                rect = fitz.Rect(cx, cy, cx + p.width, cy + p.height)
                page.insert_image(rect, stream=sig_bytes, keep_proportion=True)
                outcome.placed += 1

            elif p.kind == "photo":
                if not photo_bytes:
                    outcome.failed += 1
                    outcome.errors.append(
                        "photo placement requested but no default passport photo is saved"
                    )
                    continue
                cx, cy = _clamp_to_page(p.x, p.y, p.width, p.height, page_w, page_h)
                rect = fitz.Rect(cx, cy, cx + p.width, cy + p.height)
                page.insert_image(rect, stream=photo_bytes, keep_proportion=True)
                outcome.placed += 1

            else:
                outcome.failed += 1
                outcome.errors.append(f"unknown placement kind: {p.kind}")
        except Exception as exc:
            logger.warning(f"manual stamp failed: {exc}")
            outcome.failed += 1
            outcome.errors.append(str(exc))

    out = io.BytesIO()
    doc.save(out, deflate=True)
    doc.close()
    return out.getvalue(), outcome


def _resolve_text(p: Placement, vault: dict[str, str]) -> str:
    """Resolve the text value for a text/number/date/time/initials placement."""
    if p.value:
        return p.value
    if p.field_name and p.field_name in vault:
        return vault[p.field_name]
    if p.kind == "date":
        return date.today().strftime("%d/%m/%Y")
    if p.kind == "time":
        return datetime.now().strftime("%H:%M")
    if p.kind == "initials" and "initials" in vault:
        return vault["initials"]
    return ""

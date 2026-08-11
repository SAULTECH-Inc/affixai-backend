"""Phase 1 of the authoring editor: create PDFs from nothing, and manipulate
their pages.

Everything else in the document pipeline starts from a file the user uploaded.
This module is the other origin — a blank page the user builds on — plus the
page-level operations (add / duplicate / delete / reorder / rotate) that an
editor needs and an upload-only flow never did.

Rendering *content* onto pages stays in `manual_stamp.py`; this module only
deals with the pages themselves.
"""
from __future__ import annotations

import io

from loguru import logger

# Page dimensions in PDF points (1/72"). PyMuPDF has paper_size() but we keep
# an explicit table so the API surface is a closed set we validate against
# rather than whatever string a client sends.
PAGE_SIZES: dict[str, tuple[float, float]] = {
    "a4": (595.0, 842.0),
    "a3": (842.0, 1191.0),
    "a5": (420.0, 595.0),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "tabloid": (792.0, 1224.0),
}

DEFAULT_PAGE_SIZE = "a4"

# A document the editor has to render page thumbnails for, in a serverless
# function with a timeout. 200 is far past any real authored document and well
# short of anything that would wedge the renderer.
MAX_PAGES = 200


def resolve_page_size(name: str | None, *, landscape: bool = False) -> tuple[float, float]:
    """Map a page-size name to (width, height) in points.

    Raises ValueError on an unknown name so routes can surface a 400 listing
    the valid options.
    """
    key = (name or DEFAULT_PAGE_SIZE).strip().lower()
    if key not in PAGE_SIZES:
        raise ValueError(
            f"Unknown page size {name!r}. Use one of: {', '.join(sorted(PAGE_SIZES))}."
        )
    w, h = PAGE_SIZES[key]
    return (h, w) if landscape else (w, h)


def create_blank_pdf(
    *,
    page_size: str = DEFAULT_PAGE_SIZE,
    pages: int = 1,
    landscape: bool = False,
) -> bytes:
    """A valid, empty PDF of `pages` blank pages."""
    import fitz

    if pages < 1:
        raise ValueError("A document needs at least one page.")
    if pages > MAX_PAGES:
        raise ValueError(f"Too many pages: {pages}. The maximum is {MAX_PAGES}.")

    width, height = resolve_page_size(page_size, landscape=landscape)
    doc = fitz.open()
    try:
        for _ in range(pages):
            doc.new_page(width=width, height=height)
        buf = io.BytesIO()
        doc.save(buf)
        logger.info(
            f"created blank PDF: {pages} page(s) {page_size}"
            f"{' landscape' if landscape else ''}"
        )
        return buf.getvalue()
    finally:
        doc.close()


def page_count(pdf_bytes: bytes) -> int:
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def _save(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- Page operations -------------------------------------------------------
#
# Each returns (new_pdf_bytes, index_map).
#
# `index_map` maps every OLD page index to its NEW index, or None when the page
# is gone. Callers use it to migrate anything keyed by page number — most
# importantly `Document.field_placements`, whose entries would otherwise end up
# pointing at the wrong page (or off the end of the document) after a delete or
# reorder. Getting this wrong silently corrupts a user's layout, so the map is
# part of every operation's contract rather than an afterthought.


def add_pages(
    pdf_bytes: bytes,
    *,
    at: int | None = None,
    count: int = 1,
    page_size: str | None = None,
    landscape: bool = False,
) -> tuple[bytes, dict[int, int | None]]:
    """Insert `count` blank pages at index `at` (append when None).

    New pages default to the size of the page they're inserted after, so adding
    a page to a Letter document doesn't silently produce an A4 one.
    """
    import fitz

    if count < 1:
        raise ValueError("count must be at least 1.")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        if total + count > MAX_PAGES:
            raise ValueError(
                f"That would make {total + count} pages; the maximum is {MAX_PAGES}."
            )
        index = total if at is None else at
        if index < 0 or index > total:
            raise ValueError(f"Cannot insert at page {index}: document has {total} page(s).")

        if page_size:
            width, height = resolve_page_size(page_size, landscape=landscape)
        else:
            # Inherit from the neighbouring page (the one we insert after).
            ref = doc[max(0, index - 1)] if total else None
            if ref is not None:
                width, height = ref.rect.width, ref.rect.height
            else:
                width, height = resolve_page_size(DEFAULT_PAGE_SIZE, landscape=landscape)

        for offset in range(count):
            doc.new_page(pno=index + offset, width=width, height=height)

        # Pages at or after the insertion point shift right by `count`.
        index_map = {
            old: (old if old < index else old + count) for old in range(total)
        }
        return _save(doc), index_map
    finally:
        doc.close()


def duplicate_page(pdf_bytes: bytes, index: int) -> tuple[bytes, dict[int, int | None]]:
    """Copy page `index`, placing the copy immediately after it."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        if index < 0 or index >= total:
            raise ValueError(f"No page {index}: document has {total} page(s).")
        if total + 1 > MAX_PAGES:
            raise ValueError(f"Already at the {MAX_PAGES}-page maximum.")
        # fullcopy_page copies content and annotations, not just a reference.
        doc.fullcopy_page(index, to=index + 1)
        index_map = {
            old: (old if old <= index else old + 1) for old in range(total)
        }
        return _save(doc), index_map
    finally:
        doc.close()


def delete_page(pdf_bytes: bytes, index: int) -> tuple[bytes, dict[int, int | None]]:
    """Remove page `index`. Refuses to empty the document."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        if index < 0 or index >= total:
            raise ValueError(f"No page {index}: document has {total} page(s).")
        if total == 1:
            raise ValueError("Cannot delete the only page — a PDF needs at least one.")
        doc.delete_page(index)
        index_map: dict[int, int | None] = {}
        for old in range(total):
            if old == index:
                index_map[old] = None          # deleted
            elif old < index:
                index_map[old] = old
            else:
                index_map[old] = old - 1       # shifted left
        return _save(doc), index_map
    finally:
        doc.close()


def reorder_pages(
    pdf_bytes: bytes, order: list[int]
) -> tuple[bytes, dict[int, int | None]]:
    """Rearrange pages to `order`, a permutation of the existing indices.

    Requires a full permutation rather than a partial list: `select()` would
    happily drop any page omitted from the list, turning a mis-sent reorder
    into silent data loss.
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        if sorted(order) != list(range(total)):
            raise ValueError(
                f"order must list every page index exactly once "
                f"(expected a permutation of 0..{total - 1})."
            )
        doc.select(order)
        # order[new] = old  →  invert to old → new
        index_map: dict[int, int | None] = {old: new for new, old in enumerate(order)}
        return _save(doc), index_map
    finally:
        doc.close()


def rotate_page(
    pdf_bytes: bytes, index: int, degrees: int
) -> tuple[bytes, dict[int, int | None]]:
    """Rotate page `index` by a multiple of 90 degrees.

    Rotation doesn't move pages, so the index map is the identity — placements
    keep their page but their x/y no longer match the visual orientation. The
    editor re-reads coordinates from the rotated page, so we leave them alone
    rather than guessing at a transform.
    """
    import fitz

    if degrees % 90 != 0:
        raise ValueError("Rotation must be a multiple of 90 degrees.")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        if index < 0 or index >= total:
            raise ValueError(f"No page {index}: document has {total} page(s).")
        page = doc[index]
        page.set_rotation((page.rotation + degrees) % 360)
        return _save(doc), {old: old for old in range(total)}
    finally:
        doc.close()


def remap_placements(
    placements: list[dict] | None, index_map: dict[int, int | None]
) -> list[dict]:
    """Rewrite placement page numbers through `index_map`.

    Placements on a deleted page are dropped. Placements whose page isn't in
    the map are kept unchanged — an unknown page is more likely a client/server
    version skew than a reason to discard the user's work.
    """
    if not placements:
        return []
    out: list[dict] = []
    for p in placements:
        if not isinstance(p, dict):
            continue
        try:
            old = int(p.get("page", 0))
        except (TypeError, ValueError):
            out.append(p)
            continue
        if old not in index_map:
            out.append(p)
            continue
        new = index_map[old]
        if new is None:
            continue  # its page is gone
        out.append({**p, "page": new})
    return out

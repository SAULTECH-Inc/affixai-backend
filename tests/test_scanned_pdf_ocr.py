"""Tests for the scanned-PDF detection and the OCR pre-pass on pdf → docx.

pdf2docx reconstructs layout from the geometry of a PDF's text spans, so a
scanned page (image only, no text layer) produces an empty DOCX. These tests
cover the detection that decides whether an OCR pre-pass is needed, and that
`convert_document` actually routes through it — without requiring OCRmyPDF or
its tesseract/ghostscript binaries to be installed, since they can't be on
every host (notably Vercel).
"""
from __future__ import annotations

import pytest

from app.common.services import document_processing as dp


def _digital_pdf(text: str = "Hello structured world, this is selectable text.") -> bytes:
    """A PDF with a real text layer."""
    return dp.text_to_pdf(text, title="Digital")


def _scanned_pdf(pages: int = 2) -> bytes:
    """A PDF whose pages are images only — no extractable text.

    Built by rendering a digital PDF to pixmaps and re-inserting them as
    images, which is exactly what a scanner or phone photo produces.
    """
    import fitz

    src = fitz.open(stream=_digital_pdf("Scanned content here"), filetype="pdf")
    out = fitz.open()
    for _ in range(pages):
        pix = src[0].get_pixmap(dpi=72)
        page = out.new_page(width=pix.width, height=pix.height)
        page.insert_image(fitz.Rect(0, 0, pix.width, pix.height), pixmap=pix)
    src.close()
    data = out.tobytes()
    out.close()
    return data


# ---- Detection -------------------------------------------------------------


def test_stats_detect_digital_pdf():
    total, with_text = dp.pdf_text_layer_stats(_digital_pdf())
    assert total >= 1
    assert with_text == total  # every page has a usable text layer


def test_stats_detect_scanned_pdf():
    total, with_text = dp.pdf_text_layer_stats(_scanned_pdf(pages=2))
    assert total == 2
    assert with_text == 0  # image-only pages carry no text


def test_stats_ignores_trivial_text():
    """A page holding only a page number shouldn't count as a text layer."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((300, 800), "7", fontsize=9)
    data = doc.tobytes()
    doc.close()
    total, with_text = dp.pdf_text_layer_stats(data, min_chars_per_page=15)
    assert (total, with_text) == (1, 0)


def test_stats_on_garbage_bytes():
    assert dp.pdf_text_layer_stats(b"not a pdf at all") == (0, 0)


# ---- Routing ---------------------------------------------------------------


def test_digital_pdf_to_docx_skips_ocr(monkeypatch):
    """A PDF that already has text must not be rasterised through OCR."""
    called = {"ocr": False}

    def fake_ocr(*a, **k):
        called["ocr"] = True
        raise AssertionError("OCR should not run for a digital PDF")

    monkeypatch.setattr(dp, "ocr_pdf", fake_ocr)
    # Bypass the conversion cache so we exercise the routing every run.
    monkeypatch.setattr(dp, "_cache_get", lambda key: None)
    monkeypatch.setattr(dp, "_cache_set", lambda key, data: None)

    out = dp.convert_document(_digital_pdf(), "pdf", "docx")
    assert out[:2] == b"PK"
    assert called["ocr"] is False


def test_scanned_pdf_to_docx_runs_ocr_prepass(monkeypatch):
    """A scanned PDF must get an OCR layer before pdf2docx sees it."""
    seen = {}

    def fake_ocr(pdf_bytes, *, language="eng", force=False):
        seen["called"] = True
        seen["force"] = force
        # Hand back a digital PDF, standing in for an OCR'd one.
        return _digital_pdf("Recognised text from the scan")

    monkeypatch.setattr(dp, "ocr_pdf", fake_ocr)
    monkeypatch.setattr(dp, "_cache_get", lambda key: None)
    monkeypatch.setattr(dp, "_cache_set", lambda key, data: None)

    out = dp.convert_document(_scanned_pdf(), "pdf", "docx")
    assert seen.get("called") is True
    # skip_text mode (force=False) leaves already-digital pages alone, which
    # matters for PDFs that mix scanned and digital pages.
    assert seen["force"] is False
    assert out[:2] == b"PK"


def test_scanned_pdf_to_txt_does_not_need_ocrmypdf(monkeypatch):
    """pdf → txt already has a per-page Tesseract fallback, so it must not
    depend on OCRmyPDF being installed."""
    def boom(*a, **k):
        raise AssertionError("pdf→txt should not call ocr_pdf")

    monkeypatch.setattr(dp, "ocr_pdf", boom)
    monkeypatch.setattr(dp, "_cache_get", lambda key: None)
    monkeypatch.setattr(dp, "_cache_set", lambda key, data: None)
    # Doesn't assert on the text itself: whether tesseract is installed varies
    # by host. The point is that it returns without requiring OCRmyPDF.
    out = dp.convert_document(_scanned_pdf(pages=1), "pdf", "txt")
    assert isinstance(out, bytes)


# ---- Missing-dependency behaviour -----------------------------------------


def test_ocr_pdf_raises_runtime_error_when_unavailable(monkeypatch):
    """Without OCRmyPDF the caller gets a RuntimeError, which the route maps
    to 501 — the same treatment as a missing LibreOffice. An empty DOCX would
    be a worse outcome than a clear 'not available here'."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "ocrmypdf":
            raise ImportError("no module named ocrmypdf")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError) as exc:
        dp.ocr_pdf(_scanned_pdf(pages=1))
    msg = str(exc.value)
    assert "OCR is unavailable" in msg
    # The message has to say how to fix it.
    assert "ocr" in msg and "tesseract" in msg

"""Tests for the standalone converter endpoint + DOCX auto-sign routing.

`POST /documents/convert` is stateless: file in, converted bytes out, no
Document row. These tests deliberately stick to conversion paths that need
no external binaries (txt→docx, md→pdf, txt→pdf) so they run in CI without
LibreOffice installed. The docx→pdf path is covered by asserting the
*routing* through `docx_to_pdf`, with the converter itself stubbed.
"""
from __future__ import annotations

import uuid

import pytest

from app.core import security
from app.db.models.user import User, UserRole, UserStatus


@pytest.fixture
async def authed_user():
    """A plain active user with a bearer token.

    Note the converter endpoint uses `get_current_user`, NOT
    `require_active_subscription` — conversion is a utility available to
    every logged-in user, so no subscription scaffolding is needed here.
    """
    u = await User.create(
        email=f"convert-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Convert",
        last_name="Tester",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    return u, security.create_access_token({"sub": str(u.id)})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- Auth ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_requires_auth(client):
    r = await client.post(
        "/api/v1/documents/convert",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"target_format": "docx"},
    )
    assert r.status_code == 401


# ---- Happy paths (no external binaries needed) -----------------------------


@pytest.mark.asyncio
async def test_convert_txt_to_docx(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("notes.txt", b"Hello world\nSecond line\n", "text/plain")},
        data={"target_format": "docx"},
    )
    assert r.status_code == 200, r.text
    # .docx is a zip container — check the magic bytes rather than trusting
    # the content-type we set ourselves.
    assert r.content[:2] == b"PK"
    assert "notes.docx" in r.headers["content-disposition"]
    assert "wordprocessingml" in r.headers["content-type"]


@pytest.mark.asyncio
async def test_convert_md_to_pdf(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("readme.md", b"# Title\n\nSome **bold** body.\n", "text/markdown")},
        data={"target_format": "pdf"},
    )
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"%PDF"
    assert "readme.pdf" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_convert_detects_source_from_extension_when_mime_is_generic(
    client, authed_user
):
    """Browsers send application/octet-stream often enough that we can't rely
    on the MIME alone — the filename extension has to carry it."""
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("plan.md", b"# Plan\n", "application/octet-stream")},
        data={"target_format": "txt"},
    )
    assert r.status_code == 200, r.text
    # Markdown → txt strips the heading marker.
    assert b"#" not in r.content
    assert b"Plan" in r.content


# ---- Rejections ------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_rejects_unknown_target(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"target_format": "xlsx"},
    )
    assert r.status_code == 400
    assert "xlsx" in r.json()["detail"]


@pytest.mark.asyncio
async def test_convert_rejects_unknown_source(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("image.png", b"\x89PNG\r\n", "image/png")},
        data={"target_format": "pdf"},
    )
    assert r.status_code == 400
    assert "source format" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_convert_rejects_empty_file(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("a.txt", b"", "text/plain")},
        data={"target_format": "docx"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_convert_rejects_same_format_roundtrip(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/convert",
        headers=_auth(token),
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"target_format": "txt"},
    )
    assert r.status_code == 400


# ---- DOCX auto-sign routing ------------------------------------------------


@pytest.mark.asyncio
async def test_auto_affix_document_bytes_routes_docx_through_libreoffice(monkeypatch):
    """A DOCX must be converted to PDF before the stamp pipeline runs.

    We stub both `docx_to_pdf` and the PDF entry point so the assertion is
    purely about routing — no LibreOffice, no vault, no DB.
    """
    from app.common.services import auto_affix as mod
    from app.common.services import document_processing as dp

    calls: dict[str, object] = {}

    def fake_docx_to_pdf(data: bytes) -> bytes:
        calls["converted"] = data
        return b"%PDF-1.7 stubbed"

    async def fake_auto_affix_pdf_bytes(pdf_bytes, user_id):
        calls["stamped"] = pdf_bytes
        return pdf_bytes, mod.StampReport()

    monkeypatch.setattr(dp, "docx_to_pdf", fake_docx_to_pdf)
    monkeypatch.setattr(mod, "auto_affix_pdf_bytes", fake_auto_affix_pdf_bytes)

    out, report = await mod.auto_affix_document_bytes(
        b"PK\x03\x04 fake docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        uuid.uuid4(),
    )

    assert calls["converted"] == b"PK\x03\x04 fake docx"
    assert calls["stamped"] == b"%PDF-1.7 stubbed"
    assert out == b"%PDF-1.7 stubbed"
    assert report.error is None


@pytest.mark.asyncio
async def test_auto_affix_document_bytes_passes_pdf_straight_through(monkeypatch):
    from app.common.services import auto_affix as mod

    seen: dict[str, object] = {}

    async def fake_auto_affix_pdf_bytes(pdf_bytes, user_id):
        seen["pdf"] = pdf_bytes
        return pdf_bytes, mod.StampReport()

    monkeypatch.setattr(mod, "auto_affix_pdf_bytes", fake_auto_affix_pdf_bytes)

    await mod.auto_affix_document_bytes(b"%PDF-1.4 real", "application/pdf", uuid.uuid4())
    assert seen["pdf"] == b"%PDF-1.4 real"


@pytest.mark.asyncio
async def test_auto_affix_document_bytes_rejects_other_formats():
    from app.common.services import auto_affix as mod

    with pytest.raises(ValueError):
        await mod.auto_affix_document_bytes(b"\x89PNG\r\n", "image/png", uuid.uuid4())

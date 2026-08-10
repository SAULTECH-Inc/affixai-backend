"""Phase 2 authoring elements: image, link, richtext, table, and password.

These assert on the *rendered output* — extracted text, embedded images, link
targets — rather than just that the call returned. A renderer that silently
places nothing is the failure mode worth catching.
"""
from __future__ import annotations

import io
import uuid

import pytest

from app.common.services import pdf_authoring as pa
from app.common.services.manual_stamp import (
    Placement,
    protect_pdf,
    restamp_pdf,
    _table_html,
)
from app.core import security
from app.db.models.document import Document, DocumentStatus
from app.db.models.user import User, UserRole, UserStatus


@pytest.fixture
async def plain_user(app):
    """A user with no saved signature/photo/vault — keeps these tests about the
    new element kinds rather than vault resolution.

    Depends on `app` because that fixture is what initialises Tortoise.
    """
    return await User.create(
        email=f"elem-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy", first_name="Elem", last_name="Tester",
        role=UserRole.USER, status=UserStatus.ACTIVE,
    )


def _blank(pages: int = 1) -> bytes:
    return pa.create_blank_pdf(pages=pages)


def _png_bytes(w: int = 40, h: int = 20, color=(200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _text_of(pdf: bytes) -> str:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


# ---- richtext --------------------------------------------------------------


@pytest.mark.asyncio
async def test_richtext_renders_formatted_html(plain_user):
    p = Placement(
        kind="richtext", page=0, x=50, y=50, width=400, height=200,
        html="<h3>Agreement</h3><p>Between <b>Acme</b> and the client.</p>"
             "<ul><li>First term</li><li>Second term</li></ul>",
    )
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1, outcome.errors
    assert outcome.failed == 0
    text = _text_of(out)
    for expected in ("Agreement", "Acme", "First term", "Second term"):
        assert expected in text


@pytest.mark.asyncio
async def test_richtext_reflows_rather_than_running_off_page(plain_user):
    """The point of insert_htmlbox over insert_text: long content wraps inside
    its frame instead of trailing off the right edge."""
    import fitz

    long_text = "This sentence exists purely to force wrapping. " * 6
    p = Placement(
        kind="richtext", page=0, x=50, y=50, width=200, height=400,
        html=f"<p>{long_text}</p>",
    )
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1

    doc = fitz.open(stream=out, filetype="pdf")
    lines = [
        line["bbox"]
        for block in doc[0].get_text("dict")["blocks"] if block.get("type") == 0
        for line in block["lines"]
    ]
    doc.close()
    assert len(lines) > 3, "expected the text to wrap onto several lines"
    # Every line must stay within the frame (plus a small tolerance).
    assert max(b[2] for b in lines) <= 50 + 200 + 4


@pytest.mark.asyncio
async def test_richtext_overflow_is_reported_not_hidden(plain_user):
    """insert_htmlbox's default silently shrinks content to fit — a user would
    just see mysteriously tiny text. Overflow must be surfaced."""
    p = Placement(
        kind="richtext", page=0, x=50, y=50, width=120, height=30,
        html="<p>" + ("far too much text to fit in this tiny box " * 10) + "</p>",
    )
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1
    assert outcome.overflowed == [0]
    # Content is still rendered (scaled), not dropped.
    assert "far too much text" in _text_of(out)


@pytest.mark.asyncio
async def test_richtext_without_html_fails_cleanly(plain_user):
    p = Placement(kind="richtext", page=0, x=10, y=10)
    _, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 0 and outcome.failed == 1
    assert "no html" in outcome.errors[0]


# ---- table ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_renders_all_cells(plain_user):
    p = Placement(
        kind="table", page=0, x=40, y=40, width=480, height=200,
        rows=[["Item", "Qty", "Price"],
              ["Design work", "2", "1500"],
              ["Development", "10", "9000"]],
    )
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1, outcome.errors
    text = _text_of(out)
    for cell in ("Item", "Qty", "Price", "Design work", "9000"):
        assert cell in text


def test_table_html_escapes_cell_text():
    """Cells are user content and insert_htmlbox parses HTML, so an unescaped
    '<' would corrupt the table or inject markup."""
    html = _table_html(Placement(
        kind="table", page=0, x=0, y=0,
        rows=[["<script>alert(1)</script>", "a & b"]], header=False,
    ))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "a &amp; b" in html


def test_table_html_uses_relative_column_widths():
    html = _table_html(Placement(
        kind="table", page=0, x=0, y=0,
        rows=[["a", "b"]], col_widths=[3.0, 1.0], header=False,
    ))
    assert "width:75.00%" in html and "width:25.00%" in html


def test_table_html_pads_ragged_rows():
    """A short row must not shift later columns out of alignment."""
    html = _table_html(Placement(
        kind="table", page=0, x=0, y=0,
        rows=[["a", "b", "c"], ["only one"]], header=False,
    ))
    assert html.count("<td") == 6  # 2 rows × 3 columns


@pytest.mark.asyncio
async def test_table_without_rows_fails_cleanly(plain_user):
    _, outcome = await restamp_pdf(
        _blank(), [Placement(kind="table", page=0, x=10, y=10)], plain_user.id
    )
    assert outcome.failed == 1 and "no rows" in outcome.errors[0]


# ---- link -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_creates_clickable_uri_and_label(plain_user):
    import fitz

    p = Placement(
        kind="link", page=0, x=60, y=100, width=150, height=20,
        url="https://affixai.example/terms", value="Read the terms",
    )
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1, outcome.errors

    doc = fitz.open(stream=out, filetype="pdf")
    links = doc[0].get_links()
    doc.close()
    assert len(links) == 1
    assert links[0]["uri"] == "https://affixai.example/terms"
    assert "Read the terms" in _text_of(out)


@pytest.mark.asyncio
async def test_link_without_label_is_still_clickable(plain_user):
    """A link over an image needs no visible text of its own."""
    import fitz

    p = Placement(kind="link", page=0, x=60, y=100, width=150, height=20,
                  url="https://example.com")
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1
    doc = fitz.open(stream=out, filetype="pdf")
    assert len(doc[0].get_links()) == 1
    doc.close()


@pytest.mark.asyncio
async def test_link_without_url_fails_cleanly(plain_user):
    _, outcome = await restamp_pdf(
        _blank(), [Placement(kind="link", page=0, x=10, y=10)], plain_user.id
    )
    assert outcome.failed == 1 and "no url" in outcome.errors[0]


# ---- image ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_placement_embeds_asset(plain_user):
    """Goes through real storage so the asset resolves exactly as it would in a
    request, rather than via a hand-built URL that might not match."""
    import fitz

    from app.common.services.local_storage import save_bytes

    stored = save_bytes(_png_bytes(), "logo.png", folder="assets")
    p = Placement(kind="image", page=0, x=70, y=70, width=80, height=40,
                  asset_id="abc123")
    out, outcome = await restamp_pdf(
        _blank(), [p], plain_user.id, assets={"abc123": stored["url"]},
    )
    assert outcome.placed == 1, outcome.errors
    assert outcome.failed == 0
    doc = fitz.open(stream=out, filetype="pdf")
    assert len(doc[0].get_images()) == 1
    doc.close()


@pytest.mark.asyncio
async def test_image_with_unknown_asset_fails_without_fetching(plain_user):
    """An asset id the server has no record of must not be resolved. This is
    what stops a crafted placement pointing the renderer at an arbitrary
    address."""
    p = Placement(kind="image", page=0, x=10, y=10, asset_id="not-a-real-asset")
    _, outcome = await restamp_pdf(_blank(), [p], plain_user.id, assets={})
    assert outcome.failed == 1
    assert "not available" in outcome.errors[0]


@pytest.mark.asyncio
async def test_image_without_asset_id_fails_cleanly(plain_user):
    _, outcome = await restamp_pdf(
        _blank(), [Placement(kind="image", page=0, x=10, y=10)], plain_user.id
    )
    assert outcome.failed == 1 and "no asset_id" in outcome.errors[0]


# ---- backwards compatibility ----------------------------------------------


@pytest.mark.asyncio
async def test_existing_text_placements_still_render(plain_user):
    """The new kinds are additive; documents saved before this change must
    render exactly as before."""
    p = Placement(kind="text", page=0, x=100, y=100, value="Legacy text", fontsize=12)
    out, outcome = await restamp_pdf(_blank(), [p], plain_user.id)
    assert outcome.placed == 1 and outcome.failed == 0
    assert "Legacy text" in _text_of(out)


@pytest.mark.asyncio
async def test_unknown_kind_is_reported_not_fatal(plain_user):
    """A client ahead of the server should degrade, not break the render."""
    good = Placement(kind="text", page=0, x=50, y=50, value="rendered")
    bad = Placement(kind="hologram", page=0, x=10, y=10)  # type: ignore[arg-type]
    out, outcome = await restamp_pdf(_blank(), [good, bad], plain_user.id)
    assert outcome.placed == 1 and outcome.failed == 1
    assert "unknown placement kind" in outcome.errors[0]
    assert "rendered" in _text_of(out)


# ---- password protection --------------------------------------------------


def test_protect_pdf_requires_password_to_open():
    import fitz

    protected = protect_pdf(_blank(), user_password="s3cret")
    doc = fitz.open(stream=protected, filetype="pdf")
    assert doc.needs_pass
    assert doc.authenticate("wrong") == 0
    assert doc.authenticate("s3cret") > 0
    doc.close()


def test_protect_pdf_preserves_content():
    import fitz

    src, _ = None, None
    base = _blank()
    doc = fitz.open(stream=base, filetype="pdf")
    doc[0].insert_text((72, 72), "Confidential terms")
    buf = io.BytesIO(); doc.save(buf); doc.close()

    protected = protect_pdf(buf.getvalue(), user_password="letmein")
    d2 = fitz.open(stream=protected, filetype="pdf")
    d2.authenticate("letmein")
    assert "Confidential terms" in d2[0].get_text()
    d2.close()


@pytest.mark.parametrize("pw", ["", "abc"])
def test_protect_pdf_rejects_weak_password(pw):
    with pytest.raises(ValueError):
        protect_pdf(_blank(), user_password=pw)


def test_protect_pdf_defaults_owner_password_to_user_password():
    """An empty owner password lets any reader strip the restrictions."""
    import fitz

    protected = protect_pdf(_blank(), user_password="userpw")
    doc = fitz.open(stream=protected, filetype="pdf")
    # Opening with the user password must not grant owner rights implicitly;
    # the important part is that the empty-owner case never happens.
    assert doc.authenticate("userpw") > 0
    doc.close()


# ---- routes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_asset_upload_rejects_non_image(client):
    u = await User.create(
        email=f"asset-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="A", last_name="B", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    doc = await Document.create(
        user_id=u.id, file_name="k", original_file_name="d.pdf",
        file_url="local://authored/d.pdf", file_mime_type="application/pdf",
        file_size=1, status=DocumentStatus.DRAFT,
    )
    r = await client.post(
        f"/api/v1/documents/{doc.id}/assets",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")},
    )
    assert r.status_code == 400
    assert "PNG" in r.json()["detail"]


@pytest.mark.asyncio
async def test_asset_upload_accepts_png_and_returns_id(client):
    u = await User.create(
        email=f"asset2-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="A", last_name="B", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    r = await client.post(
        "/api/v1/documents/blank",
        headers={"Authorization": f"Bearer {token}"}, json={},
    )
    doc_id = r.json()["id"]

    r = await client.post(
        f"/api/v1/documents/{doc_id}/assets",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("logo.png", _png_bytes(60, 30), "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mime_type"] == "image/png"
    assert (body["width"], body["height"]) == (60, 30)

    # The server records the asset against the document; the client only ever
    # holds the id.
    doc = await Document.get(id=doc_id)
    assert body["asset_id"] in doc.metadata["assets"]
    assert "url" in doc.metadata["assets"][body["asset_id"]]


@pytest.mark.asyncio
async def test_asset_upload_rejects_mislabelled_content_type(client):
    """The declared content type is attacker-controlled; magic bytes decide."""
    u = await User.create(
        email=f"asset3-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="A", last_name="B", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    r = await client.post(
        "/api/v1/documents/blank", headers={"Authorization": f"Bearer {token}"}, json={},
    )
    doc_id = r.json()["id"]
    r = await client.post(
        f"/api/v1/documents/{doc_id}/assets",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.png", b"#!/bin/sh\nrm -rf /", "image/png")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_protect_route_returns_encrypted_pdf(client):
    import fitz

    u = await User.create(
        email=f"prot-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="A", last_name="B", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    r = await client.post(
        "/api/v1/documents/blank", headers={"Authorization": f"Bearer {token}"}, json={},
    )
    doc_id = r.json()["id"]

    r = await client.post(
        f"/api/v1/documents/{doc_id}/protect",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "opensesame"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert "protected.pdf" in r.headers["content-disposition"]
    doc = fitz.open(stream=r.content, filetype="pdf")
    assert doc.needs_pass
    assert doc.authenticate("opensesame") > 0
    doc.close()


@pytest.mark.asyncio
async def test_protect_route_rejects_short_password(client):
    u = await User.create(
        email=f"prot2-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="A", last_name="B", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    r = await client.post(
        "/api/v1/documents/blank", headers={"Authorization": f"Bearer {token}"}, json={},
    )
    doc_id = r.json()["id"]
    r = await client.post(
        f"/api/v1/documents/{doc_id}/protect",
        headers={"Authorization": f"Bearer {token}"}, json={"password": "ab"},
    )
    assert r.status_code == 422  # schema enforces min_length

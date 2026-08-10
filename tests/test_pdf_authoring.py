"""Phase 1 authoring: blank documents and page operations.

The interesting risk here isn't producing a valid PDF — it's that a page
operation silently corrupts the user's layout. Placements are keyed by page
index, so deleting or reordering pages has to migrate them; getting it wrong
leaves fields pointing at the wrong page (or off the end of the document) with
no error. Most of these tests are about that.
"""
from __future__ import annotations

import uuid

import pytest

from app.common.services import pdf_authoring as pa
from app.core import security
from app.db.models.document import Document, DocumentStatus
from app.db.models.user import User, UserRole, UserStatus


@pytest.fixture
async def authed_user():
    u = await User.create(
        email=f"author-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Author",
        last_name="Tester",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    return u, security.create_access_token({"sub": str(u.id)})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _placement(page: int, name: str) -> dict:
    return {"kind": "text", "page": page, "x": 100.0, "y": 200.0, "value": name}


# ---- Blank creation --------------------------------------------------------


def test_create_blank_pdf_defaults_to_a4():
    data = pa.create_blank_pdf()
    assert data[:4] == b"%PDF"
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    assert doc.page_count == 1
    assert round(doc[0].rect.width) == 595 and round(doc[0].rect.height) == 842
    doc.close()


def test_create_blank_pdf_landscape_swaps_dimensions():
    import fitz

    doc = fitz.open(stream=pa.create_blank_pdf(page_size="letter", landscape=True),
                    filetype="pdf")
    assert round(doc[0].rect.width) == 792 and round(doc[0].rect.height) == 612
    doc.close()


def test_create_blank_pdf_multi_page():
    assert pa.page_count(pa.create_blank_pdf(pages=5)) == 5


@pytest.mark.parametrize("kwargs", [{"pages": 0}, {"pages": 500}, {"page_size": "a9"}])
def test_create_blank_pdf_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        pa.create_blank_pdf(**kwargs)


def test_unknown_page_size_error_lists_valid_options():
    with pytest.raises(ValueError) as exc:
        pa.resolve_page_size("foolscap")
    assert "a4" in str(exc.value) and "letter" in str(exc.value)


# ---- Page ops: index maps --------------------------------------------------


def test_add_pages_shifts_following_indices():
    src = pa.create_blank_pdf(pages=3)
    out, imap = pa.add_pages(src, at=1, count=2)
    assert pa.page_count(out) == 5
    # page 0 stays; pages 1,2 move right by 2
    assert imap == {0: 0, 1: 3, 2: 4}


def test_add_pages_appends_by_default():
    out, imap = pa.add_pages(pa.create_blank_pdf(pages=2), count=1)
    assert pa.page_count(out) == 3
    assert imap == {0: 0, 1: 1}  # nothing moved


def test_add_pages_inherits_neighbour_size():
    """Adding a page to a Letter document must not produce an A4 page."""
    import fitz

    src = pa.create_blank_pdf(page_size="letter", pages=1)
    out, _ = pa.add_pages(src, count=1)
    doc = fitz.open(stream=out, filetype="pdf")
    assert round(doc[1].rect.width) == 612 and round(doc[1].rect.height) == 792
    doc.close()


def test_duplicate_page_index_map():
    out, imap = pa.duplicate_page(pa.create_blank_pdf(pages=3), 0)
    assert pa.page_count(out) == 4
    assert imap == {0: 0, 1: 2, 2: 3}


def test_delete_page_marks_removed_and_shifts_rest():
    out, imap = pa.delete_page(pa.create_blank_pdf(pages=4), 1)
    assert pa.page_count(out) == 3
    assert imap == {0: 0, 1: None, 2: 1, 3: 2}


def test_delete_refuses_to_empty_document():
    with pytest.raises(ValueError) as exc:
        pa.delete_page(pa.create_blank_pdf(pages=1), 0)
    assert "at least one" in str(exc.value)


def test_reorder_inverts_to_old_to_new():
    # order[new] = old  →  new order is [2,0,1]
    out, imap = pa.reorder_pages(pa.create_blank_pdf(pages=3), [2, 0, 1])
    assert pa.page_count(out) == 3
    assert imap == {2: 0, 0: 1, 1: 2}


def test_reorder_requires_full_permutation():
    """A partial list would make select() drop pages — silent data loss."""
    src = pa.create_blank_pdf(pages=3)
    for bad in ([0, 1], [0, 1, 1], [0, 1, 5]):
        with pytest.raises(ValueError) as exc:
            pa.reorder_pages(src, bad)
        assert "permutation" in str(exc.value)


def test_rotate_is_identity_map_and_persists():
    import fitz

    out, imap = pa.rotate_page(pa.create_blank_pdf(pages=2), 1, 90)
    assert imap == {0: 0, 1: 1}
    doc = fitz.open(stream=out, filetype="pdf")
    assert doc[1].rotation == 90
    doc.close()


def test_rotate_rejects_non_right_angles():
    with pytest.raises(ValueError):
        pa.rotate_page(pa.create_blank_pdf(), 0, 45)


@pytest.mark.parametrize("op,args", [
    ("duplicate_page", (9,)), ("delete_page", (9,)), ("rotate_page", (9, 90)),
])
def test_ops_reject_out_of_range_index(op, args):
    with pytest.raises(ValueError):
        getattr(pa, op)(pa.create_blank_pdf(pages=2), *args)


# ---- Placement remapping ---------------------------------------------------


def test_remap_drops_placements_on_deleted_page():
    placements = [_placement(0, "keep"), _placement(1, "gone"), _placement(2, "shift")]
    _, imap = pa.delete_page(pa.create_blank_pdf(pages=3), 1)
    out = pa.remap_placements(placements, imap)
    assert [p["value"] for p in out] == ["keep", "shift"]
    assert [p["page"] for p in out] == [0, 1]


def test_remap_follows_reorder():
    placements = [_placement(0, "a"), _placement(1, "b"), _placement(2, "c")]
    _, imap = pa.reorder_pages(pa.create_blank_pdf(pages=3), [2, 0, 1])
    out = {p["value"]: p["page"] for p in pa.remap_placements(placements, imap)}
    assert out == {"a": 1, "b": 2, "c": 0}


def test_remap_preserves_all_other_fields():
    p = {"kind": "signature", "page": 0, "x": 1.5, "y": 2.5,
         "width": 180.0, "height": 36.0, "color": "#112233"}
    out = pa.remap_placements([p], {0: 1})[0]
    assert out == {**p, "page": 1}


def test_remap_keeps_placements_on_unknown_pages():
    """An unknown page is likelier a client/server skew than a reason to throw
    away the user's work."""
    out = pa.remap_placements([_placement(7, "orphan")], {0: 0})
    assert out == [_placement(7, "orphan")]


def test_remap_handles_empty_and_malformed():
    assert pa.remap_placements(None, {0: 0}) == []
    assert pa.remap_placements([], {0: 0}) == []
    assert pa.remap_placements(["not a dict"], {0: 0}) == []
    # A non-numeric page is passed through rather than crashing the request.
    assert pa.remap_placements([{"page": "x"}], {0: 0}) == [{"page": "x"}]


# ---- Routes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_blank_requires_auth(client):
    r = await client.post("/api/v1/documents/blank", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_blank_document(client, authed_user):
    u, token = authed_user
    r = await client.post(
        "/api/v1/documents/blank",
        headers=_auth(token),
        json={"title": "Service Agreement", "page_size": "letter", "pages": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original_file_name"] == "Service Agreement.pdf"

    doc = await Document.get(id=body["id"])
    assert doc.status == DocumentStatus.DRAFT
    assert doc.metadata["origin"] == "created"
    assert doc.metadata["page_size"] == "letter"
    assert doc.field_placements == []


@pytest.mark.asyncio
async def test_create_blank_rejects_bad_page_size(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/blank", headers=_auth(token),
        json={"page_size": "papyrus"},
    )
    assert r.status_code == 400
    assert "a4" in r.json()["detail"]


@pytest.mark.asyncio
async def test_page_delete_route_migrates_placements(client, authed_user):
    """End-to-end: the route must persist both the new PDF and the migrated
    placements, and report what it dropped."""
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/blank", headers=_auth(token), json={"pages": 3},
    )
    doc_id = r.json()["id"]

    doc = await Document.get(id=doc_id)
    doc.field_placements = [_placement(0, "a"), _placement(1, "b"), _placement(2, "c")]
    await doc.save()

    r = await client.post(
        f"/api/v1/documents/{doc_id}/pages",
        headers=_auth(token), json={"op": "delete", "index": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page_count"] == 2
    assert body["placements_kept"] == 2
    assert body["placements_dropped"] == 1

    await doc.refresh_from_db()
    assert [(p["value"], p["page"]) for p in doc.field_placements] == [("a", 0), ("c", 1)]


@pytest.mark.asyncio
async def test_page_reorder_route(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/documents/blank", headers=_auth(token), json={"pages": 3},
    )
    doc_id = r.json()["id"]
    doc = await Document.get(id=doc_id)
    doc.field_placements = [_placement(0, "a"), _placement(2, "c")]
    await doc.save()

    r = await client.post(
        f"/api/v1/documents/{doc_id}/pages",
        headers=_auth(token), json={"op": "reorder", "order": [2, 0, 1]},
    )
    assert r.status_code == 200, r.text
    await doc.refresh_from_db()
    assert {p["value"]: p["page"] for p in doc.field_placements} == {"a": 1, "c": 0}


@pytest.mark.asyncio
async def test_page_op_validates_missing_index(client, authed_user):
    _, token = authed_user
    r = await client.post("/api/v1/documents/blank", headers=_auth(token), json={})
    doc_id = r.json()["id"]
    r = await client.post(
        f"/api/v1/documents/{doc_id}/pages", headers=_auth(token), json={"op": "delete"},
    )
    assert r.status_code == 400
    assert "index" in r.json()["detail"]


@pytest.mark.asyncio
async def test_page_op_404_for_other_users_document(client, authed_user):
    _, token = authed_user
    other = await User.create(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        first_name="O", last_name="P", role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    doc = await Document.create(
        user_id=other.id, file_name="x", original_file_name="x.pdf",
        file_url="local://authored/x.pdf", file_mime_type="application/pdf",
        file_size=1, status=DocumentStatus.DRAFT,
    )
    r = await client.post(
        f"/api/v1/documents/{doc.id}/pages",
        headers=_auth(token), json={"op": "duplicate", "index": 0},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_page_op_refuses_rendered_document(client, authed_user):
    """A completed render is a finished artifact — re-paginating underneath it
    would invalidate the signature layout."""
    u, token = authed_user
    doc = await Document.create(
        user_id=u.id, file_name="x", original_file_name="x.pdf",
        file_url="local://authored/x.pdf", file_mime_type="application/pdf",
        file_size=1, status=DocumentStatus.COMPLETED,
        completed_file_url="local://signed/x.pdf",
    )
    r = await client.post(
        f"/api/v1/documents/{doc.id}/pages",
        headers=_auth(token), json={"op": "delete", "index": 0},
    )
    assert r.status_code == 409

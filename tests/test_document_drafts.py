"""Regression test for the draft-status bug.

Until this fix landed, two different handlers shared the route
`PUT /documents/{id}/placements`. FastAPI picked the second one, which
persisted placements but never flipped `status` to DRAFT — so the
Documents page's Drafts filter showed nothing, even after the user had
clearly been editing. This test pins down the right behavior.
"""
from __future__ import annotations

import uuid

import pytest

from app.db.models.document import Document, DocumentStatus
from app.db.models.user import User, UserRole, UserStatus
from app.core import security


@pytest.fixture
async def authed_user():
    """Create a user with an auth token attached.

    We don't go through the register flow because it pulls in subscription
    / Stripe scaffolding that's noise for this scenario. JWT payload uses
    `sub` as the user id, matching `get_current_user_id`.
    """
    u = await User.create(
        email=f"draft-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Draft",
        last_name="Tester",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    return u, token


@pytest.mark.asyncio
async def test_placements_save_flips_status_to_draft(client, authed_user):
    """Core regression: auto-save must set status to DRAFT regardless of
    what state the document was in before."""
    u, token = authed_user

    # Start the document in EXTRACTED — typical state right after an
    # upload + auto-fill that the user hasn't reviewed yet.
    doc = await Document.create(
        user_id=u.id,
        file_name="hello.pdf",
        original_file_name="hello.pdf",
        file_url="local://upload/x.pdf",
        file_mime_type="application/pdf",
        file_size=1024,
        status=DocumentStatus.EXTRACTED,
    )

    r = await client.put(
        f"/api/v1/documents/{doc.id}/placements",
        json={"placements": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["placement_count"] == 0

    await doc.refresh_from_db()
    assert doc.status == DocumentStatus.DRAFT
    assert (doc.metadata or {}).get("draft_saved_at") is not None


@pytest.mark.asyncio
async def test_completed_doc_reverts_to_draft_on_edit(client, authed_user):
    """A completed (already-stamped) document that's reopened and edited
    must revert to DRAFT so the user can find it in the Drafts filter and
    knows their changes aren't yet in the rendered PDF."""
    u, token = authed_user
    doc = await Document.create(
        user_id=u.id,
        file_name="signed.pdf",
        original_file_name="signed.pdf",
        file_url="local://upload/y.pdf",
        file_mime_type="application/pdf",
        file_size=2048,
        status=DocumentStatus.COMPLETED,
    )

    r = await client.put(
        f"/api/v1/documents/{doc.id}/placements",
        json={"placements": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200

    await doc.refresh_from_db()
    assert doc.status == DocumentStatus.DRAFT


@pytest.mark.asyncio
async def test_archived_doc_rejects_edits(client, authed_user):
    """Archived documents are intentionally read-only — saving placements
    on one must return 409, not silently un-archive it."""
    u, token = authed_user
    doc = await Document.create(
        user_id=u.id,
        file_name="old.pdf",
        original_file_name="old.pdf",
        file_url="local://upload/z.pdf",
        file_mime_type="application/pdf",
        file_size=512,
        status=DocumentStatus.ARCHIVED,
    )

    r = await client.put(
        f"/api/v1/documents/{doc.id}/placements",
        json={"placements": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    await doc.refresh_from_db()
    assert doc.status == DocumentStatus.ARCHIVED


@pytest.mark.asyncio
async def test_other_user_cannot_save_to_my_document(client, authed_user):
    """Cross-user isolation: someone else's token can't flip my doc to DRAFT."""
    u, _ = authed_user
    doc = await Document.create(
        user_id=u.id,
        file_name="mine.pdf",
        original_file_name="mine.pdf",
        file_url="local://upload/m.pdf",
        file_mime_type="application/pdf",
        file_size=1024,
        status=DocumentStatus.EXTRACTED,
    )

    other = await User.create(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Other",
        last_name="User",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    other_token = security.create_access_token({"sub": str(other.id)})

    r = await client.put(
        f"/api/v1/documents/{doc.id}/placements",
        json={"placements": []},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r.status_code == 404  # we don't leak existence to non-owners
    await doc.refresh_from_db()
    assert doc.status == DocumentStatus.EXTRACTED  # untouched

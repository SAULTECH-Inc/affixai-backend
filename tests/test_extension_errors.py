"""Tests for Chrome extension backend endpoints and route matching.

Includes:
1. GET /api/v1/data-vault/flat returns decrypted vault key-value entries.
2. GET /api/v1/documents/pending-mine resolves without matching collision against /documents/{document_id}.
"""
from __future__ import annotations

import uuid
import pytest

from app.db.models.user import User, UserRole, UserStatus
from app.db.models.data_vault import DataVault, DataSource
from app.db.models.document import Document, DocumentStatus
from app.db.models.document_participant import DocumentParticipant, ParticipantRole, ParticipantStatus
from app.core import security
from app.core.encryption import encrypt


@pytest.fixture
async def authed_user():
    """Create a user with an auth token attached."""
    u = await User.create(
        email=f"tester-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Extension",
        last_name="Tester",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    token = security.create_access_token({"sub": str(u.id)})
    return u, token


@pytest.mark.asyncio
async def test_get_flat_vault(client, authed_user):
    """Test that GET /api/v1/data-vault/flat returns all active decrypted vault fields."""
    u, token = authed_user

    # Create standard vault records
    await DataVault.create(
        user_id=u.id,
        segment="personal",
        field_name="first_name",
        encrypted_value=encrypt("John"),
        source=DataSource.USER_INPUT,
    )
    await DataVault.create(
        user_id=u.id,
        segment="personal",
        field_name="last_name",
        encrypted_value=encrypt("Doe"),
        source=DataSource.USER_INPUT,
    )

    # Create a custom vault record
    await DataVault.create(
        user_id=u.id,
        segment="custom:some_uuid",
        field_name="custom_field_name",
        encrypted_value=encrypt("Custom Value"),
        source=DataSource.USER_INPUT,
    )

    # Inactive vault record (should be ignored)
    await DataVault.create(
        user_id=u.id,
        segment="personal",
        field_name="ignored_field",
        encrypted_value=encrypt("Ignored"),
        source=DataSource.USER_INPUT,
        is_active=False,
    )

    r = await client.get(
        "/api/v1/data-vault/flat",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data == {
        "first_name": "John",
        "last_name": "Doe",
        "custom_field_name": "Custom Value",
    }


@pytest.mark.asyncio
async def test_pending_mine_no_uuid_collision(client, authed_user):
    """Test that GET /api/v1/documents/pending-mine resolves without UUID path collision.
    
    If it collided with GET /api/v1/documents/{document_id}, it would return 422.
    """
    u, token = authed_user

    # Create a document and a participant invite
    doc_owner = await User.create(
        email="owner@example.com",
        password_hash="dummy",
        first_name="Doc",
        last_name="Owner",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    doc = await Document.create(
        user_id=doc_owner.id,
        file_name="doc.pdf",
        original_file_name="doc.pdf",
        file_url="local://upload/doc.pdf",
        file_mime_type="application/pdf",
        file_size=1024,
        status=DocumentStatus.DRAFT,
    )
    
    # Participant mapping the user's email to this document
    await DocumentParticipant.create(
        document_id=doc.id,
        email=u.email,
        name="Extension Tester",
        role=ParticipantRole.SIGNER,
        status=ParticipantStatus.INVITED,
        invite_token="test_invite_token_12345",
        invited_by=doc_owner.id,
    )

    r = await client.get(
        "/api/v1/documents/pending-mine",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 1
    assert data[0]["document_title"] == "doc.pdf"
    assert data[0]["invite_token"] == "test_invite_token_12345"
    assert data[0]["sender_name"] == "Doc Owner"

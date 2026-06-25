"""Tests for the public lead-capture endpoint.

The leads endpoint is open to the internet, so all of the spam-mitigation
behaviors that AREN'T obvious from the docstring (honeypot trip silently
succeeds, rate limit returns 429, short messages fail validation) need to
be locked in by tests — a regression here means real spam in the inbox.
"""
from __future__ import annotations

import io

import pytest

from app.api.routes import leads as leads_module
from app.db.models.lead import Lead, LeadKind, LeadStatus


@pytest.mark.asyncio
async def test_contact_lead_success(client):
    """Happy path: a complete contact submission lands in the DB."""
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "contact",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "topic": "Sales",
            "message": "We're evaluating signing tools. Can we chat?",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert "id" in body

    lead = await Lead.get(id=body["id"])
    assert lead.kind == LeadKind.CONTACT
    assert lead.name == "Jane Doe"
    assert lead.email == "jane@example.com"
    assert lead.topic == "Sales"
    assert lead.status == LeadStatus.NEW


@pytest.mark.asyncio
async def test_email_is_lowercased(client):
    """Email comparison is case-insensitive — store the canonical form."""
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "contact",
            "name": "Mixed",
            "email": "Mixed.Case@Example.COM",
            "message": "Testing email normalization",
        },
    )
    assert r.status_code == 201
    lead = await Lead.get(id=r.json()["id"])
    assert lead.email == "mixed.case@example.com"


@pytest.mark.asyncio
async def test_short_message_rejected(client):
    """Pydantic enforces a 10-char minimum on `message`."""
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "contact",
            "name": "X",
            "email": "x@x.com",
            "message": "short",
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("at least 10 characters" in str(d) for d in detail)


@pytest.mark.asyncio
async def test_invalid_email_rejected(client):
    """Pydantic's EmailStr rejects malformed addresses."""
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "contact",
            "name": "X",
            "email": "not-an-email",
            "message": "This should fail validation",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_honeypot_trip_returns_201_but_no_row(client):
    """When the honeypot is filled, we return 201 to avoid tipping off the
    bot — but no row should be persisted. This is THE spam mitigation; if
    it stops working, the inbox fills up overnight."""
    before = await Lead.all().count()
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "contact",
            "name": "Bot",
            "email": "bot@spam.com",
            "message": "Buy cheap things from us today!",
            "website": "https://spam.example.com",
        },
    )
    assert r.status_code == 201
    assert "id" in r.json()
    after = await Lead.all().count()
    assert after == before, "honeypot trip should NOT persist a Lead row"


@pytest.mark.asyncio
async def test_rate_limit_after_max(client):
    """After 5 successful submissions from the same IP, the 6th returns 429."""
    payload = {
        "kind": "contact",
        "name": "Burst",
        "email": "burst@example.com",
        "message": "This message is long enough to pass validation.",
    }
    # The httpx ASGI transport uses 127.0.0.1 by default — all five count
    # against the same IP bucket.
    for i in range(leads_module._RATE_MAX_PER_WINDOW):
        r = await client.post("/api/v1/leads", json=payload)
        assert r.status_code == 201, f"submission {i + 1} should succeed"

    r = await client.post("/api/v1/leads", json=payload)
    assert r.status_code == 429
    assert "too quickly" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_extra_field_persisted(client):
    """The careers form rides LinkedIn / resume-url in `extra`. Round-trip it."""
    r = await client.post(
        "/api/v1/leads",
        json={
            "kind": "careers",
            "name": "Jane",
            "email": "jane@example.com",
            "topic": "Senior Backend Engineer",
            "message": "Long-time Python dev would love to chat.",
            "extra": {"linkedin": "https://linkedin.com/in/jane"},
        },
    )
    assert r.status_code == 201
    lead = await Lead.get(id=r.json()["id"])
    assert lead.kind == LeadKind.CAREERS
    assert lead.extra == {"linkedin": "https://linkedin.com/in/jane"}


# ---- Careers application multipart endpoint --------------------------------


@pytest.mark.asyncio
async def test_careers_application_without_file(client):
    """Multipart endpoint accepts submissions without a resume file."""
    r = await client.post(
        "/api/v1/leads/careers-application",
        data={
            "name": "Pat",
            "email": "pat@example.com",
            "role": "Product Designer",
            "message": "Excited to chat about this role.",
        },
    )
    assert r.status_code == 201
    lead = await Lead.get(id=r.json()["id"])
    assert lead.kind == LeadKind.CAREERS
    assert lead.topic == "Product Designer"


@pytest.mark.asyncio
async def test_careers_application_rejects_oversized_file(client, monkeypatch):
    """Files over the size cap return 413."""
    # Build a payload larger than _RESUME_MAX_BYTES
    big_blob = b"x" * (leads_module._RESUME_MAX_BYTES + 1024)
    r = await client.post(
        "/api/v1/leads/careers-application",
        data={
            "name": "Pat",
            "email": "pat@example.com",
            "role": "Product Designer",
            "message": "Resume attached.",
        },
        files={
            "resume_file": ("huge.pdf", io.BytesIO(big_blob), "application/pdf"),
        },
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_careers_application_rejects_bad_mime(client):
    """Files of an unsupported type return 415."""
    r = await client.post(
        "/api/v1/leads/careers-application",
        data={
            "name": "Pat",
            "email": "pat@example.com",
            "role": "Product Designer",
            "message": "Wrong format attached.",
        },
        files={
            "resume_file": (
                "headshot.png",
                io.BytesIO(b"fake png bytes"),
                "image/png",
            ),
        },
    )
    assert r.status_code == 415

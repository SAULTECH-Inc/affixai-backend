"""Public lead-capture endpoint for /contact and /careers forms.

The endpoint is OPEN to the internet — anyone can POST. We layer a few
cheap defenses so it doesn't become a spam vacuum:

  1. Pydantic length / format validators (rejects junk shapes outright).
  2. A "honeypot" field (`website`) that real users never fill in but bots
     happily auto-fill. Filled = silently 201 with no DB write.
  3. Per-IP rate limit: 5 posts / hour / IP, kept in an in-process LRU.
     Good enough to slow scripted abuse. Behind a real load-balancer or
     for higher-volume sites, swap for Redis-backed slowapi.
  4. Bounded message length (already enforced via Pydantic).

After persisting we fire-and-forget an SMTP notification to the relevant
inbox (hello@ / careers@) so the team gets a real-time ping. SMTP
failures are logged and swallowed — the lead is still stored.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from loguru import logger
from pydantic import BaseModel, EmailStr, Field

from app.core.config import settings
from app.db.models.lead import Lead, LeadKind
from app.common.services import email_service
from app.common.services.s3_service import s3_service


router = APIRouter()


# ---- Rate limiting --------------------------------------------------------
#
# Per-IP, in-memory, time-windowed. We store a deque of submission timestamps
# per IP; on each request we evict anything older than the window and check
# the remaining count. This is intentionally simple and good enough for a
# single-instance deployment. For a multi-instance setup, replace with a
# Redis-backed counter.

_RATE_WINDOW_SECONDS = 3600  # 1 hour
_RATE_MAX_PER_WINDOW = 5
_rate_log: dict[str, Deque[float]] = {}


def _check_rate_limit(ip: str) -> bool:
    """Return True if this IP is within the allowed rate, False otherwise."""
    now = time.time()
    window_start = now - _RATE_WINDOW_SECONDS
    log = _rate_log.setdefault(ip, deque())
    while log and log[0] < window_start:
        log.popleft()
    if len(log) >= _RATE_MAX_PER_WINDOW:
        return False
    log.append(now)
    return True


def _client_ip(request: Request) -> str:
    # Honor a single layer of proxy headers — anything richer needs a real
    # reverse-proxy config. Falls back to the direct peer address.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---- Schemas --------------------------------------------------------------


class LeadIn(BaseModel):
    """Public lead submission. Used by both /contact and /careers forms."""
    kind: LeadKind
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    topic: str | None = Field(default=None, max_length=120)
    message: str = Field(min_length=10, max_length=5000)
    # Optional extras — careers form may include LinkedIn or a resume URL.
    extra: dict[str, str] | None = None
    # Honeypot — real users leave this empty. Bots fill EVERY field.
    # We accept the submission to avoid tipping off the bot, but skip
    # persisting it.
    website: str | None = Field(default=None, max_length=200)


class LeadOut(BaseModel):
    id: UUID
    message: str = "Thanks — we'll get back to you soon."


# ---- Public route ---------------------------------------------------------


@router.post("", response_model=LeadOut, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadIn,
    request: Request,
    background_tasks: BackgroundTasks,
) -> LeadOut:
    ip = _client_ip(request)

    # Honeypot trip — pretend it worked, but don't store anything. This
    # avoids signaling to the bot author that their form fingerprint failed,
    # which would just prompt them to refine it.
    if payload.website:
        logger.info(f"lead honeypot tripped from {ip}")
        from uuid import uuid4

        return LeadOut(id=uuid4())

    if not _check_rate_limit(ip):
        logger.warning(f"lead rate-limit hit for ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="You're sending messages too quickly. Try again later.",
        )

    ua = request.headers.get("user-agent", "")[:512] or None

    lead = await Lead.create(
        kind=payload.kind,
        name=payload.name.strip(),
        email=str(payload.email).lower(),
        topic=(payload.topic or "").strip() or None,
        message=payload.message.strip(),
        extra=payload.extra,
        ip_address=ip,
        user_agent=ua,
    )
    logger.info(
        f"new {payload.kind.value} lead from {payload.email} (id={lead.id})"
    )

    # Notify the team. Fire-and-forget so the visitor sees a fast 201 even
    # if SMTP is slow / offline.
    background_tasks.add_task(_notify_team, lead)

    return LeadOut(id=lead.id)


# ---- Email forward --------------------------------------------------------

# Route each lead kind to a dedicated team inbox. Override per-env via
# settings (LEADS_CONTACT_TO / LEADS_CAREERS_TO) if you want to send to a
# real address. Falls back to EMAIL_FROM so dev SMTP traffic stays visible.
_INBOX_DEFAULT = {
    LeadKind.CONTACT: "hello@affixai.com",
    LeadKind.CAREERS: "careers@affixai.com",
}


async def _notify_team(lead: Lead) -> None:
    """Forward the lead body to the appropriate team inbox."""
    to = _inbox_for(lead.kind)
    subject_prefix = "Contact" if lead.kind == LeadKind.CONTACT else "Careers"
    topic_part = f" — {lead.topic}" if lead.topic else ""
    subject = f"[{subject_prefix}{topic_part}] {lead.name}"

    extra_lines = ""
    if lead.extra:
        extra_lines = "<hr/><h4>Extras</h4><ul>" + "".join(
            f"<li><strong>{k}</strong>: {_escape(str(v))}</li>"
            for k, v in lead.extra.items()
        ) + "</ul>"

    html = (
        f"<p><strong>From:</strong> {_escape(lead.name)} "
        f"&lt;{_escape(lead.email)}&gt;</p>"
        f"<p><strong>Topic:</strong> {_escape(lead.topic or '—')}</p>"
        f"<p><strong>IP:</strong> {_escape(lead.ip_address or '—')} · "
        f"<strong>UA:</strong> {_escape((lead.user_agent or '—')[:120])}</p>"
        f"<hr/><p>{_escape(lead.message).replace(chr(10), '<br/>')}</p>"
        f"{extra_lines}"
        f"<hr/><p style='color:#666;font-size:12px;'>"
        f"Lead ID: {lead.id} · Reply to {_escape(lead.email)} directly.</p>"
    )
    try:
        await email_service._send(to=to, subject=subject, html=html)
    except Exception as exc:
        logger.warning(f"lead notify email failed (lead={lead.id}): {exc}")


def _inbox_for(kind: LeadKind) -> str:
    # Allow override via env. settings names map 1:1 with the kind enum.
    attr = f"LEADS_{kind.value.upper()}_TO"
    return getattr(settings, attr, None) or _INBOX_DEFAULT[kind]


def _escape(s: str) -> str:
    """Tiny HTML escape — we don't want a hostile message to break out of
    the email shell into the recipient's inbox UI."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---- Careers application (multipart, with optional resume file) -----------
#
# The careers form on the frontend uses this endpoint specifically because
# applicants may attach a resume PDF. JSON-only /leads stays the way it is
# for the contact form. We share the IP rate-limiter so a bot can't flood
# both endpoints in parallel.

_RESUME_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — generous for PDFs / DOCXs
_RESUME_OK_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}


@router.post(
    "/careers-application",
    response_model=LeadOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_careers_application(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(..., min_length=1, max_length=200),
    email: str = Form(..., max_length=254),
    role: str = Form(..., max_length=120),
    message: str = Form(..., min_length=10, max_length=5000),
    linkedin: str | None = Form(default=None, max_length=300),
    resume_url: str | None = Form(default=None, max_length=500),
    # Honeypot — same trick as the JSON endpoint.
    website: str | None = Form(default=None, max_length=200),
    resume_file: UploadFile | None = File(default=None),
) -> LeadOut:
    """Careers application with optional resume upload.

    On the wire this is multipart/form-data so the file can ride along.
    If `resume_file` is omitted, behavior is equivalent to POST /leads with
    `kind=careers`.
    """
    ip = _client_ip(request)

    if website:
        logger.info(f"careers application honeypot tripped from {ip}")
        from uuid import uuid4

        return LeadOut(id=uuid4())

    if not _check_rate_limit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="You're submitting too many applications. Try again later.",
        )

    # Basic email validation — we don't want to pull in another dependency
    # just for the marketing form, so a minimal sanity check is enough.
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=422, detail="Invalid email address")

    extra: dict[str, str] = {}
    if linkedin and linkedin.strip():
        extra["linkedin"] = linkedin.strip()
    if resume_url and resume_url.strip():
        extra["resume_url"] = resume_url.strip()

    # Resume upload — happens BEFORE the DB write so we can fail fast on a
    # bad file without orphaning a lead row.
    if resume_file is not None and resume_file.filename:
        contents = await resume_file.read()
        if len(contents) == 0:
            raise HTTPException(status_code=422, detail="Resume file is empty")
        if len(contents) > _RESUME_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Resume file too large (max {_RESUME_MAX_BYTES // (1024*1024)} MB)",
            )
        mime = resume_file.content_type or "application/octet-stream"
        if mime not in _RESUME_OK_MIMES:
            raise HTTPException(
                status_code=415,
                detail="Unsupported file type — please upload PDF, DOC, DOCX, or TXT",
            )
        try:
            s3 = s3_service()
            uploaded = s3.upload_file(
                contents,
                resume_file.filename,
                mime,
                folder="leads/resumes",
            )
            extra["resume_s3_key"] = uploaded["key"]
            extra["resume_filename"] = resume_file.filename
            # 7-day presigned download URL for the admin — long enough that
            # the link lasts through normal review cadence.
            extra["resume_download_url"] = s3.get_presigned_url(
                uploaded["key"], expires_in=7 * 24 * 3600
            )
        except Exception as exc:
            logger.error(f"resume upload to S3 failed: {exc}")
            raise HTTPException(
                status_code=503,
                detail="Could not store your resume — please try again or paste a link",
            )

    ua = request.headers.get("user-agent", "")[:512] or None
    lead = await Lead.create(
        kind=LeadKind.CAREERS,
        name=name.strip(),
        email=email.lower().strip(),
        topic=role.strip(),
        message=message.strip(),
        extra=extra or None,
        ip_address=ip,
        user_agent=ua,
    )
    logger.info(
        f"new careers application from {email} for role={role} "
        f"(id={lead.id}, resume={'yes' if 'resume_s3_key' in extra else 'no'})"
    )

    background_tasks.add_task(_notify_team, lead)

    return LeadOut(id=lead.id)

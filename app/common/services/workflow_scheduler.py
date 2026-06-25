"""Scheduled background tasks for the workflow layer.

Two jobs:
  * `reminder_sweep` (hourly): finds every IN_PROGRESS / SENT workflow with
    pending participants whose last_reminder_at is older than
    REMINDER_INTERVAL_HOURS. Re-emails them, records the time.
  * `expiration_sweep` (hourly): finds workflows past their expires_at and
    flips them to EXPIRED (the same lazy check `expire_if_due` would do, but
    proactive so the audit timeline is accurate even if no one accesses).

Uses APScheduler's AsyncIOScheduler so the same asyncio loop as FastAPI runs
the jobs — no extra worker process needed. For a multi-process deployment we'd
add a database advisory lock to prevent duplicate sends; single-process dev
and most early SaaS deployments don't need it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.core.config import settings
from app.db.models.document import Document, RoutingStatus
from app.db.models.document_participant import (
    DocumentParticipant,
    ParticipantStatus,
)
from app.db.models.user import User


# Hours between automatic reminder emails per pending participant.
REMINDER_INTERVAL_HOURS = 24

# Skip if we've sent a reminder more recently than this. Multiple reminders
# per day would be spam.
MIN_REMINDER_GAP_HOURS = 20


_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Lazily-construct the singleton scheduler. main.py starts it on app
    lifespan startup and stops it on shutdown."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def register_jobs() -> None:
    """Idempotent: registers our jobs on the scheduler. Safe to call once
    at startup; APScheduler dedupes by job id."""
    sch = get_scheduler()
    sch.add_job(
        reminder_sweep,
        trigger=IntervalTrigger(hours=1),
        id="reminder_sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sch.add_job(
        expiration_sweep,
        trigger=IntervalTrigger(hours=1),
        id="expiration_sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("workflow scheduler jobs registered (hourly reminders + expirations)")


def start() -> None:
    sch = get_scheduler()
    if not sch.running:
        sch.start()
        logger.info("workflow scheduler started")


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("workflow scheduler stopped")


# ---- Jobs -------------------------------------------------------------------


async def reminder_sweep() -> None:
    """Email pending participants whose last reminder was a while ago.

    Conservative: never reminds more than once per ~24h per participant,
    and never on a workflow that's terminal (completed / declined / etc.).
    """
    from app.common.services.email_service import (
        send_collaboration_invite_email,
    )
    from app.common.services.workflow import participants_to_notify

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=MIN_REMINDER_GAP_HOURS)

    active_docs = await Document.filter(
        deleted_at=None,
        routing_status__in=[RoutingStatus.SENT, RoutingStatus.IN_PROGRESS],
    )
    if not active_docs:
        return

    sent_count = 0
    for doc in active_docs:
        try:
            to_notify = await participants_to_notify(doc)
        except Exception as exc:
            logger.warning(
                f"reminder_sweep: participants_to_notify failed "
                f"for {doc.id}: {exc}"
            )
            continue
        if not to_notify:
            continue

        owner = await User.get_or_none(id=doc.user_id, deleted_at=None) if doc.user_id else None
        sender_name = (
            (" ".join(filter(None, [owner.first_name, owner.last_name])).strip()
             or owner.email)
            if owner else "Someone"
        )

        base = settings.FRONTEND_URL.rstrip("/")
        for p in to_notify:
            # Throttle. metadata.last_reminder_at controls cadence.
            meta = p.metadata or {}
            last_str = meta.get("last_reminder_at")
            if last_str:
                try:
                    last = datetime.fromisoformat(last_str)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if last > threshold:
                        continue
                except ValueError:
                    pass

            try:
                await send_collaboration_invite_email(
                    email=p.email,
                    document_name=doc.original_file_name or "document",
                    sender_name=sender_name,
                    role=p.role.value,
                    invite_url=f"{base}/invite/{p.invite_token}",
                    personal_message=p.message,
                )
                sent_count += 1
                p.metadata = {**meta, "last_reminder_at": now.isoformat()}
                await p.save()
            except Exception as exc:
                logger.warning(
                    f"reminder_sweep: email failed to {p.email}: {exc}"
                )
    if sent_count:
        logger.info(f"reminder_sweep: sent {sent_count} reminder email(s)")


async def expiration_sweep() -> None:
    """Mark every overdue workflow as EXPIRED. Cheap query — uses the
    indexed `routing_status` and direct datetime comparison."""
    now = datetime.now(timezone.utc)
    overdue = await Document.filter(
        deleted_at=None,
        expires_at__lt=now,
        routing_status__in=[RoutingStatus.SENT, RoutingStatus.IN_PROGRESS],
    )
    if not overdue:
        return
    for doc in overdue:
        doc.routing_status = RoutingStatus.EXPIRED
        await doc.save()
    logger.info(f"expiration_sweep: marked {len(overdue)} workflow(s) expired")


__all__ = [
    "get_scheduler",
    "register_jobs",
    "start",
    "stop",
    "reminder_sweep",
    "expiration_sweep",
]

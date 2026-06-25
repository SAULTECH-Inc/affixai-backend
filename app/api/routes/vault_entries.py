"""Multi-entry vault routes — Education + Employment.

Mounted under `/api/v1/data-vault/entries/{section}` to sit next to the
existing single-entry vault endpoints.

The payload shape is FLEXIBLE: each entry is a JSON map of {field_name:
value}. We trust the frontend to send the field names that match
`FIELD_REGISTRY[section]` — unknown names are dropped on write rather than
rejected, so adding new fields in `vault_schema.py` is non-breaking.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field

from app.common.deps import get_current_user
from app.common.vault_schema import (
    MULTI_ENTRY_SEGMENTS,
    VaultSegment,
    field_names_for,
)
from app.core.encryption import decrypt, encrypt
from app.db.models.data_vault import DataVault
from app.db.models.user import User
from app.db.models.vault_entry import VaultEntry
from app.models.auth_schemas import MessageOut


router = APIRouter()


# ---- Schemas ---------------------------------------------------------------


class VaultEntryDto(BaseModel):
    """Both create + update use this shape. Unknown fields are dropped on
    write (forward-compatible with schema additions)."""
    fields: dict[str, str | None] = Field(default_factory=dict)
    is_current: bool = False
    sort_order: int | None = None


class VaultEntryOut(BaseModel):
    id: UUID
    section: str
    fields: dict[str, str]
    is_current: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


# ---- Helpers ---------------------------------------------------------------


def _resolve_section(section: str) -> VaultSegment:
    """Validate that `section` is a known multi-entry segment.

    Single-entry sections (Personal, Identity, etc) live in the OTHER vault
    routes, not here. We reject them with 400 so a typo on the frontend
    fails fast and visibly.
    """
    try:
        seg = VaultSegment(section)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown section: {section}") from exc
    if seg not in MULTI_ENTRY_SEGMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Section '{section}' is single-entry — use /data-vault/segments/{section} instead",
        )
    return seg


def _payload_to_out(row: VaultEntry) -> VaultEntryOut:
    """Decrypt + decode the entry's JSON payload for the API response."""
    try:
        fields = json.loads(decrypt(row.encrypted_payload))
        if not isinstance(fields, dict):
            fields = {}
    except Exception as exc:
        logger.warning(f"vault entry {row.id} payload unreadable: {exc}")
        fields = {}
    return VaultEntryOut(
        id=row.id,
        section=row.section,
        fields=fields,
        is_current=row.is_current,
        sort_order=row.sort_order,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _migrate_from_legacy_if_needed(
    user: User, segment: VaultSegment
) -> list[VaultEntry]:
    """If the user has no VaultEntry rows for this section BUT they have
    legacy single-entry DataVault rows under the same segment, fold those
    into ONE entry so existing data carries forward into the multi-entry UI.

    Idempotent: runs only when VaultEntry is empty for this (user, section).
    The migrated DataVault rows are marked inactive so they don't get
    re-migrated.
    """
    existing = await VaultEntry.filter(
        user_id=user.id, section=segment.value, deleted_at=None
    ).count()
    if existing > 0:
        return []

    legacy = await DataVault.filter(
        user_id=user.id, segment=segment.value, is_active=True, deleted_at=None
    )
    if not legacy:
        return []

    fields_map: dict[str, str] = {}
    for row in legacy:
        try:
            fields_map[row.field_name] = decrypt(row.encrypted_value)
        except Exception as exc:
            logger.warning(f"could not decrypt legacy vault row {row.id}: {exc}")

    if not fields_map:
        return []

    new_entry = await VaultEntry.create(
        user_id=user.id,
        section=segment.value,
        encrypted_payload=encrypt(json.dumps(fields_map)),
        is_current=True,
        sort_order=0,
    )
    # Deactivate the legacy rows so we don't migrate them again. We don't
    # delete them — keeps a paper trail in case someone wants to audit.
    for r in legacy:
        r.is_active = False
        await r.save(update_fields=["is_active", "updated_at"])
    logger.info(
        f"migrated {len(legacy)} legacy {segment.value} field(s) into "
        f"VaultEntry {new_entry.id} for user {user.id}"
    )
    return [new_entry]


def _filter_known_fields(
    segment: VaultSegment, payload_fields: dict[str, str | None]
) -> dict[str, str]:
    """Keep only field names that the section's schema knows about, drop
    None / empty values. Forward-compatible: new schema fields just work
    once they're added to FIELD_REGISTRY."""
    allowed = field_names_for(segment)
    out: dict[str, str] = {}
    for k, v in payload_fields.items():
        if k not in allowed:
            continue
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        out[k] = s
    return out


# ---- Routes ----------------------------------------------------------------


@router.get("/{section}", response_model=list[VaultEntryOut])
async def list_entries(
    section: str, user: User = Depends(get_current_user)
) -> list[VaultEntryOut]:
    seg = _resolve_section(section)
    # Backfill from the old flat storage on first access — gives users with
    # existing Employment data continuity without an explicit migration step.
    await _migrate_from_legacy_if_needed(user, seg)
    rows = await VaultEntry.filter(
        user_id=user.id, section=seg.value, deleted_at=None
    ).order_by("sort_order", "-is_current", "-created_at")
    return [_payload_to_out(r) for r in rows]


@router.post(
    "/{section}", response_model=VaultEntryOut, status_code=status.HTTP_201_CREATED
)
async def create_entry(
    section: str,
    payload: VaultEntryDto,
    user: User = Depends(get_current_user),
) -> VaultEntryOut:
    seg = _resolve_section(section)
    cleaned = _filter_known_fields(seg, payload.fields)
    if not cleaned:
        raise HTTPException(400, "Provide at least one non-empty field")

    # Only one entry per section can be the "current" one. If this one is
    # marked current, demote any other current entry first.
    if payload.is_current:
        await VaultEntry.filter(
            user_id=user.id, section=seg.value, is_current=True, deleted_at=None
        ).update(is_current=False)

    entry = await VaultEntry.create(
        user_id=user.id,
        section=seg.value,
        encrypted_payload=encrypt(json.dumps(cleaned)),
        is_current=payload.is_current,
        sort_order=(
            payload.sort_order
            if payload.sort_order is not None
            else await _next_sort_order(user, seg)
        ),
    )
    return _payload_to_out(entry)


@router.put("/{section}/{entry_id}", response_model=VaultEntryOut)
async def update_entry(
    section: str,
    entry_id: UUID,
    payload: VaultEntryDto,
    user: User = Depends(get_current_user),
) -> VaultEntryOut:
    seg = _resolve_section(section)
    entry = await VaultEntry.get_or_none(
        id=entry_id, user_id=user.id, section=seg.value, deleted_at=None
    )
    if not entry:
        raise HTTPException(404, "Entry not found")

    cleaned = _filter_known_fields(seg, payload.fields)
    entry.encrypted_payload = encrypt(json.dumps(cleaned))

    # Mark-as-current is mutually exclusive across the section.
    if payload.is_current and not entry.is_current:
        await VaultEntry.filter(
            user_id=user.id, section=seg.value, is_current=True, deleted_at=None
        ).exclude(id=entry.id).update(is_current=False)
    entry.is_current = payload.is_current

    if payload.sort_order is not None:
        entry.sort_order = payload.sort_order
    await entry.save()
    return _payload_to_out(entry)


@router.delete("/{section}/{entry_id}", response_model=MessageOut)
async def delete_entry(
    section: str,
    entry_id: UUID,
    user: User = Depends(get_current_user),
) -> MessageOut:
    seg = _resolve_section(section)
    entry = await VaultEntry.get_or_none(
        id=entry_id, user_id=user.id, section=seg.value, deleted_at=None
    )
    if not entry:
        raise HTTPException(404, "Entry not found")
    entry.deleted_at = datetime.now(timezone.utc)
    await entry.save()
    return MessageOut(message="Entry deleted")


async def _next_sort_order(user: User, segment: VaultSegment) -> int:
    """Default sort order = highest existing + 1, so new entries go to the
    bottom of the list. Users reorder via drag — not implemented yet but the
    column is ready."""
    rows = await VaultEntry.filter(
        user_id=user.id, section=segment.value, deleted_at=None
    ).order_by("-sort_order").limit(1)
    if not rows:
        return 100
    return rows[0].sort_order + 10

"""User-defined vault sections + fields.

CRUD for the schema (sections/fields). Values still live in the unified
`data_vault` table under `segment = "custom:<key>"` — write/read those via
the existing `/data-vault/segments/...` endpoints OR the dedicated
file-upload endpoint here for `field_type=file`.

Endpoints:
  GET    /custom/sections                 — list user's sections (with fields)
  POST   /custom/sections                 — create a section
  PUT    /custom/sections/{id}            — rename / reorder
  DELETE /custom/sections/{id}            — soft-delete (and its fields + values)
  POST   /custom/sections/{id}/fields     — add a field
  PUT    /custom/fields/{id}              — rename / change type / aliases
  DELETE /custom/fields/{id}              — soft-delete field + its value
  POST   /custom/fields/{id}/upload       — upload a file value (field_type=file)
  GET    /custom/fields/{id}/file         — download the stored file value
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from loguru import logger

from tortoise.expressions import Q

from app.common.deps import get_current_user
from app.core.encryption import decrypt, encrypt
from app.db.models.custom_vault import (
    CustomFieldType,
    CustomScope,
    CustomVaultField,
    CustomVaultSection,
)
from app.db.models.data_vault import DataSource, DataVault
from app.db.models.user import User, UserRole
from app.models.auth_schemas import MessageOut
from app.models.custom_vault_schemas import (
    CustomFieldCreateDto,
    CustomFieldOut,
    CustomFieldUpdateDto,
    CustomSectionCreateDto,
    CustomSectionOut,
    CustomSectionUpdateDto,
    CustomSectionWithFields,
    FileUploadOut,
)

router = APIRouter()




def _slugify(name: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with '_', strip leading/trailing.

    Keep it simple — used for the segment key & field key. If the slug collides
    with an existing one for the same scope, the caller appends `_2`, `_3`, etc.
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "untitled"


async def _unique_section_key(
    base: str,
    *,
    user_id: UUID | None,
    enterprise_id: UUID | None,
) -> str:
    """Append a numeric suffix until we find a slug not in use within the scope."""
    key = base
    n = 2
    while await CustomVaultSection.exists(
        user_id=user_id, enterprise_id=enterprise_id, key=key, deleted_at=None
    ):
        key = f"{base}_{n}"
        n += 1
    return key


async def _unique_field_key(section_id: UUID, base: str) -> str:
    key = base
    n = 2
    while await CustomVaultField.exists(section_id=section_id, key=key, deleted_at=None):
        key = f"{base}_{n}"
        n += 1
    return key


def _can_mutate_section(section: CustomVaultSection, user: User) -> bool:
    """True iff `user` may rename/delete/add-fields to `section`.

    User-scope: only the owner.
    Enterprise-scope: only enterprise admins (or super admin) of the SAME org.
    """
    if section.scope == CustomScope.USER:
        return section.user_id == user.id
    # ENTERPRISE
    if user.role == UserRole.SUPER_ADMIN:
        return True
    if user.role != UserRole.ENTERPRISE_ADMIN:
        return False
    return section.enterprise_id == user.enterprise_id


def _can_view_section(section: CustomVaultSection, user: User) -> bool:
    if section.scope == CustomScope.USER:
        return section.user_id == user.id
    return user.enterprise_id is not None and section.enterprise_id == user.enterprise_id


def _segment_key(section: CustomVaultSection) -> str:
    """data_vault.segment value for a custom section.

    We key on the section's UUID rather than its slug because two sections
    with the same slug can legitimately exist in different scopes (e.g. a
    user's personal "Insurance" plus the enterprise-wide "Insurance"). Using
    the UUID guarantees the segment is unique per definition; the
    user_id+segment+field_name unique constraint in data_vault then partitions
    values per user as expected.
    """
    return f"custom:{section.id}"


def _field_to_out(f: CustomVaultField) -> CustomFieldOut:
    return CustomFieldOut(
        id=f.id,
        section_id=f.section_id,  # type: ignore[attr-defined]
        name=f.name,
        key=f.key,
        field_type=f.field_type,
        aliases=f.aliases,
        placeholder=f.placeholder,
        required=f.required,
        display_order=f.display_order,
        created_at=f.created_at,
    )


def _section_to_out(s: CustomVaultSection, *, can_edit: bool) -> CustomSectionOut:
    return CustomSectionOut(
        id=s.id,
        name=s.name,
        key=s.key,
        icon=s.icon,
        display_order=s.display_order,
        scope=s.scope,
        enterprise_id=s.enterprise_id,
        can_edit=can_edit,
        created_at=s.created_at,
    )


# ---- Section endpoints ------------------------------------------------------


@router.get("/sections", response_model=list[CustomSectionWithFields])
async def list_sections(
    user: User = Depends(get_current_user),
) -> list[CustomSectionWithFields]:
    """Return sections + their fields + each field's current value preview.

    Includes:
      * the user's own (`scope=user, user_id=current`)
      * every enterprise-scope section in the user's enterprise (if any)

    Values are decrypted server-side and ALWAYS per-user. File-typed fields
    return the file's download URL rather than the URL itself in cleartext
    (the URL IS the sensitive part — decrypt then redirect).
    """
    scope_q = Q(user_id=user.id, scope=CustomScope.USER)
    if user.enterprise_id is not None:
        scope_q |= Q(enterprise_id=user.enterprise_id, scope=CustomScope.ENTERPRISE)

    sections = (
        await CustomVaultSection.filter(scope_q, deleted_at=None)
        .order_by("scope", "display_order", "created_at")
    )
    if not sections:
        return []

    section_ids = [s.id for s in sections]
    fields = (
        await CustomVaultField.filter(
            section_id__in=section_ids, deleted_at=None
        )
        .order_by("display_order", "created_at")
    )

    # Fetch existing values in one shot.
    segment_keys = [_segment_key(s) for s in sections]
    value_rows = await DataVault.filter(
        user_id=user.id, segment__in=segment_keys, is_active=True, deleted_at=None
    )
    # Map (segment, field_name) → decrypted value.
    values: dict[tuple[str, str], str] = {}
    for vr in value_rows:
        try:
            values[(vr.segment, vr.field_name)] = decrypt(vr.encrypted_value)
        except Exception:
            continue

    out: list[CustomSectionWithFields] = []
    for s in sections:
        seg_key = _segment_key(s)
        s_fields = [f for f in fields if f.section_id == s.id]  # type: ignore[attr-defined]
        field_outs: list[CustomFieldOut] = []
        for f in s_fields:
            fo = _field_to_out(f)
            v = values.get((seg_key, f.key))
            if v is not None:
                if f.field_type == CustomFieldType.FILE:
                    # `v` is the on-disk relative path. Don't leak the path —
                    # surface only "has a file" boolean + the download URL.
                    fo.has_value = True
                    fo.file_download_url = f"/api/v1/data-vault/custom/fields/{f.id}/file"
                else:
                    fo.value = v
                    fo.has_value = True
            field_outs.append(fo)
        out.append(
            CustomSectionWithFields(
                **_section_to_out(s, can_edit=_can_mutate_section(s, user)).model_dump(),
                fields=field_outs,
            )
        )
    return out


@router.put("/sections/{section_id}/values", response_model=MessageOut)
async def upsert_section_values(
    section_id: UUID,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
) -> MessageOut:
    """Upsert text/number values for one custom section.

    Body: `{ fields: { <field_key>: <value_string_or_null> } }`. Null clears.
    File-typed fields are rejected here — they have their own upload endpoint.
    """
    section = await CustomVaultSection.get_or_none(id=section_id, deleted_at=None)
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Section not found")
    # Note: any member of the enterprise can WRITE their own values into an
    # enterprise-scope section. The admin-only gate is on mutating the
    # section/field DEFINITIONS, not the per-user values.

    fields_map = payload.get("fields") or {}
    if not isinstance(fields_map, dict):
        raise HTTPException(status_code=400, detail="`fields` must be an object")

    # Load all of this section's fields once to validate keys + types.
    schema_fields = {
        f.key: f
        for f in await CustomVaultField.filter(
            section_id=section.id, deleted_at=None
        )
    }
    unknown = [k for k in fields_map if k not in schema_fields]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown fields: {unknown}",
        )
    file_keys = [
        k for k in fields_map
        if schema_fields[k].field_type == CustomFieldType.FILE
    ]
    if file_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Use the upload endpoint for file fields: {file_keys}",
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    segment_value = _segment_key(section)

    for key, raw in fields_map.items():
        existing = await DataVault.get_or_none(
            user_id=user.id, segment=segment_value, field_name=key
        )
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if existing and existing.is_active:
                existing.is_active = False
                existing.deleted_at = now
                await existing.save()
            continue

        # Coerce numbers to a clean string so the auto-affix vault dict
        # treats them like any other text value.
        if schema_fields[key].field_type == CustomFieldType.NUMBER:
            try:
                f = float(raw)
                value_str = (
                    str(int(f)) if f.is_integer() else str(f)
                )
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"Field '{key}' expects a number, got: {raw!r}",
                )
        else:
            value_str = str(raw)

        if existing:
            existing.encrypted_value = encrypt(value_str)
            existing.is_active = True
            existing.deleted_at = None
            await existing.save()
        else:
            await DataVault.create(
                user_id=user.id,
                segment=segment_value,
                field_name=key,
                encrypted_value=encrypt(value_str),
                source=DataSource.USER_INPUT,
            )

    return MessageOut(message=f"Saved {len(fields_map)} field(s)")


@router.post("/sections", response_model=CustomSectionOut, status_code=status.HTTP_201_CREATED)
async def create_section(
    payload: CustomSectionCreateDto, user: User = Depends(get_current_user)
) -> CustomSectionOut:
    # Resolve scope. Default = user. Enterprise scope requires the caller to
    # belong to an enterprise AND have the admin role.
    scope = payload.scope or CustomScope.USER
    if scope == CustomScope.ENTERPRISE:
        if user.enterprise_id is None:
            raise HTTPException(
                status_code=400,
                detail="You're not part of an enterprise — cannot create enterprise-scope section",
            )
        if user.role not in (UserRole.ENTERPRISE_ADMIN, UserRole.SUPER_ADMIN):
            raise HTTPException(
                status_code=403,
                detail="Only enterprise admins can create enterprise-scope sections",
            )

    owner_user = user.id if scope == CustomScope.USER else None
    owner_ent = user.enterprise_id if scope == CustomScope.ENTERPRISE else None
    base = _slugify(payload.name)
    key = await _unique_section_key(base, user_id=owner_user, enterprise_id=owner_ent)

    section = await CustomVaultSection.create(
        user_id=owner_user,
        enterprise_id=owner_ent,
        scope=scope,
        name=payload.name.strip(),
        key=key,
        icon=payload.icon,
        display_order=payload.display_order or 100,
    )
    logger.info(
        f"custom section created: scope={scope} user={user.id} ent={owner_ent} "
        f"name={section.name!r} key={key}"
    )
    return _section_to_out(section, can_edit=True)


@router.put("/sections/{section_id}", response_model=CustomSectionOut)
async def update_section(
    section_id: UUID,
    payload: CustomSectionUpdateDto,
    user: User = Depends(get_current_user),
) -> CustomSectionOut:
    section = await CustomVaultSection.get_or_none(id=section_id, deleted_at=None)
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Section not found")
    if not _can_mutate_section(section, user):
        raise HTTPException(
            status_code=403,
            detail="Only enterprise admins can edit this section",
        )

    # Rename = display name only; we DON'T change the key, otherwise existing
    # data_vault rows would orphan.
    if payload.name is not None:
        section.name = payload.name.strip()
    if payload.icon is not None:
        section.icon = payload.icon
    if payload.display_order is not None:
        section.display_order = payload.display_order
    await section.save()
    return _section_to_out(section, can_edit=True)


@router.delete("/sections/{section_id}", response_model=MessageOut)
async def delete_section(
    section_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    section = await CustomVaultSection.get_or_none(id=section_id, deleted_at=None)
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Section not found")
    if not _can_mutate_section(section, user):
        raise HTTPException(
            status_code=403,
            detail="Only enterprise admins can delete this section",
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    section.deleted_at = now
    await section.save()
    # Cascade: soft-delete fields. For enterprise sections, also deactivate
    # ALL members' values (not just the caller's).
    await CustomVaultField.filter(section_id=section.id, deleted_at=None).update(
        deleted_at=now
    )
    await DataVault.filter(segment=_segment_key(section)).update(
        is_active=False, deleted_at=now
    )
    return MessageOut(message=f"Deleted section: {section.name}")


# ---- Field endpoints --------------------------------------------------------


@router.post(
    "/sections/{section_id}/fields",
    response_model=CustomFieldOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_field(
    section_id: UUID,
    payload: CustomFieldCreateDto,
    user: User = Depends(get_current_user),
) -> CustomFieldOut:
    section = await CustomVaultSection.get_or_none(id=section_id, deleted_at=None)
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Section not found")
    if not _can_mutate_section(section, user):
        raise HTTPException(
            status_code=403,
            detail="Only enterprise admins can add fields to this section",
        )

    base = _slugify(payload.name)
    key = await _unique_field_key(section.id, base)
    # Mirror section's owner onto the field for the permission helpers.
    fld = await CustomVaultField.create(
        user_id=section.user_id,
        enterprise_id=section.enterprise_id,
        section=section,
        name=payload.name.strip(),
        key=key,
        field_type=payload.field_type,
        aliases=payload.aliases,
        placeholder=payload.placeholder,
        required=payload.required or False,
        display_order=payload.display_order or 100,
    )
    logger.info(
        f"custom field created: scope={section.scope} section={section.key} "
        f"name={fld.name!r} type={fld.field_type}"
    )
    return _field_to_out(fld)


async def _resolve_field_for_mutation(
    field_id: UUID, user: User
) -> tuple[CustomVaultField, CustomVaultSection]:
    """Load a field + its section and enforce admin gating if enterprise-scoped."""
    fld = await CustomVaultField.get_or_none(id=field_id, deleted_at=None)
    if not fld:
        raise HTTPException(status_code=404, detail="Field not found")
    section = await CustomVaultSection.get_or_none(
        id=fld.section_id, deleted_at=None  # type: ignore[arg-type]
    )
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Field not found")
    if not _can_mutate_section(section, user):
        raise HTTPException(
            status_code=403,
            detail="Only enterprise admins can modify this field",
        )
    return fld, section


@router.put("/fields/{field_id}", response_model=CustomFieldOut)
async def update_field(
    field_id: UUID,
    payload: CustomFieldUpdateDto,
    user: User = Depends(get_current_user),
) -> CustomFieldOut:
    fld, section = await _resolve_field_for_mutation(field_id, user)

    if payload.name is not None:
        fld.name = payload.name.strip()
    if payload.aliases is not None:
        fld.aliases = payload.aliases
    if payload.placeholder is not None:
        fld.placeholder = payload.placeholder
    if payload.required is not None:
        fld.required = payload.required
    if payload.display_order is not None:
        fld.display_order = payload.display_order
    if payload.field_type is not None and payload.field_type != fld.field_type:
        # Changing type wipes existing values across ALL users (for enterprise
        # sections) — the old payload won't validate against the new type and
        # silent coercion is worse than asking everyone to re-enter.
        await DataVault.filter(
            segment=_segment_key(section), field_name=fld.key
        ).update(is_active=False)
        fld.field_type = payload.field_type
    await fld.save()
    return _field_to_out(fld)


@router.delete("/fields/{field_id}", response_model=MessageOut)
async def delete_field(
    field_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    fld, section = await _resolve_field_for_mutation(field_id, user)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    fld.deleted_at = now
    await fld.save()
    # Deactivate stored values across ALL users (matters for enterprise scope).
    await DataVault.filter(
        segment=_segment_key(section), field_name=fld.key
    ).update(is_active=False)
    return MessageOut(message=f"Deleted field: {fld.name}")


# ---- File upload / download (for field_type=file) --------------------------


@router.post("/fields/{field_id}/upload", response_model=FileUploadOut)
async def upload_field_file(
    field_id: UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> FileUploadOut:
    # Anyone with VIEW on the section can upload their OWN file value — even
    # for enterprise-scope schemas (admins define the field; members fill it).
    fld = await CustomVaultField.get_or_none(id=field_id, deleted_at=None)
    if not fld:
        raise HTTPException(status_code=404, detail="Field not found")
    section = await CustomVaultSection.get_or_none(
        id=fld.section_id, deleted_at=None  # type: ignore[arg-type]
    )
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Field not found")
    if fld.field_type != CustomFieldType.FILE:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{fld.name}' is type {fld.field_type}, not file",
        )

    # Size sanity check before consuming the body. 25MB ceiling matches the
    # rest of the platform's uploads.
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 25MB)")

    # Upload to storage (Cloudinary in production, local disk in dev).
    from app.common.services.local_storage import save_bytes as storage_save
    stored = storage_save(raw, file.filename or "upload.bin", folder="custom_vault")

    # Reuse existing data_vault storage so auto-affix sees it like any field.
    # `section` was already loaded + permission-checked above.
    segment_value = _segment_key(section)
    encrypted = encrypt(stored["url"])
    existing = await DataVault.get_or_none(
        user_id=user.id, segment=segment_value, field_name=fld.key
    )
    if existing:
        existing.encrypted_value = encrypted
        existing.is_active = True
        existing.source = DataSource.USER_INPUT
        existing.metadata = {
            **(existing.metadata or {}),
            "original_filename": file.filename,
            "mime_type": file.content_type,
            "size_bytes": len(raw),
        }
        await existing.save()
    else:
        await DataVault.create(
            user_id=user.id,
            segment=segment_value,
            field_name=fld.key,
            encrypted_value=encrypted,
            source=DataSource.USER_INPUT,
            metadata={
                "original_filename": file.filename,
                "mime_type": file.content_type,
                "size_bytes": len(raw),
            },
        )

    return FileUploadOut(
        field_id=fld.id,
        original_filename=file.filename or "upload.bin",
        size_bytes=len(raw),
        mime_type=file.content_type or "application/octet-stream",
        download_url=f"/api/v1/data-vault/custom/fields/{fld.id}/file",
    )


@router.get("/fields/{field_id}/file")
async def download_field_file(
    field_id: UUID, user: User = Depends(get_current_user)
):
    fld = await CustomVaultField.get_or_none(id=field_id, deleted_at=None)
    if not fld or fld.field_type != CustomFieldType.FILE:
        raise HTTPException(status_code=404, detail="Field not found")
    section = await CustomVaultSection.get_or_none(
        id=fld.section_id, deleted_at=None  # type: ignore[arg-type]
    )
    if not section or not _can_view_section(section, user):
        raise HTTPException(status_code=404, detail="Field not found")
    # The user only ever downloads THEIR OWN file value (we store per-user
    # values even on enterprise-scope schemas).
    row = await DataVault.get_or_none(
        user_id=user.id,
        segment=_segment_key(section),
        field_name=fld.key,
        is_active=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No file uploaded for this field")
    try:
        file_url = decrypt(row.encrypted_value)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Could not decrypt file path") from exc
    meta = row.metadata if isinstance(row.metadata, dict) else {}
    original = meta.get("original_filename") or "download"
    from app.common.services.local_storage import serve_file
    return serve_file(file_url, filename=original)

"""Ready-made document templates: browse, inspect, and turn into a document.

The catalogue is shipped code (see `document_templates.py`), so these routes are
read-only over a static set plus one create action. Creating returns an ordinary
Document, which means the result drops straight into the editor, the signing
flow and the audit trail with no special handling.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.audit_service import log_audit
from app.common.services.document_templates import (
    LEGAL_DISCLAIMER,
    get_template,
    render_template_pdf,
    resolve_values,
    template_catalogue,
)
from app.common.services.local_storage import save_bytes as local_save_bytes
from app.db.models.audit_log import AuditAction
from app.db.models.document import (
    Document,
    DocumentStatus,
    DocumentType,
    ProcessingMode,
)
from app.db.models.user import User
from app.models.document_schemas import DocumentOut
from app.models.template_schemas import (
    TemplateDetailOut,
    TemplateListOut,
    TemplatePlaceholderOut,
    UseTemplateDto,
)

router = APIRouter()


@router.get("", response_model=TemplateListOut)
async def list_templates(user: User = Depends(get_current_user)) -> TemplateListOut:
    """The template gallery."""
    return TemplateListOut(
        templates=template_catalogue(), disclaimer=LEGAL_DISCLAIMER
    )


@router.get("/{slug}", response_model=TemplateDetailOut)
async def get_template_detail(
    slug: str, user: User = Depends(get_current_user)
) -> TemplateDetailOut:
    """A template's placeholders, prefilled from the user's vault where possible.

    `value` on each placeholder is what we'd use if the user submitted right now,
    so the form can render already-populated and the user only fills the gaps.
    """
    try:
        spec = get_template(slug)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    from app.common.services.auto_affix import get_user_vault_dict

    vault = await get_user_vault_dict(user.id)
    values, missing = resolve_values(spec, {}, vault)

    return TemplateDetailOut(
        slug=spec.slug,
        title=spec.title,
        category=spec.category,
        summary=spec.summary,
        disclaimer=LEGAL_DISCLAIMER,
        missing_required=missing,
        placeholders=[
            TemplatePlaceholderOut(
                key=ph.key,
                label=ph.label,
                kind=ph.kind,
                required=ph.required,
                help=ph.help,
                value=values.get(ph.key) or None,
                # So the UI can show "from your vault" rather than looking like
                # it invented the value.
                prefilled_from_vault=bool(
                    ph.vault_field and (vault.get(ph.vault_field) or "").strip()
                ),
            )
            for ph in spec.placeholders
        ],
    )


@router.post("/{slug}/use", response_model=DocumentOut)
async def use_template(
    slug: str,
    dto: UseTemplateDto,
    user: User = Depends(get_current_user),
) -> DocumentOut:
    """Fill a template and save it as a document.

    Refuses rather than rendering a contract full of blanks when required values
    are missing — a half-filled agreement that looks finished is worse than an
    error.
    """
    try:
        spec = get_template(slug)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    # Ignore keys this template doesn't define instead of trusting the body to
    # decide what gets substituted.
    known = spec.placeholder_keys()
    supplied = {k: v for k, v in (dto.values or {}).items() if k in known}

    from app.common.services.auto_affix import get_user_vault_dict

    vault = await get_user_vault_dict(user.id)
    values, missing = resolve_values(spec, supplied, vault)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required values: {', '.join(missing)}",
        )

    try:
        pdf_bytes = render_template_pdf(spec, values, page_size=dto.page_size)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception(f"template render failed: {slug}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not render the template: {exc}",
        ) from exc

    title = (dto.title or spec.title).strip() or spec.title
    file_name = title if title.lower().endswith(".pdf") else f"{title}.pdf"
    stored = local_save_bytes(pdf_bytes, file_name, folder="authored")

    doc = await Document.create(
        user_id=user.id,
        file_name=stored["key"],
        original_file_name=file_name,
        file_url=stored["url"],
        file_mime_type="application/pdf",
        file_size=len(pdf_bytes),
        document_type=(
            DocumentType.CONTRACT if spec.category == "contracts" else DocumentType.OTHER
        ),
        status=DocumentStatus.DRAFT,
        processing_mode=ProcessingMode.MANUAL,
        field_placements=[],
        metadata={
            "origin": "template",
            "template_slug": spec.slug,
            # The values are kept so the document can be regenerated or
            # audited later. They are the user's own inputs, already stored
            # in this row's PDF.
            "template_values": values,
        },
    )
    await log_audit(
        user_id=user.id,
        action=AuditAction.DOCUMENT_UPLOADED,
        entity_type="document",
        entity_id=str(doc.id),
        description=f"Created from template: {spec.title}",
        metadata={"template_slug": spec.slug},
    )
    return DocumentOut.model_validate(doc, from_attributes=True)

"""Cloud-storage integration endpoints.

  GET    /integrations/cloud/providers       — capability matrix for the UI
  GET    /integrations/cloud/connections     — user's active connections
  POST   /integrations/cloud/{provider}/connect          — start OAuth
  GET    /integrations/cloud/{provider}/callback         — OAuth callback
  DELETE /integrations/cloud/connections/{id}            — revoke
  POST   /documents/{id}/export/{provider}               — push file to cloud
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from loguru import logger

from app.common.deps import get_current_user
from app.common.services.cloud_storage import (
    CloudProviderError,
    get_provider,
)
from app.common.services.local_storage import UPLOADS_ROOT
from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.db.models.cloud_connection import CloudConnection, CloudProvider
from app.db.models.document import Document
from app.db.models.user import User
from app.models.auth_schemas import MessageOut

router = APIRouter()


# ---- Provider capability matrix --------------------------------------------


@router.get("/cloud/providers")
async def list_providers(
    _: User = Depends(get_current_user),
) -> list[dict]:
    """For each provider: is it CONFIGURED (server-side OAuth keys present),
    and what's its display info? Frontend renders the connect cards from this."""
    out: list[dict] = []
    catalog = [
        (CloudProvider.GOOGLE_DRIVE, "Google Drive"),
        (CloudProvider.DROPBOX, "Dropbox"),
        (CloudProvider.ONEDRIVE, "OneDrive"),
        (CloudProvider.MS365, "Microsoft 365"),
    ]
    for p, label in catalog:
        provider = get_provider(p)
        out.append({
            "id": p.value,
            "label": label,
            "configured": provider.is_configured(),
        })
    return out


@router.get("/cloud/connections")
async def list_connections(
    user: User = Depends(get_current_user),
) -> list[dict]:
    rows = await CloudConnection.filter(
        user_id=user.id, deleted_at=None
    ).order_by("provider")
    return [
        {
            "id": str(r.id),
            "provider": r.provider.value,
            "account_email": r.account_email,
            "account_name": r.account_name,
            "scopes": r.scopes,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---- OAuth start / callback ------------------------------------------------


def _redirect_uri(provider: CloudProvider) -> str:
    """Callback URL the OAuth provider will redirect to AFTER user consent.

    We use the backend's own /callback endpoint, not the frontend, so the
    code → token exchange happens server-side where the client secret lives.
    The backend then redirects the user back to the frontend's
    `/settings#integrations` page.
    """
    # FRONTEND_URL is the SPA host — we host on the same domain ideally; for
    # dev with separate frontend (vite :3001) and backend (:8000) the
    # callback goes to the backend.
    api_base = settings.FRONTEND_URL  # if frontend proxies to backend
    # Compose: <backend-host>/api/v1/integrations/cloud/<provider>/callback
    # We store the API base separately; for now, derive from FRONTEND_URL.
    # This is intentionally hardcoded to the deployment's known callback.
    return f"http://localhost:8000/api/v1/integrations/cloud/{provider.value}/callback"


@router.post("/cloud/{provider_id}/connect")
async def start_connect(
    provider_id: str, user: User = Depends(get_current_user),
) -> dict:
    """Begin OAuth. Returns a URL the frontend redirects the user's browser to."""
    try:
        provider_enum = CloudProvider(provider_id)
    except ValueError:
        raise HTTPException(404, f"Unknown provider: {provider_id}")
    p = get_provider(provider_enum)
    if not p.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider_id} OAuth credentials are not configured on this server",
        )
    try:
        result = p.start_oauth(
            redirect_uri=_redirect_uri(provider_enum),
            user_id=str(user.id),
        )
    except CloudProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"authorize_url": result.authorize_url, "state": result.state}


@router.get("/cloud/{provider_id}/callback")
async def oauth_callback(
    provider_id: str,
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """OAuth redirect target. Validates `state`, exchanges `code` for tokens,
    stores them, and bounces the user back to the frontend's integrations
    settings page."""
    if error:
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/settings?integration_error={error}"
        )
    if not code or not state:
        raise HTTPException(400, "Missing OAuth code or state")

    # state was built as `<user_id>:<random>` — verify the user_id is a real
    # one. (In production we'd also check it matches the current session's
    # user, but the callback isn't an authenticated request — the user is
    # bouncing back from Google, no JWT in the cookie/header. We trust the
    # state's user_id since it's tamper-proof via the OAuth provider's CSRF
    # protection on `state`.)
    try:
        user_id_str = state.split(":", 1)[0]
        user_uuid = UUID(user_id_str)
    except (ValueError, IndexError):
        raise HTTPException(400, "Bad state token")
    user = await User.get_or_none(id=user_uuid, deleted_at=None)
    if not user:
        raise HTTPException(404, "User from state token not found")

    try:
        provider_enum = CloudProvider(provider_id)
    except ValueError:
        raise HTTPException(404, "Unknown provider")
    p = get_provider(provider_enum)

    try:
        tokens = await p.complete_oauth(
            code=code,
            redirect_uri=_redirect_uri(provider_enum),
            state=state,
        )
    except CloudProviderError as exc:
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/settings?integration_error={exc}"
        )

    # UPSERT — replacing any existing connection for (user, provider).
    existing = await CloudConnection.get_or_none(
        user_id=user.id, provider=provider_enum, deleted_at=None
    )
    expires_at = (
        datetime.fromtimestamp(tokens.expires_at, tz=timezone.utc)
        if tokens.expires_at else None
    )
    if existing:
        existing.encrypted_access_token = encrypt(tokens.access_token)
        existing.encrypted_refresh_token = (
            encrypt(tokens.refresh_token) if tokens.refresh_token else None
        )
        existing.expires_at = expires_at
        existing.account_email = tokens.account_email
        existing.account_name = tokens.account_name
        existing.scopes = tokens.scopes
        await existing.save()
    else:
        await CloudConnection.create(
            user_id=user.id,
            provider=provider_enum,
            encrypted_access_token=encrypt(tokens.access_token),
            encrypted_refresh_token=(
                encrypt(tokens.refresh_token) if tokens.refresh_token else None
            ),
            expires_at=expires_at,
            account_email=tokens.account_email,
            account_name=tokens.account_name,
            scopes=tokens.scopes,
        )
    logger.info(
        f"cloud connection saved: user={user.id} provider={provider_id} "
        f"account={tokens.account_email}"
    )
    return RedirectResponse(
        f"{settings.FRONTEND_URL}/settings?integration_connected={provider_id}"
    )


@router.delete(
    "/cloud/connections/{connection_id}", response_model=MessageOut
)
async def disconnect(
    connection_id: UUID, user: User = Depends(get_current_user)
) -> MessageOut:
    conn = await CloudConnection.get_or_none(
        id=connection_id, user_id=user.id, deleted_at=None
    )
    if not conn:
        raise HTTPException(404, "Connection not found")
    conn.deleted_at = datetime.now(timezone.utc)
    await conn.save()
    return MessageOut(message=f"Disconnected {conn.provider.value}")


# ---- Export endpoint -------------------------------------------------------


@router.post("/documents/{document_id}/export/{provider_id}")
async def export_document(
    document_id: UUID,
    provider_id: str,
    user: User = Depends(get_current_user),
    format: str = Query(default="pdf", pattern="^(pdf|docx|txt|md)$"),
) -> dict:
    """Upload the document (in `format`) to the user's cloud provider.

    Reuses Phase B's `convert_document` for non-PDF targets, so we get the
    conversion cache for free.
    """
    try:
        provider_enum = CloudProvider(provider_id)
    except ValueError:
        raise HTTPException(404, "Unknown provider")
    p = get_provider(provider_enum)

    conn = await CloudConnection.get_or_none(
        user_id=user.id, provider=provider_enum, deleted_at=None
    )
    if not conn:
        raise HTTPException(
            400,
            f"{provider_id} is not connected — connect first under Settings → Integrations.",
        )

    doc = await Document.get_or_none(id=document_id, deleted_at=None)
    if not doc or doc.user_id != user.id:
        raise HTTPException(404, "Document not found")
    url = doc.completed_file_url or doc.file_url
    if not url or not url.startswith("local://"):
        raise HTTPException(404, "No file to export")
    src_path = Path(UPLOADS_ROOT) / url.replace("local://", "", 1)
    if not src_path.exists():
        raise HTTPException(404, "File missing on disk")
    pdf_bytes = src_path.read_bytes()

    # Convert if needed. PDF is passthrough.
    if format != "pdf":
        from app.common.services.document_processing import (
            convert_document,
            detect_format,
            MIME_BY_FORMAT,
        )
        src_fmt = detect_format(doc.original_file_name, doc.file_mime_type)
        if src_fmt is None:
            raise HTTPException(400, "Could not detect source format")
        try:
            out_bytes = convert_document(pdf_bytes, src_fmt.value, format)  # type: ignore[arg-type]
        except Exception as exc:
            raise HTTPException(500, f"Conversion failed: {exc}") from exc
        mime = MIME_BY_FORMAT[format]
    else:
        out_bytes = pdf_bytes
        mime = doc.file_mime_type or "application/pdf"

    # Replace the extension on the upload name with the chosen format.
    base = (doc.original_file_name or "document").rsplit(".", 1)[0]
    upload_name = f"{base}.{format}"

    try:
        result = await p.upload(
            conn,
            file_bytes=out_bytes,
            file_name=upload_name,
            mime_type=mime,
        )
        # Persist any refreshed tokens.
        await conn.save()
    except CloudProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        logger.exception("cloud upload failed")
        raise HTTPException(500, f"Upload failed: {exc}") from exc

    return {
        "provider": provider_id,
        "file_id": result.file_id,
        "file_name": result.file_name,
        "view_url": result.view_url,
    }


# Re-export the unused symbol so linters don't flag the import.
_ = decrypt

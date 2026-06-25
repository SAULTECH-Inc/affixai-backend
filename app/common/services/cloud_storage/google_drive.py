"""Google Drive provider — OAuth + upload.

Uses google-auth's installed-app + refresh-token flow. The Drive API call
itself uses googleapiclient v3.
"""
from __future__ import annotations

import asyncio
import io
import secrets
import time
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.db.models.cloud_connection import CloudConnection

from .base import (
    AuthCompleteResult,
    AuthStartResult,
    CloudProviderError,
    CloudStorageProvider,
    UploadResult,
)


# Drive API scope. `drive.file` only sees files our app created — narrower
# than `drive` which would let us touch everything in the user's Drive. We
# pick the narrower scope so the consent prompt is less scary AND so a
# compromised access token can't exfiltrate the user's entire Drive.
_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.file", "openid", "email", "profile"]
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class GoogleDriveProvider(CloudStorageProvider):
    name = "google_drive"

    def is_configured(self) -> bool:
        return bool(
            settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET
        )

    # ---- OAuth -----------------------------------------------------------

    def start_oauth(self, *, redirect_uri: str, user_id: str) -> AuthStartResult:
        if not self.is_configured():
            raise CloudProviderError("Google OAuth is not configured")
        # State token defends against CSRF + transports the user_id we'll
        # need on callback. The state is just `<user_id>:<random>` —
        # opaque enough; we verify the user_id matches the current session
        # in the callback handler.
        state = f"{user_id}:{secrets.token_urlsafe(16)}"
        params = {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(_GOOGLE_SCOPES),
            "access_type": "offline",       # gets us a refresh_token
            "include_granted_scopes": "true",
            "prompt": "consent",            # force the refresh_token even on re-connect
            "state": state,
        }
        from urllib.parse import urlencode
        return AuthStartResult(
            authorize_url=f"{_AUTHORIZE_URL}?{urlencode(params)}",
            state=state,
        )

    async def complete_oauth(
        self, *, code: str, redirect_uri: str, state: str | None = None,
    ) -> AuthCompleteResult:
        import httpx

        data = {
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(_TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise CloudProviderError(f"Google token exchange network error: {exc}") from exc
        if r.status_code >= 400:
            raise CloudProviderError(
                f"Google token exchange failed: {r.status_code} {r.text[:300]}"
            )
        body = r.json()
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        expires_in = body.get("expires_in")
        scopes = (body.get("scope") or "").split()
        if not access:
            raise CloudProviderError("Google returned no access_token")

        # Identify the user we just authorized — gives us a display
        # email/name for the connections list.
        account_email: str | None = None
        account_name: str | None = None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                ui = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access}"},
                )
            if ui.status_code < 400:
                d = ui.json()
                account_email = d.get("email")
                account_name = d.get("name")
        except Exception as exc:
            logger.warning(f"google userinfo lookup failed: {exc}")

        return AuthCompleteResult(
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + float(expires_in) if expires_in else None,
            account_email=account_email,
            account_name=account_name,
            scopes=scopes,
        )

    async def refresh_if_needed(self, connection: CloudConnection) -> None:
        # Only refresh if the token expires within ~5 minutes — saves us a
        # network hop on every upload.
        if connection.expires_at is None:
            return
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        expiry = connection.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if (expiry - now).total_seconds() > 300:
            return  # still fresh
        if not connection.encrypted_refresh_token:
            raise CloudProviderError(
                "Token expired and no refresh_token available — reconnect Google Drive"
            )

        refresh_token = decrypt(connection.encrypted_refresh_token)
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(_TOKEN_URL, data={
                    "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                })
        except httpx.HTTPError as exc:
            raise CloudProviderError(f"Google refresh network error: {exc}") from exc
        if r.status_code >= 400:
            raise CloudProviderError(
                f"Google refresh failed: {r.status_code} {r.text[:300]}"
            )
        body = r.json()
        access = body.get("access_token")
        expires_in = body.get("expires_in")
        if not access:
            raise CloudProviderError("Google refresh returned no access_token")
        connection.encrypted_access_token = encrypt(access)
        from datetime import timedelta
        connection.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in or 3600)
        )
        # Refresh tokens are sometimes rotated; persist the new one if so.
        if (new_refresh := body.get("refresh_token")):
            connection.encrypted_refresh_token = encrypt(new_refresh)

    # ---- Upload ----------------------------------------------------------

    async def upload(
        self,
        connection: CloudConnection,
        *,
        file_bytes: bytes,
        file_name: str,
        mime_type: str,
        folder_id: str | None = None,
    ) -> UploadResult:
        await self.refresh_if_needed(connection)
        try:
            access = decrypt(connection.encrypted_access_token)
        except Exception as exc:
            raise CloudProviderError(f"Could not decrypt access token: {exc}") from exc

        # googleapiclient is sync — wrap in a thread so we don't block the
        # event loop while uploading.
        def _do() -> dict[str, Any]:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseUpload

            creds = Credentials(token=access)
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            metadata: dict[str, Any] = {"name": file_name}
            if folder_id:
                metadata["parents"] = [folder_id]
            media = MediaIoBaseUpload(
                io.BytesIO(file_bytes),
                mimetype=mime_type,
                resumable=False,
            )
            created = (
                service.files()
                .create(body=metadata, media_body=media, fields="id, name, webViewLink")
                .execute()
            )
            return created

        try:
            created = await asyncio.to_thread(_do)
        except Exception as exc:
            # googleapiclient raises googleapiclient.errors.HttpError on 4xx/5xx
            raise CloudProviderError(f"Google Drive upload failed: {exc}") from exc

        return UploadResult(
            file_id=created.get("id", ""),
            file_name=created.get("name", file_name),
            view_url=created.get("webViewLink"),
        )

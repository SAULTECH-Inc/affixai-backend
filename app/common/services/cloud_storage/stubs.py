"""Scaffolds for cloud providers that aren't fully implemented yet.

Each stub keeps the `CloudStorageProvider` shape so routes can introspect
`is_configured()` and the UI can render "Coming soon" gracefully. Wiring
any one of these is a focused chunk:

  Dropbox  — OAuth2 + /2/files/upload — 1-2h
  OneDrive — Microsoft Graph (personal accounts) — 2-3h
  MS365    — Microsoft Graph (work / SharePoint Drives) — 3-5h
"""
from __future__ import annotations

from app.db.models.cloud_connection import CloudConnection

from .base import (
    AuthCompleteResult,
    AuthStartResult,
    CloudProviderError,
    CloudStorageProvider,
    UploadResult,
)


class _NotYetImpl(CloudStorageProvider):
    """Common implementation that just raises 'not yet implemented' for
    every action. The concrete stubs below set `name` + customize the
    error message."""

    def is_configured(self) -> bool:
        return False  # stubs are never "configured" until we write the real thing

    def start_oauth(self, **kwargs) -> AuthStartResult:  # type: ignore[override]
        raise CloudProviderError(
            f"{self.name} integration isn't ready yet — coming soon."
        )

    async def complete_oauth(self, **kwargs) -> AuthCompleteResult:  # type: ignore[override]
        raise CloudProviderError(
            f"{self.name} integration isn't ready yet — coming soon."
        )

    async def refresh_if_needed(self, connection: CloudConnection) -> None:
        raise CloudProviderError(
            f"{self.name} integration isn't ready yet — coming soon."
        )

    async def upload(
        self, connection: CloudConnection, **kwargs
    ) -> UploadResult:  # type: ignore[override]
        raise CloudProviderError(
            f"{self.name} integration isn't ready yet — coming soon."
        )


class DropboxProvider(_NotYetImpl):
    name = "dropbox"


class OneDriveProvider(_NotYetImpl):
    name = "onedrive"


class MS365Provider(_NotYetImpl):
    name = "ms365"

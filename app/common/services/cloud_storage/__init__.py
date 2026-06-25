"""Provider-agnostic cloud storage exports.

Routes / services don't import a specific provider — they go through
`get_provider(name)` which returns whichever implementation matches. Today
that's Google Drive fully + scaffolded stubs for Dropbox / OneDrive / MS365
that return a clear "not yet implemented" error so the UI can render
"Coming soon" gracefully.
"""
from __future__ import annotations

from .base import CloudStorageProvider, CloudProviderError, UploadResult
from .google_drive import GoogleDriveProvider
from .stubs import DropboxProvider, OneDriveProvider, MS365Provider

from app.db.models.cloud_connection import CloudProvider


_PROVIDERS: dict[CloudProvider, type[CloudStorageProvider]] = {
    CloudProvider.GOOGLE_DRIVE: GoogleDriveProvider,
    CloudProvider.DROPBOX: DropboxProvider,
    CloudProvider.ONEDRIVE: OneDriveProvider,
    CloudProvider.MS365: MS365Provider,
}


def get_provider(provider: CloudProvider) -> CloudStorageProvider:
    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise CloudProviderError(f"Unknown cloud provider: {provider}")
    return cls()


__all__ = [
    "CloudStorageProvider",
    "CloudProviderError",
    "UploadResult",
    "get_provider",
]

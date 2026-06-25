"""Common interface every cloud storage provider must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.db.models.cloud_connection import CloudConnection


class CloudProviderError(Exception):
    """Raised for any provider-specific failure. Routes catch this and
    map to a 502 / 503 / 501 depending on the cause."""


@dataclass
class UploadResult:
    """Surface what the user needs to know after a successful upload."""
    file_id: str          # provider-native id
    file_name: str
    view_url: str | None  # link the user can click to open it remotely


@dataclass
class AuthStartResult:
    authorize_url: str    # URL the user's browser is redirected to
    state: str            # opaque token we'll verify on callback


@dataclass
class AuthCompleteResult:
    access_token: str
    refresh_token: str | None
    expires_at: float | None  # epoch seconds
    account_email: str | None
    account_name: str | None
    scopes: list[str]


class CloudStorageProvider(ABC):
    """Provider-agnostic operations.

    Every implementation is responsible for its own SDK quirks (Google's
    `googleapiclient` vs Dropbox's REST API vs Microsoft Graph) and exposes
    only the cleaned-up shapes above.
    """

    name: str = "base"

    # --- Configuration -----------------------------------------------------

    @abstractmethod
    def is_configured(self) -> bool:
        """True iff the server has client_id + secret env vars for this
        provider. Routes return 503 cleanly when False."""

    # --- OAuth flow --------------------------------------------------------

    @abstractmethod
    def start_oauth(self, *, redirect_uri: str, user_id: str) -> AuthStartResult:
        """Build the consent URL the user's browser visits to authorize us."""

    @abstractmethod
    async def complete_oauth(
        self, *, code: str, redirect_uri: str, state: str | None = None,
    ) -> AuthCompleteResult:
        """Exchange the OAuth `code` for tokens + identify the remote
        account."""

    @abstractmethod
    async def refresh_if_needed(self, connection: CloudConnection) -> None:
        """If the access token is close to / past expiry, refresh in-place.

        Idempotent — implementations should no-op when the existing token
        still has plenty of life. Saves the updated tokens to `connection`
        but DOES NOT commit (the caller's responsibility to .save())."""

    # --- File upload -------------------------------------------------------

    @abstractmethod
    async def upload(
        self,
        connection: CloudConnection,
        *,
        file_bytes: bytes,
        file_name: str,
        mime_type: str,
        folder_id: str | None = None,
    ) -> UploadResult:
        """Upload `file_bytes` as `file_name` to the connected account.
        `folder_id` is provider-native; passing None means the user's root."""

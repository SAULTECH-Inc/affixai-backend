from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

from app.db.models.webhook_endpoint import WebhookEndpointStatus


class CreateWebhookDto(BaseModel):
    url: HttpUrl
    name: str | None = Field(default=None, max_length=120)
    # Empty list ⇒ all events. Otherwise a subset of WebhookEventType.
    events: list[str] = Field(default_factory=list)
    scope: str = "user"  # "user" or "enterprise"


class UpdateWebhookDto(BaseModel):
    url: HttpUrl | None = None
    name: str | None = None
    events: list[str] | None = None
    status: WebhookEndpointStatus | None = None


class WebhookOut(BaseModel):
    id: UUID
    user_id: UUID | None
    enterprise_id: UUID | None
    url: str
    name: str | None
    events: list | None
    status: WebhookEndpointStatus
    delivery_attempts: int
    delivery_successes: int
    delivery_failures: int
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_reason: str | None
    created_at: datetime


class WebhookCreatedOut(WebhookOut):
    """Only the CREATE response includes the secret. Stored only as a hash
    in our future hardening pass; for the MVP we keep the secret plaintext
    in the DB so we can re-sign on every dispatch."""
    secret: str


class TestPingResultOut(BaseModel):
    ok: bool
    status_code: int
    body: str

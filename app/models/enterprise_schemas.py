from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models.api_key import ApiKeyStatus, ApiKeyType
from app.db.models.enterprise import EnterprisePlan, EnterpriseStatus


class CreateEnterpriseDto(BaseModel):
    name: str
    domain: str | None = None
    description: str | None = None
    contact_email: EmailStr
    contact_phone: str | None = None
    plan: EnterprisePlan = EnterprisePlan.STARTER
    max_users: int | None = None
    max_documents: int | None = None
    max_api_calls: int | None = None


class UpdateEnterpriseDto(BaseModel):
    name: str | None = None
    description: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    status: EnterpriseStatus | None = None
    plan: EnterprisePlan | None = None
    max_users: int | None = None
    max_documents: int | None = None
    max_api_calls: int | None = None


class EnterpriseOut(BaseModel):
    id: UUID
    name: str
    domain: str | None
    description: str | None
    logo_url: str | None
    status: EnterpriseStatus
    plan: EnterprisePlan
    contact_email: str | None
    contact_phone: str | None
    address: dict | None
    max_users: int
    max_documents: int
    max_api_calls: int
    features: list | None
    trial_ends_at: datetime | None
    subscription_starts_at: datetime | None
    subscription_ends_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EnterpriseStatsOut(BaseModel):
    users: int
    documents: int
    api_calls: int
    active_api_keys: int
    limits: dict


class EnterpriseDocumentOut(BaseModel):
    id: UUID
    original_file_name: str
    file_size: int
    status: str
    document_type: str
    completed_at: datetime | None
    created_at: datetime


class CreateApiKeyDto(BaseModel):
    name: str
    description: str | None = None
    key_type: ApiKeyType = ApiKeyType.TEST
    permissions: list[str] | None = None
    ip_whitelist: list[str] | None = None
    rate_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    id: UUID
    enterprise_id: UUID
    name: str
    description: str | None
    key_type: ApiKeyType
    status: ApiKeyStatus
    permissions: list | None
    ip_whitelist: list | None
    usage_count: int
    rate_limit: int | None
    last_used_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class CreateApiKeyOut(BaseModel):
    api_key: ApiKeyOut
    key: str  # plaintext, shown once

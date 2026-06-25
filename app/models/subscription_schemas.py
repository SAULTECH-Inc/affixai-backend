from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

from app.db.models.invoice import InvoiceStatus
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus


class SubscriptionOut(BaseModel):
    id: UUID
    user_id: UUID
    plan: SubscriptionPlan
    status: SubscriptionStatus
    trial_ends_at: datetime | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    stripe_customer_id: str | None
    stripe_subscription_id: str | None


class PlanOut(BaseModel):
    """One row in the public plans catalog."""
    plan: SubscriptionPlan
    name: str
    description: str
    price_id: str | None
    # Amount per billing period, in the active currency. Optional because
    # legacy callers may not have prices set yet.
    amount: Decimal | None = None
    currency: str | None = None
    interval: str = "month"  # "month" | "year"
    features: list[str] = Field(default_factory=list)
    free_trial_days: int | None = None


class CheckoutDto(BaseModel):
    plan: SubscriptionPlan = SubscriptionPlan.PRO
    success_url: HttpUrl | None = None
    cancel_url: HttpUrl | None = None


class CheckoutOut(BaseModel):
    checkout_url: str


class PortalDto(BaseModel):
    return_url: HttpUrl | None = None


class PortalOut(BaseModel):
    portal_url: str


class SubscriptionStateOut(BaseModel):
    """Frontend-friendly view: what the user can do right now."""
    plan: SubscriptionPlan
    status: SubscriptionStatus
    provider: str | None = None
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    has_paid_features: bool
    free_tier_enabled: bool
    active_provider: str
    currency: str
    days_left: int | None = Field(
        default=None,
        description="Days until current access ends (trial or paid period). Null if indefinite or expired.",
    )


class InvoiceOut(BaseModel):
    id: UUID
    provider: str
    provider_invoice_id: str
    amount: Decimal
    currency: str
    status: InvoiceStatus
    description: str | None
    hosted_url: str | None
    pdf_url: str | None
    paid_at: datetime | None
    created_at: datetime


class ExtendTrialDto(BaseModel):
    """Admin: extend a user's free trial by N days."""
    days: int = Field(gt=0, le=365, description="How many days to add to trial_ends_at")
    reason: str | None = None


class AdminUserRow(BaseModel):
    """Slim user view for the admin users table."""
    id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    role: str
    status: str
    enterprise_id: UUID | None
    plan: SubscriptionPlan | None
    sub_status: SubscriptionStatus | None
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    last_login_at: datetime | None
    created_at: datetime


class AdminUsersOut(BaseModel):
    items: list[AdminUserRow]
    total: int
    limit: int
    offset: int


class AdminStatsOut(BaseModel):
    total_users: int
    active_subscriptions: int     # status=ACTIVE
    trialing: int                  # status=TRIALING
    documents_this_month: int     # docs created since UTC month start
    invoices_this_month: int      # paid invoices since UTC month start
    revenue_this_month: Decimal   # sum of paid invoice amounts this month
    currency: str
    enterprises: int

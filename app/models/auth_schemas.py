from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models.user import AuthProvider, UserRole, UserStatus


class RegisterDto(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    first_name: str | None = None
    last_name: str | None = None
    # Two-letter ISO 3166-1 country code. Used to pick the payment gateway
    # (Paystack for NG, Flutterwave for other Africa, Stripe elsewhere) and
    # to show region-appropriate pricing. If the client doesn't send it,
    # the server falls back to CDN-edge headers (CF-IPCountry / Vercel) at
    # registration time.
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    # Optional referral code from the landing page's `?ref=` query param,
    # captured client-side and forwarded at signup. Unknown codes are
    # silently ignored — never block a signup over a bad ref.
    referral_code: str | None = Field(default=None, max_length=24)


class LoginDto(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailDto(BaseModel):
    token: str


class ForgotPasswordDto(BaseModel):
    email: EmailStr


class ResetPasswordDto(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class RefreshTokenDto(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    phone_number: str | None
    profile_image: str | None
    auth_provider: AuthProvider
    role: UserRole
    status: UserStatus
    email_verified: bool
    enterprise_id: UUID | None
    last_login_at: datetime | None
    preferences: dict | None
    # ISO 3166-1 alpha-2 country code — drives payment-gateway routing.
    country_code: str | None = None
    created_at: datetime
    updated_at: datetime


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str


class AuthResponse(BaseModel):
    user: UserOut
    access_token: str
    refresh_token: str
    verification_token: str | None = None


class MessageOut(BaseModel):
    message: str

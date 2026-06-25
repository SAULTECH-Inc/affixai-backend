"""Authentication routes: register, login, OAuth, email verification, password reset."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from loguru import logger

from app.common.services.audit_service import log_audit
from app.common.services.email_service import (
    send_password_reset_email,
    send_verification_email,
)
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models.audit_log import AuditAction
from app.db.models.user import AuthProvider, User, UserRole, UserStatus
from app.models.auth_schemas import (
    AuthResponse,
    ForgotPasswordDto,
    LoginDto,
    MessageOut,
    RefreshTokenDto,
    RegisterDto,
    ResetPasswordDto,
    TokensOut,
    UserOut,
    VerifyEmailDto,
)

router = APIRouter()


def _user_out(user: User) -> UserOut:
    return UserOut.model_validate(user, from_attributes=True)


def _issue_tokens(user: User) -> tuple[str, str]:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role.value if hasattr(user.role, "value") else user.role,
        "enterprise_id": str(user.enterprise_id) if user.enterprise_id else None,
    }
    return create_access_token(payload), create_refresh_token({"sub": str(user.id)})


def _admin_emails() -> set[str]:
    """Parse SUPER_ADMIN_EMAILS into a normalized set. Comma OR whitespace
    separated, case-insensitive, blank entries dropped.
    """
    raw = (settings.SUPER_ADMIN_EMAILS or "").replace(",", " ")
    return {e.strip().lower() for e in raw.split() if e.strip()}


async def _bootstrap_super_admin(user: User) -> None:
    """If this user's email is in SUPER_ADMIN_EMAILS, ensure they have the
    SUPER_ADMIN role. Idempotent — safe to call on every login.

    Re-promoting on login matters: if you add an email to the allowlist after
    the user already exists, we want the next login to upgrade them.
    """
    if not user.email:
        return
    if user.email.lower() not in _admin_emails():
        return
    if user.role == UserRole.SUPER_ADMIN:
        return
    user.role = UserRole.SUPER_ADMIN
    await user.save(update_fields=["role"])
    logger.info(f"Promoted {user.email} to SUPER_ADMIN via env allowlist")


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterDto, request: Request) -> AuthResponse:
    existing = await User.get_or_none(email=payload.email.lower())
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    verification_token = uuid4().hex

    # Country: prefer the value the client supplied (from the registration
    # form's picker / browser-locale guess); fall back to a CDN edge header
    # (CF-IPCountry / Vercel) if the request was proxied through one. Both
    # absent → null, and payment routing falls back to the platform default.
    from app.common.geo import country_from_request

    country = (payload.country_code or "").upper() or country_from_request(request)

    user = await User.create(
        email=payload.email.lower(),
        password=hash_password(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
        country_code=country,
        verification_token=verification_token,
        status=UserStatus.PENDING_VERIFICATION,
        auth_provider=AuthProvider.LOCAL,
    )

    # Auto-promote env-allowlisted emails to platform admin.
    await _bootstrap_super_admin(user)

    # Wire up the referral if the registration carried a ?ref= code.
    # Best-effort — unknown codes silently no-op, see referral_service.
    if payload.referral_code:
        try:
            from app.common.services.referral_service import attribute_signup
            await attribute_signup(
                referred_user_id=user.id,
                code=payload.referral_code,
            )
        except Exception as exc:
            logger.warning(f"referral attribution failed for {user.email}: {exc}")

    # Provision the 30-day trial subscription. Best-effort: failure here
    # shouldn't block signup — the `require_active_subscription` dependency
    # will create the row lazily on first paid-feature access.
    try:
        from app.common.services.subscription_service import ensure_subscription
        await ensure_subscription(user)
    except Exception as exc:
        logger.warning(f"trial provisioning failed for {user.email}: {exc}")

    access, refresh = _issue_tokens(user)

    await send_verification_email(user.email, verification_token)
    await log_audit(
        user_id=user.id,
        action=AuditAction.USER_CREATED,
        entity_type="user",
        entity_id=str(user.id),
        description=f"User registered: {user.email}",
        ip_address=request.client.host if request.client else None,
    )

    return AuthResponse(
        user=_user_out(user),
        access_token=access,
        refresh_token=refresh,
        verification_token=verification_token if settings.DEBUG else None,
    )


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginDto, request: Request) -> AuthResponse:
    user = await User.get_or_none(email=payload.email.lower(), deleted_at=None)
    if not user or not user.password or not verify_password(payload.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    if user.status == UserStatus.SUSPENDED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")

    user.last_login_at = datetime.now(timezone.utc)
    user.last_login_ip = request.client.host if request.client else None
    await user.save(update_fields=["last_login_at", "last_login_ip", "updated_at"])

    # Re-check the env allowlist each login so admins added after registration
    # get promoted next time they sign in.
    await _bootstrap_super_admin(user)

    access, refresh = _issue_tokens(user)
    await log_audit(
        user_id=user.id,
        action=AuditAction.USER_LOGIN,
        entity_type="user",
        entity_id=str(user.id),
        ip_address=user.last_login_ip,
    )
    return AuthResponse(user=_user_out(user), access_token=access, refresh_token=refresh)


@router.post("/verify-email", response_model=MessageOut)
async def verify_email(payload: VerifyEmailDto) -> MessageOut:
    user = await User.get_or_none(verification_token=payload.token)
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    user.email_verified = True
    user.verification_token = None
    user.status = UserStatus.ACTIVE
    await user.save()
    return MessageOut(message="Email verified")


@router.post("/forgot-password", response_model=MessageOut)
async def forgot_password(payload: ForgotPasswordDto) -> MessageOut:
    user = await User.get_or_none(email=payload.email.lower(), deleted_at=None)
    # Always return success — do not leak existence.
    if user:
        token = uuid4().hex
        user.reset_password_token = token
        user.reset_password_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await user.save()
        await send_password_reset_email(user.email, token)
    return MessageOut(message="If that email exists, a reset link has been sent")


@router.post("/reset-password", response_model=MessageOut)
async def reset_password(payload: ResetPasswordDto) -> MessageOut:
    user = await User.get_or_none(reset_password_token=payload.token)
    if (
        not user
        or not user.reset_password_expires
        or user.reset_password_expires < datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Token invalid or expired"
        )
    user.password = hash_password(payload.new_password)
    user.reset_password_token = None
    user.reset_password_expires = None
    await user.save()
    return MessageOut(message="Password reset successful")


@router.post("/refresh", response_model=TokensOut)
async def refresh_tokens(payload: RefreshTokenDto) -> TokensOut:
    decoded = decode_token(payload.refresh_token, refresh=True)
    user_id = decoded.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    user = await User.get_or_none(id=user_id, deleted_at=None)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    access, refresh = _issue_tokens(user)
    return TokensOut(access_token=access, refresh_token=refresh)


@router.get("/google")
async def google_login() -> RedirectResponse:
    """Kick off the Google OAuth flow. Uses Authlib's authorization URL."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth not configured"
        )
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    from urllib.parse import urlencode

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(code: str | None = None, state: str | None = None) -> RedirectResponse:
    """Exchange the code, find-or-create the user, redirect to frontend with tokens."""
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code")
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth not configured"
        )

    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        access_tok = token_resp.json()["access_token"]
        profile_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_tok}"},
        )
        profile_resp.raise_for_status()
        profile = profile_resp.json()

    email = profile.get("email", "").lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Google account has no email"
        )

    user = await User.get_or_none(email=email)
    is_new = user is None
    if not user:
        user = await User.create(
            email=email,
            first_name=profile.get("given_name"),
            last_name=profile.get("family_name"),
            profile_image=profile.get("picture"),
            auth_provider=AuthProvider.GOOGLE,
            provider_id=profile.get("id"),
            email_verified=True,
            status=UserStatus.ACTIVE,
        )
    else:
        user.last_login_at = datetime.now(timezone.utc)
        user.email_verified = True
        if user.status == UserStatus.PENDING_VERIFICATION:
            user.status = UserStatus.ACTIVE
        await user.save()

    if is_new:
        try:
            from app.common.services.subscription_service import ensure_subscription
            await ensure_subscription(user)
        except Exception as exc:
            logger.warning(f"trial provisioning failed for {user.email}: {exc}")

    access, refresh = _issue_tokens(user)
    redirect = f"{settings.FRONTEND_URL}/auth/callback?token={access}&refresh={refresh}"
    return RedirectResponse(redirect)

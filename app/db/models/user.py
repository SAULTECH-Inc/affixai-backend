from enum import Enum

from tortoise import fields
from tortoise.models import Model


class AuthProvider(str, Enum):
    LOCAL = "local"
    GOOGLE = "google"
    MICROSOFT = "microsoft"
    LINKEDIN = "linkedin"


class UserRole(str, Enum):
    USER = "user"
    ENTERPRISE_USER = "enterprise_user"
    ENTERPRISE_ADMIN = "enterprise_admin"
    SUPER_ADMIN = "super_admin"


class UserStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    PENDING_VERIFICATION = "pending_verification"


class User(Model):
    id = fields.UUIDField(pk=True)
    email = fields.CharField(max_length=255, unique=True)
    password = fields.CharField(max_length=255, null=True)

    auth_provider = fields.CharEnumField(AuthProvider, max_length=32, default=AuthProvider.LOCAL)
    provider_id = fields.CharField(max_length=255, null=True)

    first_name = fields.CharField(max_length=120, null=True)
    last_name = fields.CharField(max_length=120, null=True)
    phone_number = fields.CharField(max_length=40, null=True)
    profile_image = fields.CharField(max_length=512, null=True)

    role = fields.CharEnumField(UserRole, max_length=32, default=UserRole.USER)
    status = fields.CharEnumField(
        UserStatus, max_length=32, default=UserStatus.PENDING_VERIFICATION
    )

    email_verified = fields.BooleanField(default=False)
    verification_token = fields.CharField(max_length=128, null=True)
    reset_password_token = fields.CharField(max_length=128, null=True)
    reset_password_expires = fields.DatetimeField(null=True)

    enterprise_id = fields.UUIDField(null=True)

    last_login_at = fields.DatetimeField(null=True)
    last_login_ip = fields.CharField(max_length=64, null=True)

    # ISO 3166-1 alpha-2 country code (e.g. "NG", "US"). Used to route the
    # user to the right payment gateway and to show region-appropriate
    # pricing. Captured at signup via header detection or explicit picker;
    # editable later in Settings.
    country_code = fields.CharField(max_length=2, null=True)

    # Public referral code — shared via /referrals page, used as `?ref=`
    # on the landing page. Unique per user. Nullable because legacy users
    # don't have one until they visit the referrals page (or someone uses
    # their share link). See `referral_service.ensure_code`.
    referral_code = fields.CharField(max_length=24, unique=True, null=True)

    preferences = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "users"

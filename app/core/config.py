from pydantic_settings import BaseSettings, NoDecode
from pydantic import field_validator
from typing import List, Union
from typing_extensions import Annotated
import os


class Settings(BaseSettings):
    """Application settings"""

    # Application
    APP_NAME: str = "AI Document Signer"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    PORT: int = 8000

    # API Security
    API_SECRET_KEY: str
    INTERNAL_API_KEY: str
    ALLOWED_ORIGINS: Annotated[List[str], NoDecode] = ["http://localhost:3000", "http://localhost:3001"]

    # JWT
    JWT_SECRET: str
    JWT_REFRESH_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = 60
    JWT_REFRESH_EXPIRATION_DAYS: int = 30

    # Field-level encryption (AES-256-GCM)
    ENCRYPTION_KEY: str
    ENCRYPTION_ALGORITHM: str = "aes-256-gcm"

    # Database (Postgres via Tortoise ORM)
    # Tortoise expects: postgres://user:pass@host:port/db  (or sqlite:///path for dev)
    DATABASE_URL: str = "postgres://postgres:postgres@localhost:5432/ai_document_signer"

    # OAuth — Google
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    # SMTP / Email
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = "noreply@example.com"
    EMAIL_FROM_NAME: str = "AI Document Signer"
    FRONTEND_URL: str = "http://localhost:3001"

    # Where leads from the public /contact and /careers forms get forwarded.
    # Leave empty to fall back to the hardcoded defaults in leads.py
    # ("hello@affixai.com" / "careers@affixai.com").
    LEADS_CONTACT_TO: str = ""
    LEADS_CAREERS_TO: str = ""

    # ---- Billing ------------------------------------------------------------
    #
    # PAYMENT_PROVIDER selects which gateway runs checkout/portal/webhook calls.
    # Each provider's keys live in its own env block below; an empty block
    # means that provider is configured-but-disabled (the route will return
    # 503 if the active provider is unconfigured).
    PAYMENT_PROVIDER: str = "stripe"  # one of: stripe | paystack | flutterwave
    BILLING_CURRENCY: str = "USD"     # ISO 4217 — used in invoice rows + UI

    FREE_TRIAL_DAYS: int = 30
    FREE_TIER_ENABLED: bool = True

    # Comma-separated list of emails that are auto-promoted to SUPER_ADMIN on
    # register / login. Empty = nobody is admin (set via SQL instead).
    SUPER_ADMIN_EMAILS: str = ""

    # ---- Cloud storage integrations (Phase E2) -----------------------------
    # Each provider's OAuth client credentials live in their own env block.
    # An empty block means that provider is configured-but-disabled (the
    # connect route returns 503 for it).
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    DROPBOX_OAUTH_CLIENT_ID: str = ""
    DROPBOX_OAUTH_CLIENT_SECRET: str = ""
    MS_OAUTH_CLIENT_ID: str = ""
    MS_OAUTH_CLIENT_SECRET: str = ""

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_ENTERPRISE: str = ""

    # Paystack — plan codes look like "PLN_xxxx"; webhook secret = your
    # Paystack secret key (Paystack signs payloads with HMAC-SHA512).
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""
    PAYSTACK_PLAN_PRO: str = ""
    PAYSTACK_PLAN_ENTERPRISE: str = ""

    # Flutterwave — plan IDs are numeric; webhook hash is the value of
    # `secret_hash` you configure in the dashboard.
    FLUTTERWAVE_SECRET_KEY: str = ""
    FLUTTERWAVE_PUBLIC_KEY: str = ""
    FLUTTERWAVE_WEBHOOK_HASH: str = ""
    FLUTTERWAVE_PLAN_PRO: str = ""
    FLUTTERWAVE_PLAN_ENTERPRISE: str = ""
    
    # AWS S3
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_S3_BUCKET: str
    
    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 1
    CACHE_TTL: int = 3600
    
    # MongoDB
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "ai_document_signer"
    
    # OCR Settings
    OCR_ENGINE: str = "tesseract"
    OCR_LANGUAGE: str = "eng"
    OCR_DPI: int = 300
    
    # ML Model Settings
    USE_GPU: bool = False
    MODEL_CACHE_DIR: str = "./models"
    CONFIDENCE_THRESHOLD: float = 0.7
    
    # Document Processing
    MAX_FILE_SIZE_MB: int = 50
    SUPPORTED_FORMATS: Annotated[List[str], NoDecode] = ["pdf", "png", "jpg", "jpeg", "tiff"]
    MAX_PAGES_PER_DOCUMENT: int = 100
    
    # Field Detection
    ENABLE_NLP_FIELD_DETECTION: bool = True
    ENABLE_LAYOUT_ANALYSIS: bool = True
    FIELD_MATCHING_THRESHOLD: float = 0.8
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/fastapi.log"
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000
    
    # Worker Settings
    MAX_WORKERS: int = 4
    ASYNC_PROCESSING: bool = True
    
    # ---- Referral program ---------------------------------------------------
    # Rate is the share of net paid revenue a referrer earns; window is
    # how long after the referred user's first paid charge the commission
    # keeps accruing. Defaults: 20% recurring for 12 months.
    REFERRAL_COMMISSION_RATE: float = 0.20
    REFERRAL_COMMISSION_MONTHS: int = 12

    # ---- Observability ------------------------------------------------------
    # Both empty in dev. Set SENTRY_DSN in prod to enable error reporting.
    SENTRY_DSN: str = ""
    # Trace sample rate (0.0 – 1.0). 0.0 disables performance tracing while
    # still allowing error capture.
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0

    @field_validator("ALLOWED_ORIGINS", "SUPPORTED_FORMATS", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    class Config:
        env_file = ".env"
        case_sensitive = True


# ---- Startup validation -----------------------------------------------------
#
# In production we refuse to start with placeholder secrets. This catches
# the "we copied the dev .env to prod" class of mistake before it lets real
# user data get signed with a known-public JWT secret.

_KNOWN_INSECURE = {
    "changeme",
    "change-me",
    "secret",
    "your-secret-here",
    "placeholder",
    "dev-only",
    "",
}


def _validate_production(s: "Settings") -> list[str]:
    """Return a list of human-readable problems with the current settings.

    Only enforced when ENVIRONMENT in {"production", "staging"} — local dev
    is allowed to run with insecure-looking defaults.
    """
    if s.ENVIRONMENT.lower() not in ("production", "staging", "prod"):
        return []

    problems: list[str] = []

    def check_secret(name: str, value: str, min_len: int = 16) -> None:
        if not value or value.strip().lower() in _KNOWN_INSECURE:
            problems.append(f"{name} is empty or set to a placeholder value")
        elif len(value) < min_len:
            problems.append(
                f"{name} is too short ({len(value)} chars) — needs ≥{min_len}"
            )

    check_secret("JWT_SECRET", s.JWT_SECRET, 32)
    check_secret("JWT_REFRESH_SECRET", s.JWT_REFRESH_SECRET, 32)
    check_secret("API_SECRET_KEY", s.API_SECRET_KEY, 32)
    check_secret("INTERNAL_API_KEY", s.INTERNAL_API_KEY, 24)
    check_secret("ENCRYPTION_KEY", s.ENCRYPTION_KEY, 32)

    if s.JWT_SECRET == s.JWT_REFRESH_SECRET:
        problems.append("JWT_SECRET and JWT_REFRESH_SECRET must be different")

    if s.DEBUG:
        problems.append("DEBUG must be false in production")

    if s.SMTP_HOST in ("localhost", "127.0.0.1"):
        problems.append(
            "SMTP_HOST is localhost — outbound email won't work in production"
        )

    if s.EMAIL_FROM.endswith("@example.com"):
        problems.append("EMAIL_FROM is using the placeholder @example.com")

    # AWS bucket name is required for document storage to work end-to-end.
    if not s.AWS_S3_BUCKET:
        problems.append("AWS_S3_BUCKET is not set")

    # Payments — only check the active provider's keys. The other two are
    # legitimately empty if the org hasn't enabled them.
    active = s.PAYMENT_PROVIDER.lower()
    if active == "stripe" and not s.STRIPE_SECRET_KEY:
        problems.append("PAYMENT_PROVIDER=stripe but STRIPE_SECRET_KEY is empty")
    if active == "paystack" and not s.PAYSTACK_SECRET_KEY:
        problems.append("PAYMENT_PROVIDER=paystack but PAYSTACK_SECRET_KEY is empty")
    if active == "flutterwave" and not s.FLUTTERWAVE_SECRET_KEY:
        problems.append(
            "PAYMENT_PROVIDER=flutterwave but FLUTTERWAVE_SECRET_KEY is empty"
        )

    # ALLOWED_ORIGINS shouldn't include localhost in prod — that lets
    # local dev pages hit production with creds.
    if any("localhost" in o or "127.0.0.1" in o for o in s.ALLOWED_ORIGINS):
        problems.append(
            "ALLOWED_ORIGINS contains localhost — strip it in production"
        )

    return problems


settings = Settings()

# Fail loud on misconfig — before the app starts serving traffic.
_problems = _validate_production(settings)
if _problems:
    import sys

    bullets = "\n".join(f"  • {p}" for p in _problems)
    sys.stderr.write(
        f"\n❌ Refusing to start: production config has {len(_problems)} "
        f"problem(s):\n{bullets}\n\n"
        "Fix the .env (or unset ENVIRONMENT=production for local debugging).\n"
    )
    sys.exit(1)

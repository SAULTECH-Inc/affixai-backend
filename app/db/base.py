"""Tortoise ORM configuration.

The TORTOISE_ORM dict is the single source of truth referenced by:
  - app startup (register_tortoise)
  - aerich CLI (`aerich init -t app.db.base.TORTOISE_ORM`)
"""
import os as _os
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

from app.core.config import settings

# All app model modules must be listed so Tortoise discovers them.
# `aerich.models` is required by the aerich migration tool itself.
MODELS_MODULES = [
    "app.db.models.user",
    "app.db.models.audit_log",
    "app.db.models.data_vault",
    "app.db.models.custom_vault",
    "app.db.models.document",
    "app.db.models.document_participant",
    "app.db.models.document_comment",
    "app.db.models.document_signing_target",
    "app.db.models.webhook_endpoint",
    "app.db.models.cloud_connection",
    "app.db.models.vault_entry",
    "app.db.models.signature",
    "app.db.models.enterprise",
    "app.db.models.api_key",
    "app.db.models.subscription",
    "app.db.models.invoice",
    "app.db.models.stripe_event",
    "app.db.models.passport_photo",
    "app.db.models.lead",
    "app.db.models.referral",
]

# aerich.models is only needed when running the aerich migration CLI locally.
# It is not installed in the production runtime, so we include it conditionally.
try:
    import aerich  # noqa: F401
    MODELS_MODULES.append("aerich.models")
except ImportError:
    pass


def _connections_config() -> dict:
    """Build the Tortoise connections dict.

    On Vercel (serverless) each cold-start creates its own asyncpg pool.
    If every instance uses the default pool size (~10) a handful of concurrent
    invocations will exhaust the database's max_connections and start returning
    TooManyConnectionsError. We cap the pool at 2 connections per instance to
    keep the total low even under concurrent cold-starts.
    """
    url = settings.DATABASE_URL

    if not _os.environ.get("VERCEL"):
        # Local / non-serverless: use the DSN string directly, let Tortoise
        # apply its default pool sizing.
        return {"default": url}

    # Serverless path — parse the DSN and inject pool constraints.
    # Tortoise accepts either asyncpg or postgresql scheme; normalise to the
    # urllib-friendly "postgresql" before parsing.
    normalised = url.replace("postgres://", "postgresql://", 1)
    p = _urlparse(normalised)

    creds: dict = {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "user": p.username or "postgres",
        "password": p.password or "",
        "database": (p.path or "/postgres").lstrip("/"),
        "minsize": 1,
        "maxsize": 2,  # 2 connections per Vercel instance is enough
    }

    # Most hosted Postgres providers (Neon, Supabase, Railway) require SSL.
    if p.query:
        sslmode = _parse_qs(p.query).get("sslmode", [""])[0]
        creds["ssl"] = sslmode not in ("disable", "allow", "prefer", "")
    elif p.hostname and p.hostname not in ("localhost", "127.0.0.1", "::1"):
        creds["ssl"] = True  # assume SSL for any remote host

    return {
        "default": {
            "engine": "tortoise.backends.asyncpg",
            "credentials": creds,
        }
    }


TORTOISE_ORM = {
    "connections": _connections_config(),
    "apps": {
        "models": {
            "models": MODELS_MODULES,
            "default_connection": "default",
        },
    },
    "use_tz": True,
    "timezone": "UTC",
}

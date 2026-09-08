"""Tortoise ORM configuration.

The TORTOISE_ORM dict is the single source of truth referenced by:
  - app startup (register_tortoise)
  - aerich CLI (`aerich init -t app.db.base.TORTOISE_ORM`)
"""
import os as _os
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs, unquote as _unquote

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

    # SQLite (local dev): Tortoise handles the DSN string directly.
    if url.startswith("sqlite"):
        return {"default": url}

    # Always build an explicit credentials dict rather than handing Tortoise the
    # raw DSN. Tortoise forwards URL query params straight to asyncpg.connect(),
    # which has no `sslmode` kwarg (TypeError) and treats ssl="disable"/"false"
    # strings as SSL-*on* — so SSL can only be controlled reliably from here.
    # It also lets us URL-decode the password (urlparse does NOT), so a password
    # like "MadAdmin@123" written as "MadAdmin%40123" authenticates correctly.
    normalised = url.replace("postgres://", "postgresql://", 1)
    p = _urlparse(normalised)

    creds: dict = {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "user": _unquote(p.username) if p.username else "postgres",
        "password": _unquote(p.password) if p.password else "",
        "database": (p.path or "/postgres").lstrip("/"),
    }

    # SSL resolution → asyncpg's real values. A remote host with no explicit
    # sslmode defaults to "prefer": it encrypts when the server supports it
    # (Neon/Supabase/etc., without chain verification) and transparently falls
    # back to plaintext when the server has no SSL — so a self-hosted Postgres
    # without SSL still connects. localhost defaults to off.
    sslmode = (_parse_qs(p.query).get("sslmode", [""])[0] if p.query else "").lower()
    is_local = (p.hostname or "") in ("localhost", "127.0.0.1", "::1")
    if sslmode in ("disable", "disabled", "off", "false", "0", "none"):
        creds["ssl"] = False
    elif sslmode in ("verify-ca", "verify-full"):
        creds["ssl"] = True
    elif sslmode in ("require", "prefer", "allow"):
        creds["ssl"] = sslmode  # honor the exact mode
    elif not is_local:
        creds["ssl"] = "prefer"
    # localhost with no sslmode → leave ssl unset (no SSL)

    # On Vercel (serverless) each cold-start opens its own asyncpg pool; cap it
    # so concurrent cold-starts can't exhaust the database's max_connections.
    if _os.environ.get("VERCEL"):
        creds["minsize"] = 1
        creds["maxsize"] = 1

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

"""Tortoise ORM configuration.

The TORTOISE_ORM dict is the single source of truth referenced by:
  - app startup (register_tortoise)
  - aerich CLI (`aerich init -t app.db.base.TORTOISE_ORM`)
"""
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
    "aerich.models",
]

TORTOISE_ORM = {
    "connections": {"default": settings.DATABASE_URL},
    "apps": {
        "models": {
            "models": MODELS_MODULES,
            "default_connection": "default",
        },
    },
    "use_tz": True,
    "timezone": "UTC",
}

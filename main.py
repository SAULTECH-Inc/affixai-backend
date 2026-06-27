from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
import time

from app.core.config import settings
from app.api.routes import (
    admin,
    audit,
    auth,
    autofill,
    classify,
    cloud_storage,
    collaboration,
    custom_vault,
    workflow,
    data_vault,
    documents,
    enterprises,
    health,
    leads,
    ocr,
    passport_photos,
    public_api,
    referrals,
    signatures,
    subscriptions,
    users,
    vault_entries,
    webhooks,
)
from app.db.session import register_db

# Configure logging — skip file sink on Vercel (read-only fs); stdout is captured.
import os as _os
if not _os.environ.get("VERCEL"):
    logger.add(
        settings.LOG_FILE,
        rotation="500 MB",
        retention="10 days",
        level=settings.LOG_LEVEL,
    )

# Error reporting — Sentry. Only initialized when SENTRY_DSN is set so dev
# stays quiet. Must run BEFORE FastAPI() so the SDK can hook the ASGI app.
if settings.SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=settings.APP_VERSION,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # Loguru integration: warnings → breadcrumbs, errors → events.
        integrations=[
            FastApiIntegration(),
            AsyncioIntegration(),
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        # Strip PII from events automatically — vault values would otherwise
        # leak through frame locals.
        send_default_pii=False,
    )
    logger.info(f"Sentry enabled (env={settings.ENVIRONMENT})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    logger.info("🚀 Starting FastAPI AI Engine...")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"OCR Engine: {settings.OCR_ENGINE}")

    # Workflow scheduler — hourly reminders + expiration sweeps. Registered
    # AFTER Tortoise so DB models are usable inside the jobs.
    from app.common.services import workflow_scheduler
    workflow_scheduler.register_jobs()
    workflow_scheduler.start()

    yield

    # Cleanup
    logger.info("Shutting down FastAPI AI Engine...")
    workflow_scheduler.stop()


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered document intelligence engine for OCR, field extraction, and auto-fill",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    logger.info(f"{request.method} {request.url.path} - {process_time:.3f}s")
    return response

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "message": "Internal server error",
            "detail": str(exc) if settings.DEBUG else "An error occurred"
        }
    )

# Database (Tortoise ORM)
# Never auto-generate schemas on Vercel: every cold-start would open extra
# connections just to run CREATE TABLE IF NOT EXISTS, pushing the serverless
# pool over max_connections. Schema must be applied via aerich migrations
# before deploying; local dev can keep generate_schemas=True for convenience.
_generate_schemas = settings.DEBUG and not _os.environ.get("VERCEL")
register_db(app, generate_schemas=_generate_schemas)

# Include routers
app.include_router(health.router, prefix="/health", tags=["Health"])

# Internal AI services (X-API-Key protected, called by the app itself)
app.include_router(ocr.router, prefix="/api/ocr", tags=["OCR Processing"])
app.include_router(autofill.router, prefix="/api/autofill", tags=["Auto-Fill"])
app.include_router(classify.router, prefix="/api/classify", tags=["Classification"])

# Application API (JWT-authenticated)
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
app.include_router(data_vault.router, prefix="/api/v1/data-vault", tags=["Data Vault"])
app.include_router(custom_vault.router, prefix="/api/v1/data-vault/custom", tags=["Custom Vault"])
# Multi-entry vault sections (Education + Employment) — each row is a
# discrete entry (one degree, one job role) rather than a flat field set.
app.include_router(vault_entries.router, prefix="/api/v1/data-vault/entries", tags=["Vault Entries"])
app.include_router(documents.router, prefix="/api/v1/documents", tags=["Documents"])
# Collaboration endpoints mount UNDER /documents so the URLs read naturally:
# /documents/{id}/participants etc. Same prefix as documents → both routers
# need to coexist without colliding (paths are disjoint).
app.include_router(collaboration.router, prefix="/api/v1/documents", tags=["Collaboration"])
# Workflow layer (Phase D): owner-side controls under /documents/<id>/...
# and guest-side actions under /shared/<token>/...
app.include_router(workflow.owner_router, prefix="/api/v1/documents", tags=["Workflow"])
app.include_router(workflow.guest_router, prefix="/api/v1/shared", tags=["Workflow (Guest)"])
app.include_router(signatures.router, prefix="/api/v1/signatures", tags=["Signatures"])
app.include_router(passport_photos.router, prefix="/api/v1/passport-photos", tags=["Passport Photos"])
app.include_router(enterprises.router, prefix="/api/v1/enterprises", tags=["Enterprises"])
app.include_router(audit.router, prefix="/api/v1/audit", tags=["Audit"])
app.include_router(subscriptions.router, prefix="/api/v1/subscriptions", tags=["Subscriptions"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["Webhooks"])
# Public lead capture — open to the internet, throttled at the route level
# (in-memory IP rate-limit + honeypot). Used by the /contact and /careers
# marketing pages on the frontend.
app.include_router(leads.router, prefix="/api/v1/leads", tags=["Leads"])
# Referrals — per-user dashboard ("my code, my earnings, my referees").
# Admin-side list + CSV export lives under /api/v1/admin/referrals so it
# can share the require_super_admin dependency.
app.include_router(referrals.router, prefix="/api/v1/referrals", tags=["Referrals"])
# Cloud storage endpoints live under /integrations so the URL tree groups
# integration features cleanly. The export endpoint piggybacks on /documents.
app.include_router(cloud_storage.router, prefix="/api/v1/integrations", tags=["Cloud Storage"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["Admin"])

# Public enterprise API (X-API-Key per enterprise)
app.include_router(public_api.router, prefix="/api/v1/public", tags=["Public API"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )

"""Shared test fixtures.

We run tests against an in-memory SQLite database via Tortoise's test helpers,
not the dev Postgres. That way:
  * each test session starts with a clean schema
  * tests don't touch the developer's local data
  * CI doesn't need a Postgres service container for unit tests
    (integration tests that exercise Postgres-only features can still add
    one — none of the current tests need it)

Env vars are set BEFORE importing app modules so `Settings()` validation
sees them. Same trick as the original conftest.
"""
from __future__ import annotations

import os

# Env must be set before any `from app...` import.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("API_SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_KEY", "x" * 32)
os.environ.setdefault("JWT_SECRET", "x" * 32)
os.environ.setdefault("JWT_REFRESH_SECRET", "y" * 32)
os.environ.setdefault("ENCRYPTION_KEY", "z" * 32)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_S3_BUCKET", "test-bucket")
os.environ.setdefault("OCR_ENGINE", "tesseract")
# In-memory SQLite — fast, no setup, isolated per process.
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tortoise import Tortoise

from app.db.base import MODELS_MODULES


@pytest_asyncio.fixture(scope="function")
async def app():
    """A fresh FastAPI app per test, with Tortoise hooked up to SQLite.

    We initialize Tortoise BEFORE importing main.py so the lifespan hook
    doesn't try to start another Tortoise instance. Then we wire the same
    register_db behavior using already-running connection.
    """
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": MODELS_MODULES},
    )
    await Tortoise.generate_schemas()

    # Import main lazily so settings/env are already resolved.
    from fastapi import FastAPI
    from main import app as real_app  # noqa: F401 — registered routers

    # We can use the real app — Tortoise is already initialized so the
    # lifespan startup's register_tortoise call is a no-op duplicate.
    # But to keep tests hermetic we'll skip the lifespan + use the app
    # router stack directly.
    test_app = FastAPI()
    test_app.router.routes = real_app.routes  # share the route table

    yield test_app

    await Tortoise.close_connections()


@pytest_asyncio.fixture(scope="function")
async def client(app):
    """An httpx AsyncClient bound to the in-process app — no real network."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts with an empty IP rate-limit log so prior submissions
    in this session don't pollute the count. Yields control then no-ops
    on teardown — the deque is keyed per IP and shared across tests so
    we clear it eagerly."""
    from app.api.routes import leads as leads_module

    leads_module._rate_log.clear()
    yield
    leads_module._rate_log.clear()

"""Tests for the webhook dispatcher's retry logic.

The dispatcher used to do exactly one attempt; it now retries 3 times with
backoff on transient failures (network errors, 5xx, 408, 429). Permanent
failures (most 4xx) skip the retry entirely. This file pins those rules.

We monkey-patch httpx.AsyncClient and asyncio.sleep to avoid both real
network I/O and the multi-minute backoff schedule. The dispatcher's stats
updates against the real (test) DB so we get end-to-end coverage of the
WebhookEndpoint row bookkeeping.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.common.services import webhook_dispatcher as wd
from app.db.models.webhook_endpoint import WebhookEndpoint, WebhookEndpointStatus


# ---- Helpers ---------------------------------------------------------------


class FakeResponse:
    """Minimal httpx.Response stand-in — only the attributes _deliver reads."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeAsyncClient:
    """Replaces httpx.AsyncClient. Each instance returns the same response
    on every call so we can assert on attempt counts."""

    def __init__(self, response: FakeResponse | Exception):
        self._response = response
        self.call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self.call_count += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_httpx(monkeypatch, response):
    """Replace httpx.AsyncClient with a fake that returns `response`.

    Returns the fake instance so the test can inspect call_count.
    """
    fake = FakeAsyncClient(response)
    # httpx.AsyncClient is used as a context manager — we need to return
    # the SAME fake from every call to it, so call_count accumulates.
    def factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(wd.httpx, "AsyncClient", factory)
    return fake


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """asyncio.sleep is the only thing that makes these tests slow. Replace
    with a no-op so the 3-attempt schedule completes in milliseconds."""

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(wd.asyncio, "sleep", fast_sleep)


async def _make_endpoint() -> WebhookEndpoint:
    """Create a fresh WebhookEndpoint row to dispatch against."""
    return await WebhookEndpoint.create(
        user_id=uuid4(),
        url="https://example.com/hook",
        secret="s3cret",
        # Endpoint subscribes to all events — irrelevant here since we call
        # _deliver directly.
        events=["*"],
        status=WebhookEndpointStatus.ACTIVE,
    )


# ---- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_first_attempt(monkeypatch, client):
    """2xx on the first try → no retries, stats reflect one success."""
    fake = _patch_httpx(monkeypatch, FakeResponse(200, "ok"))
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_1"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1
    assert ep.delivery_successes == 1
    assert ep.delivery_failures == 0
    assert ep.consecutive_failures == 0
    assert ep.last_failure_reason is None


@pytest.mark.asyncio
async def test_retries_on_5xx(monkeypatch, client):
    """5xx is transient — we exhaust retries (4 total attempts)."""
    fake = _patch_httpx(monkeypatch, FakeResponse(503, "down for maintenance"))
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_2"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1 + len(wd._RETRY_DELAYS)  # 4 attempts
    assert ep.delivery_failures == 1
    assert ep.delivery_successes == 0
    assert ep.consecutive_failures == 1
    assert "503" in (ep.last_failure_reason or "")
    assert "after 4 attempt" in (ep.last_failure_reason or "")


@pytest.mark.asyncio
async def test_no_retry_on_4xx(monkeypatch, client):
    """A 400 from the receiver means 'this payload is broken' — retries
    won't help. We bail after the first attempt."""
    fake = _patch_httpx(monkeypatch, FakeResponse(400, "bad request"))
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_3"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1, "should not retry on permanent 4xx"
    assert ep.delivery_failures == 1
    assert "400" in (ep.last_failure_reason or "")


@pytest.mark.asyncio
async def test_retries_on_429(monkeypatch, client):
    """429 = receiver is rate-limiting us. That's transient; retry."""
    fake = _patch_httpx(monkeypatch, FakeResponse(429, "slow down"))
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_4"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1 + len(wd._RETRY_DELAYS)


@pytest.mark.asyncio
async def test_retries_on_408(monkeypatch, client):
    """408 = receiver timed out. Same story as 429."""
    fake = _patch_httpx(monkeypatch, FakeResponse(408))
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_5"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1 + len(wd._RETRY_DELAYS)


@pytest.mark.asyncio
async def test_retries_on_network_error(monkeypatch, client):
    """Connection failures get the full retry treatment."""
    fake = _patch_httpx(
        monkeypatch, wd.httpx.ConnectError("connection refused")
    )
    ep = await _make_endpoint()

    await wd._deliver(ep, b'{"id":"evt_6"}', "document.signed")
    await ep.refresh_from_db()

    assert fake.call_count == 1 + len(wd._RETRY_DELAYS)
    assert "Network error" in (ep.last_failure_reason or "")


@pytest.mark.asyncio
async def test_auto_disable_after_max_consecutive_failures(monkeypatch, client):
    """After N back-to-back failed deliveries the endpoint flips to DISABLED."""
    _patch_httpx(monkeypatch, FakeResponse(500))
    ep = await _make_endpoint()
    # Start one short of the disable threshold so a single failed delivery
    # tips us over.
    ep.consecutive_failures = wd._MAX_CONSECUTIVE_FAILURES - 1
    await ep.save()

    await wd._deliver(ep, b'{"id":"evt_7"}', "document.signed")
    await ep.refresh_from_db()

    assert ep.status == WebhookEndpointStatus.DISABLED
    assert ep.consecutive_failures >= wd._MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_success_resets_consecutive_failures(monkeypatch, client):
    """A successful delivery zeroes the failure streak."""
    fake = _patch_httpx(monkeypatch, FakeResponse(200))
    ep = await _make_endpoint()
    ep.consecutive_failures = 5
    await ep.save()

    await wd._deliver(ep, b'{"id":"evt_8"}', "document.signed")
    await ep.refresh_from_db()

    assert ep.consecutive_failures == 0
    assert ep.last_failure_reason is None

"""Pins the hand-rolled Drive v3 multipart upload.

google_drive.py used to delegate to google-api-python-client, which built
this request for us. It now assembles the `multipart/related` body itself
(that library bundles ~100MB of static discovery documents and broke the
Vercel 500MB function limit), so the wire format is our responsibility and
worth locking down — there is no way to exercise it against real Google
credentials in CI.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.common.services.cloud_storage.base import CloudProviderError
from app.common.services.cloud_storage.google_drive import (
    _UPLOAD_URL,
    GoogleDriveProvider,
)
from app.core.encryption import encrypt


class _FakeConnection:
    """Stands in for a CloudConnection row — the provider only reads the
    encrypted token and `expires_at` (None short-circuits the refresh)."""

    def __init__(self) -> None:
        self.encrypted_access_token = encrypt("ya29.fake-access-token")
        self.encrypted_refresh_token = None
        self.expires_at = None


def _patch_transport(monkeypatch, handler):
    """Route every httpx.AsyncClient request through `handler`."""
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)


def _parse_related(body: bytes, boundary: str) -> tuple[dict, bytes]:
    """Split a multipart/related body into (metadata_json, file_bytes)."""
    parts = body.split(f"--{boundary}".encode())
    # parts[0] is empty, parts[-1] is the trailing "--\r\n"
    meta_part, file_part = parts[1], parts[2]
    meta_json = json.loads(meta_part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
    file_body = file_part.split(b"\r\n\r\n", 1)[1]
    if file_body.endswith(b"\r\n"):
        file_body = file_body[:-2]
    return meta_json, file_body


@pytest.mark.asyncio
async def test_upload_builds_multipart_related_request(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(
            200,
            json={"id": "drive-file-1", "name": "signed.pdf",
                  "webViewLink": "https://drive.google.com/file/d/drive-file-1/view"},
        )

    _patch_transport(monkeypatch, handler)

    result = await GoogleDriveProvider().upload(
        _FakeConnection(),
        file_bytes=b"%PDF-1.7 payload",
        file_name="signed.pdf",
        mime_type="application/pdf",
        folder_id="folder-abc",
    )

    assert result.file_id == "drive-file-1"
    assert result.file_name == "signed.pdf"
    assert result.view_url.endswith("/view")

    assert captured["url"].startswith(_UPLOAD_URL)
    assert "uploadType=multipart" in captured["url"]
    # Without `fields`, Drive omits webViewLink from the response entirely.
    assert "fields=id" in captured["url"]

    ctype = captured["headers"]["content-type"]
    assert ctype.startswith("multipart/related; boundary=")
    assert captured["headers"]["authorization"] == "Bearer ya29.fake-access-token"

    boundary = ctype.split("boundary=", 1)[1]
    meta, payload = _parse_related(captured["content"], boundary)
    assert meta == {"name": "signed.pdf", "parents": ["folder-abc"]}
    assert payload == b"%PDF-1.7 payload"


@pytest.mark.asyncio
async def test_upload_omits_parents_when_no_folder(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        captured["ctype"] = request.headers["content-type"]
        return httpx.Response(200, json={"id": "x", "name": "a.txt"})

    _patch_transport(monkeypatch, handler)

    res = await GoogleDriveProvider().upload(
        _FakeConnection(), file_bytes=b"hi", file_name="a.txt", mime_type="text/plain",
    )
    # Drive drops the file in the user's root when `parents` is absent —
    # sending `parents: [None]` would be a 400.
    boundary = captured["ctype"].split("boundary=", 1)[1]
    meta, payload = _parse_related(captured["content"], boundary)
    assert meta == {"name": "a.txt"}
    assert payload == b"hi"
    assert res.view_url is None


@pytest.mark.asyncio
async def test_upload_raises_on_http_error(monkeypatch):
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(403, text='{"error": "insufficientPermissions"}'),
    )
    with pytest.raises(CloudProviderError) as exc:
        await GoogleDriveProvider().upload(
            _FakeConnection(), file_bytes=b"x", file_name="x.pdf",
            mime_type="application/pdf",
        )
    assert "403" in str(exc.value)


@pytest.mark.asyncio
async def test_upload_raises_on_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(CloudProviderError) as exc:
        await GoogleDriveProvider().upload(
            _FakeConnection(), file_bytes=b"x", file_name="x.pdf",
            mime_type="application/pdf",
        )
    assert "network error" in str(exc.value)


def test_no_google_client_libraries_imported():
    """The whole point of the rewrite: importing the provider must not drag
    in google-api-python-client / google-auth."""
    import subprocess
    import sys

    code = (
        "import app.common.services.cloud_storage.google_drive as m, sys;"
        "bad=[n for n in sys.modules if n.split('.')[0] in "
        "{'googleapiclient','google','google_auth_oauthlib'}];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"google libs pulled in: {out.stdout}"

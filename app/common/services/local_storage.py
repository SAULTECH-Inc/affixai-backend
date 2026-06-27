"""Storage facade for uploaded files.

Routes to Cloudinary when CLOUDINARY_API_KEY is set, otherwise falls back
to the local filesystem (dev / self-hosted). All callers import from here
so the switch is transparent.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.responses import Response

# Vercel's /var/task filesystem is read-only; only /tmp is writable.
UPLOADS_ROOT = Path("/tmp/uploads") if os.environ.get("VERCEL") else Path("uploads")


def _use_cloudinary() -> bool:
    from app.core.config import settings
    return bool(settings.CLOUDINARY_API_KEY)


def _ensure_dir(folder: str) -> Path:
    target = UPLOADS_ROOT / folder
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Core storage API
# ---------------------------------------------------------------------------

def save_bytes(data: bytes, original_file_name: str, folder: str = "documents") -> dict[str, str]:
    """Persist bytes. Returns {key, url} — url is either https:// or local://."""
    if _use_cloudinary():
        from app.common.services import cloudinary_storage
        return cloudinary_storage.save_bytes(data, original_file_name, folder)

    extension = original_file_name.rsplit(".", 1)[-1] if "." in original_file_name else "bin"
    extension = "".join(c for c in extension if c.isalnum())[:8] or "bin"
    key = f"{folder}/{uuid.uuid4()}.{extension}"
    path = UPLOADS_ROOT / key
    _ensure_dir(folder)
    path.write_bytes(data)
    return {"key": key, "url": f"local://{key}", "path": str(path)}


def read_bytes(key: str) -> bytes:
    return (UPLOADS_ROOT / key).read_bytes()


def delete(key: str) -> None:
    if _use_cloudinary():
        from app.common.services import cloudinary_storage
        cloudinary_storage.delete(key)
        return
    path = UPLOADS_ROOT / key
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Async helpers for routes
# ---------------------------------------------------------------------------

async def fetch_file_bytes(url: str) -> bytes:
    """Fetch file bytes from a local:// pseudo-URL or a remote https:// URL."""
    if url.startswith("https://") or url.startswith("http://"):
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    # Local — strip local:// prefix or treat as bare relative key
    key = url.replace("local://", "", 1)
    return (UPLOADS_ROOT / key).read_bytes()


def serve_file(url: str, media_type: str = "application/octet-stream", filename: str = "file") -> "Response":
    """Return the right Response type: RedirectResponse for remote, FileResponse for local."""
    from fastapi import HTTPException, status as http_status
    from fastapi.responses import FileResponse, RedirectResponse

    if url.startswith("https://") or url.startswith("http://"):
        return RedirectResponse(url)

    key = url.replace("local://", "", 1)
    path = UPLOADS_ROOT / key
    if not path.exists():
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="File missing on disk")
    return FileResponse(str(path), media_type=media_type, filename=filename)

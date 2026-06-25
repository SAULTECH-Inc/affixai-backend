"""Dev-mode local filesystem storage for uploaded documents.

Saves files to `./uploads/<folder>/<uuid>.<ext>` relative to the project root.
Returns a `local://uploads/...` pseudo-URL so we can persist a `file_url` on
Document rows without depending on S3 being configured. In production this is
swappable for `app.common.services.s3_service`.
"""
from __future__ import annotations

import uuid
from pathlib import Path

UPLOADS_ROOT = Path("uploads")


def _ensure_dir(folder: str) -> Path:
    target = UPLOADS_ROOT / folder
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_bytes(data: bytes, original_file_name: str, folder: str = "documents") -> dict[str, str]:
    """Persist bytes to a unique local path. Returns {key, url} mirroring the S3 helper."""
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
    path = UPLOADS_ROOT / key
    if path.exists():
        path.unlink()

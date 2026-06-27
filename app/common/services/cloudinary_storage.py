"""Cloudinary storage backend.

Drop-in replacement for local_storage — same save_bytes / delete interface.
Activated automatically when CLOUDINARY_API_KEY is set in config.
"""
from __future__ import annotations

import cloudinary
import cloudinary.uploader


def _cfg() -> None:
    from app.core.config import settings
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )


def save_bytes(data: bytes, original_file_name: str, folder: str = "documents") -> dict[str, str]:
    """Upload bytes to Cloudinary. Returns {key, url} matching local_storage's interface."""
    _cfg()
    result = cloudinary.uploader.upload(
        data,
        folder=f"affixai/{folder}",
        resource_type="auto",
        use_filename=False,
    )
    return {"key": result["public_id"], "url": result["secure_url"]}


def delete(public_id: str) -> None:
    _cfg()
    cloudinary.uploader.destroy(public_id, resource_type="auto")

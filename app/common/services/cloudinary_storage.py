"""Cloudinary storage backend.

Drop-in replacement for local_storage — same save_bytes / delete interface.
Activated automatically when CLOUDINARY_API_KEY is set in config.
"""
from __future__ import annotations

import re

import cloudinary
import cloudinary.uploader
import cloudinary.utils


def _cfg() -> None:
    from app.core.config import settings
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )


def _resource_type_for(filename: str) -> str:
    """PDFs must be stored as 'raw' so Cloudinary delivers them as-is without
    going through its image transformation pipeline — which requires signed
    URLs when Strict Transformations is enabled on the account.
    Everything else uses 'auto' so images are recognised and optimised."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "raw" if ext == "pdf" else "auto"


def save_bytes(data: bytes, original_file_name: str, folder: str = "documents") -> dict[str, str]:
    """Upload bytes to Cloudinary. Returns {key, url} matching local_storage's interface."""
    _cfg()
    resource_type = _resource_type_for(original_file_name)
    result = cloudinary.uploader.upload(
        data,
        folder=f"affixai/{folder}",
        resource_type=resource_type,
        use_filename=False,
    )
    return {"key": result["public_id"], "url": result["secure_url"]}


def signed_download_url(url: str, filename: str = "file") -> str:
    """Return a server-signed Cloudinary URL the browser can download directly.

    Handles both /raw/upload/ and /image/upload/ URLs. Signing makes the URL
    work even when the account has Strict Transformations enabled, and forces
    the browser to download rather than render the file inline.
    """
    _cfg()

    # Parse resource_type, public_id, and extension from the stored URL.
    m = re.search(
        r"res\.cloudinary\.com/[^/]+/(image|raw|video)/upload/(?:v\d+/)?(.+?)(?:\.([^./]+))?$",
        url,
    )
    if not m:
        return url  # not a recognisable Cloudinary URL — return as-is

    resource_type = m.group(1)
    public_id = m.group(2)
    fmt = m.group(3) or ""

    signed, _ = cloudinary.utils.cloudinary_url(
        public_id,
        resource_type=resource_type,
        type="upload",
        sign_url=True,
        format=fmt,
        attachment=filename,
    )
    return signed


def delete(public_id: str) -> None:
    _cfg()
    cloudinary.uploader.destroy(public_id, resource_type="auto")

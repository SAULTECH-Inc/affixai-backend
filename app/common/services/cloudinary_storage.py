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
    ext = original_file_name.rsplit(".", 1)[-1].lower() if "." in original_file_name else ""
    resource_type = _resource_type_for(original_file_name)

    upload_opts: dict = {
        "folder": f"affixai/{folder}",
        "resource_type": resource_type,
        "use_filename": False,
    }
    # For raw (PDF) uploads Cloudinary does not automatically append the file
    # extension to the public_id.  Without it the stored URL has no extension,
    # which later breaks signed URL generation (trailing dot in the URL).
    if resource_type == "raw" and ext:
        upload_opts["format"] = ext

    result = cloudinary.uploader.upload(data, **upload_opts)
    return {"key": result["public_id"], "url": result["secure_url"]}


# URL regex — captures resource_type, optional version, public_id, optional extension.
_CL_URL_RE = re.compile(
    r"res\.cloudinary\.com/[^/]+/(image|raw|video)/upload/"
    r"(?:v(\d+)/)?"      # optional version group (without 'v' prefix)
    r"(.+?)"             # public_id (non-greedy)
    r"(?:\.([^./]+))?$"  # optional extension
)


def signed_download_url(url: str, filename: str = "file") -> str:
    """Return a server-signed Cloudinary URL the browser can download directly.

    Handles both /raw/upload/ and /image/upload/ URLs. Signing makes the URL
    work even when the account has Strict Transformations enabled, and forces
    the browser to download rather than render the file inline.
    """
    _cfg()

    m = _CL_URL_RE.search(url)
    if not m:
        return url  # not a recognisable Cloudinary URL — return as-is

    resource_type = m.group(1)
    version = m.group(2)    # digit string e.g. "1782583566", or None
    public_id = m.group(3)
    fmt = m.group(4) or ""  # e.g. "pdf", or "" when URL has no extension

    opts: dict = {
        "resource_type": resource_type,
        "type": "upload",
        "sign_url": True,
        "attachment": filename,
    }
    # Pass the original version so the signed URL keeps the correct path.
    # Without it the SDK defaults to v1, which resolves to a different (wrong)
    # resource version and returns 404.
    if version:
        opts["version"] = version
    # Only pass format when it's non-empty.  An empty string causes the SDK to
    # append a bare '.' to the public_id (e.g. "file.") which returns 404.
    if fmt:
        opts["format"] = fmt

    signed, _ = cloudinary.utils.cloudinary_url(public_id, **opts)
    return signed


def delete(public_id: str) -> None:
    _cfg()
    cloudinary.uploader.destroy(public_id, resource_type="auto")

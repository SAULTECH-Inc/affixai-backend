"""Cloudinary storage backend.

Drop-in replacement for local_storage — same save_bytes / delete interface.
Activated automatically when CLOUDINARY_API_KEY is set in config.
"""
from __future__ import annotations

import re
import time

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
    """Return a time-limited, API-authenticated Cloudinary download URL.

    Uses private_download_url() which routes through Cloudinary's API server
    (not the CDN) and embeds the API key + HMAC signature + expiry directly in
    the query string.  This bypasses ALL CDN-level delivery restrictions —
    Strict Transformations, account security settings, restricted delivery —
    because the request is authenticated at the API layer, not the CDN layer.

    The generated URL is valid for 1 hour and can be followed by any client
    without additional credentials.
    """
    _cfg()

    m = _CL_URL_RE.search(url)
    if not m:
        return url  # not a recognisable Cloudinary URL — return as-is

    resource_type = m.group(1)
    # group(2) is the version — not needed for private_download_url
    public_id = m.group(3)
    fmt = m.group(4) or ""  # e.g. "pdf"

    if resource_type == "raw":
        # Cloudinary raw resources: the file extension is part of the public_id
        # itself (e.g. "affixai/to-sign/abc.pdf"), not a separate format field.
        # Calling private_download_url with public_id="abc" + format="pdf" returns
        # 404 because Cloudinary stores it as public_id="abc.pdf".
        # Fix: reconstruct the full public_id with the extension and pass format="".
        full_public_id = f"{public_id}.{fmt}" if fmt else public_id
        return cloudinary.utils.private_download_url(
            full_public_id,
            "",  # format is part of public_id for raw resources
            resource_type="raw",
            type="upload",
            expires_at=int(time.time()) + 3600,
            attachment=filename,
        )

    # image / video: public_id does NOT include the extension; format is separate.
    return cloudinary.utils.private_download_url(
        public_id,
        fmt,
        resource_type=resource_type,
        type="upload",
        expires_at=int(time.time()) + 3600,
        attachment=filename,
    )


def delete(public_id: str) -> None:
    _cfg()
    cloudinary.uploader.destroy(public_id, resource_type="auto")

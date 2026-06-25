"""Country detection from request context.

Order of precedence (most to least reliable):
  1. Cloudflare's `CF-IPCountry` header (when proxied through CF)
  2. Vercel's `X-Vercel-IP-Country` header (when deployed on Vercel)
  3. Generic `X-Country-Code` header (custom edge config)
  4. None — caller decides the fallback (usually "show a picker")

We intentionally do NOT do server-side IP lookups (Maxmind / ipapi) here:
that would add a network hop on every signup. The CDN-edge headers are
free and accurate. If you deploy without a CDN, expose the country
picker on the registration page so the user supplies it themselves.
"""
from __future__ import annotations

from fastapi import Request


def country_from_request(request: Request) -> str | None:
    """Return a 2-letter ISO country code, or None if undetectable."""
    for header in (
        "cf-ipcountry",          # Cloudflare
        "x-vercel-ip-country",   # Vercel
        "x-country-code",        # generic edge passthrough
    ):
        v = request.headers.get(header)
        if v and len(v) == 2 and v.isalpha() and v != "XX":
            # "XX" / "T1" are Cloudflare's "unknown" / "Tor" sentinels —
            # ignore them and let the caller fall back.
            return v.upper()
    return None

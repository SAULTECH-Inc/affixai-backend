"""The Google OAuth callback must report *why* Google refused."""
import httpx, pytest
from app.api.routes.auth import _google_error

def test_invalid_grant_is_named():
    r = httpx.Response(400, json={"error": "invalid_grant",
                                  "error_description": "Bad Request"})
    assert "invalid_grant" in _google_error(r)
    assert "Bad Request" in _google_error(r)

def test_redirect_uri_mismatch_is_named():
    r = httpx.Response(400, json={"error": "redirect_uri_mismatch"})
    assert _google_error(r) == "redirect_uri_mismatch"

def test_non_json_body_falls_back_to_text():
    r = httpx.Response(400, text="<html>gateway blew up</html>")
    assert "gateway blew up" in _google_error(r)

def test_empty_body_is_still_described():
    assert _google_error(httpx.Response(400, text="")) == "no response body"

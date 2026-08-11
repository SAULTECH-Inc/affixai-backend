"""Phase 3: ready-made templates.

Covers the catalogue's own integrity (a template whose body references a
placeholder it never declares would ship "__________" into someone's contract),
value precedence, escaping, and the routes.
"""
from __future__ import annotations

import uuid

import pytest

from app.common.services import document_templates as dt
from app.core import security
from app.db.models.document import Document, DocumentStatus
from app.db.models.user import User, UserRole, UserStatus


@pytest.fixture
async def authed_user(app):
    u = await User.create(
        email=f"tmpl-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy", first_name="Tmpl", last_name="Tester",
        role=UserRole.USER, status=UserStatus.ACTIVE,
    )
    return u, security.create_access_token({"sub": str(u.id)})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _deligature(s: str) -> str:
    """Expand the typographic ligatures the serif face uses.

    PDF text extraction returns these as single code points, so "Confirmation"
    comes back as "Conﬁrmation" (U+FB01). Not a rendering fault — the document is
    correct — but it does mean naive text search over generated documents misses
    words containing fi/fl/ff.
    """
    for lig, plain in (("ﬀ", "ff"), ("ﬁ", "fi"), ("ﬂ", "fl"),
                       ("ﬃ", "ffi"), ("ﬄ", "ffl")):
        s = s.replace(lig, plain)
    return s


# ---- Catalogue integrity ---------------------------------------------------


def test_catalogue_is_not_empty():
    assert dt.TEMPLATES
    assert len(dt.template_catalogue()) == len(dt.TEMPLATES)


@pytest.mark.parametrize("slug", sorted(dt.TEMPLATES))
def test_every_body_placeholder_is_declared(slug):
    """The important guard on this catalogue.

    A marker in the body with no matching Placeholder can never be filled, so it
    would render as a blank rule in a real contract with nothing in the form to
    complete it. `offer_expiry_clause` is a composed clause handled in fill_body
    rather than a user-facing field.
    """
    spec = dt.TEMPLATES[slug]
    used = set(dt._PLACEHOLDER_RE.findall(spec.body_html))
    declared = spec.placeholder_keys() | {"offer_expiry_clause"}
    assert not (used - declared), f"{slug}: undeclared {sorted(used - declared)}"


@pytest.mark.parametrize("slug", sorted(dt.TEMPLATES))
def test_every_declared_placeholder_is_used(slug):
    """The inverse: a field the form asks for but the document never shows is
    wasted effort for the user."""
    spec = dt.TEMPLATES[slug]
    used = set(dt._PLACEHOLDER_RE.findall(spec.body_html))
    # offer_expiry_date feeds the composed offer_expiry_clause.
    exempt = {"offer_expiry_date"}
    unused = spec.placeholder_keys() - used - exempt
    assert not unused, f"{slug}: declared but unused {sorted(unused)}"


@pytest.mark.parametrize("slug", sorted(dt.TEMPLATES))
def test_slugs_and_titles_are_sane(slug):
    spec = dt.TEMPLATES[slug]
    assert spec.slug == slug
    assert spec.title and spec.summary and spec.category
    assert "<h1>" in spec.body_html


def test_no_template_hardcodes_a_jurisdiction():
    """Governing law must be a placeholder — a baked-in jurisdiction is wrong
    everywhere except one country."""
    for slug, spec in dt.TEMPLATES.items():
        body = spec.body_html
        if "governing" in body.lower() or "law of" in body.lower():
            assert "{{governing_law}}" in body, f"{slug} mentions law without the placeholder"


def test_get_template_unknown_lists_options():
    with pytest.raises(ValueError) as exc:
        dt.get_template("does-not-exist")
    assert "offer-letter" in str(exc.value)


# ---- Value precedence ------------------------------------------------------


def test_supplied_value_beats_vault_and_default():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(
        spec, {"employee_name": "Typed Name"}, {"full_legal_name": "Vault Name"},
    )
    assert values["employee_name"] == "Typed Name"


def test_vault_fills_when_not_supplied():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(spec, {}, {"full_legal_name": "Vault Name"})
    assert values["employee_name"] == "Vault Name"


def test_default_applies_when_neither_supplied_nor_in_vault():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(spec, {}, {})
    assert values["company_signatory_title"] == "Director"


def test_document_date_defaults_to_today():
    from datetime import date

    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(spec, {}, {})
    assert values["agreement_date"] == date.today().strftime("%d %B %Y")


def test_other_date_fields_do_not_default_to_today():
    """Only the document's own date may default. An offer expiry that quietly
    becomes today is an offer that expires the moment it's written, and a start
    date silently set to today hides a field the user must actually choose."""
    spec = dt.get_template("offer-letter")
    values, missing = dt.resolve_values(spec, {}, {})
    assert values["offer_expiry_date"] == ""
    assert values["start_date"] == ""
    assert "Start date" in missing  # required, so it must be surfaced


def test_probation_end_date_is_not_silently_today():
    spec = dt.get_template("probation-confirmation")
    values, missing = dt.resolve_values(spec, {}, {})
    assert values["probation_end_date"] == ""
    assert "Probation ended on" in missing


def test_missing_required_are_reported_by_label():
    spec = dt.get_template("offer-letter")
    _, missing = dt.resolve_values(spec, {}, {})
    assert "Company name" in missing
    # Optional ones with defaults must not be reported.
    assert "Notice period" not in missing


def test_blank_supplied_value_is_treated_as_absent():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(
        spec, {"employee_name": "   "}, {"full_legal_name": "Vault Name"},
    )
    assert values["employee_name"] == "Vault Name"


# ---- Filling and escaping --------------------------------------------------


def test_fill_escapes_html_in_values():
    """Values are user input and the body is parsed as HTML, so a pasted tag
    must not become markup in the rendered contract."""
    spec = dt.get_template("employee-nda")
    values, _ = dt.resolve_values(
        spec, {"company_name": "<b>Evil</b> & Co", "employee_name": "A",
               "company_address": "x", "company_signatory": "y",
               "governing_law": "Nigeria"}, {},
    )
    body = dt.fill_body(spec, values)
    assert "<b>Evil</b>" not in body
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in body


def test_fill_preserves_multiline_input_as_breaks():
    spec = dt.get_template("employee-nda")
    values, _ = dt.resolve_values(spec, {"company_address": "Line one\nLine two"}, {})
    body = dt.fill_body(spec, values)
    assert "Line one<br/>Line two" in body


def test_fill_renders_blank_rule_for_unfilled_optional():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(spec, {}, {})
    body = dt.fill_body(spec, values)
    assert dt._BLANK in body
    assert "{{" not in body  # no leftover markers


def test_composed_offer_expiry_clause_appears_only_when_dated():
    """An optional date must not leave "return a copy of this letter by ______"
    in the document — hence the composed clause rather than a bare marker."""
    spec = dt.get_template("offer-letter")
    with_date, _ = dt.resolve_values(spec, {"offer_expiry_date": "1 Sept 2026"}, {})
    assert "by 1 Sept 2026" in dt.fill_body(spec, with_date)

    without, _ = dt.resolve_values(spec, {}, {})
    body = dt.fill_body(spec, without)
    assert f"by {dt._BLANK}" not in body
    assert "{{offer_expiry_clause}}" not in body


# ---- Rendering -------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(dt.TEMPLATES))
def test_every_template_renders_to_a_valid_pdf(slug):
    """Renders each template with only its defaults, which is the worst case for
    layout — every unfilled field becomes a blank rule."""
    import fitz

    spec = dt.get_template(slug)
    values, _ = dt.resolve_values(spec, {}, {})
    pdf = dt.render_template_pdf(spec, values)
    assert pdf[:4] == b"%PDF"
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert doc.page_count >= 1
        text = "\n".join(p.get_text() for p in doc)
        assert "{{" not in text
        # The title should survive into the rendered text. Normalise ligatures
        # first: the serif face renders "fi"/"fl" as single glyphs, so extracted
        # text contains U+FB01 and "Confirmation" comes back as "Conﬁrmation".
        # Worth knowing beyond this test — naive text search over generated
        # documents will miss those words.
        assert _deligature(spec.title.split()[0]).lower() in _deligature(text).lower()
    finally:
        doc.close()


def test_long_input_paginates_rather_than_clipping():
    import fitz

    spec = dt.get_template("contractor-agreement")
    values, _ = dt.resolve_values(
        spec, {"services": "Detailed scope paragraph. " * 400}, {},
    )
    pdf = dt.render_template_pdf(spec, values)
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert doc.page_count >= 2
        text = "\n".join(p.get_text() for p in doc)
        # The clause *after* the long one must still be present, i.e. content
        # flowed on instead of being cut off.
        assert "Governing law" in text
    finally:
        doc.close()


def test_render_rejects_unknown_page_size():
    spec = dt.get_template("offer-letter")
    values, _ = dt.resolve_values(spec, {}, {})
    with pytest.raises(ValueError):
        dt.render_template_pdf(spec, values, page_size="scroll")


# ---- Routes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_templates_require_auth(client):
    assert (await client.get("/api/v1/templates")).status_code == 401


@pytest.mark.asyncio
async def test_list_templates_includes_disclaimer(client, authed_user):
    _, token = authed_user
    r = await client.get("/api/v1/templates", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body["templates"]) == len(dt.TEMPLATES)
    assert "not legal advice" in body["disclaimer"]


@pytest.mark.asyncio
async def test_template_detail_prefills_from_vault(client, authed_user, monkeypatch):
    u, token = authed_user
    monkeypatch.setattr(
        "app.common.services.auto_affix.get_user_vault_dict",
        lambda user_id: _async_return({"full_legal_name": "Ada Lovelace"}),
    )
    r = await client.get("/api/v1/templates/offer-letter", headers=_auth(token))
    assert r.status_code == 200
    by_key = {p["key"]: p for p in r.json()["placeholders"]}
    assert by_key["employee_name"]["value"] == "Ada Lovelace"
    assert by_key["employee_name"]["prefilled_from_vault"] is True
    assert by_key["company_name"]["prefilled_from_vault"] is False


def _async_return(value):
    async def _inner(*a, **k):
        return value
    return _inner()


@pytest.mark.asyncio
async def test_template_detail_unknown_slug_404(client, authed_user):
    _, token = authed_user
    r = await client.get("/api/v1/templates/nope", headers=_auth(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_use_template_refuses_when_required_missing(client, authed_user):
    """A half-filled agreement that looks finished is worse than an error."""
    _, token = authed_user
    r = await client.post(
        "/api/v1/templates/offer-letter/use", headers=_auth(token),
        json={"values": {"job_title": "Engineer"}},
    )
    assert r.status_code == 400
    assert "Company name" in r.json()["detail"]


@pytest.mark.asyncio
async def test_use_template_creates_editable_draft(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/templates/employee-nda/use", headers=_auth(token),
        json={"values": {
            "company_name": "Acme Ltd", "company_address": "1 Road",
            "company_signatory": "Chidi", "employee_name": "Ada Lovelace",
            "governing_law": "Nigeria",
        }, "title": "NDA - Ada"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original_file_name"] == "NDA - Ada.pdf"

    doc = await Document.get(id=body["id"])
    assert doc.status == DocumentStatus.DRAFT
    assert doc.metadata["origin"] == "template"
    assert doc.metadata["template_slug"] == "employee-nda"
    # Values kept so the document can be regenerated or audited.
    assert doc.metadata["template_values"]["company_name"] == "Acme Ltd"
    # Starts with no placements, so it opens cleanly in the editor.
    assert doc.field_placements == []


@pytest.mark.asyncio
async def test_use_template_ignores_unknown_keys(client, authed_user):
    """The request body must not decide what gets substituted."""
    _, token = authed_user
    r = await client.post(
        "/api/v1/templates/employee-nda/use", headers=_auth(token),
        json={"values": {
            "company_name": "Acme Ltd", "company_address": "1 Road",
            "company_signatory": "Chidi", "employee_name": "Ada",
            "governing_law": "Nigeria",
            "injected_key": "should be ignored",
        }},
    )
    assert r.status_code == 200
    doc = await Document.get(id=r.json()["id"])
    assert "injected_key" not in doc.metadata["template_values"]


@pytest.mark.asyncio
async def test_use_template_unknown_slug_404(client, authed_user):
    _, token = authed_user
    r = await client.post(
        "/api/v1/templates/nope/use", headers=_auth(token), json={"values": {}},
    )
    assert r.status_code == 404

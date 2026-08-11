"""Phase 3: ready-made document templates, focused on new hires and new
contracts.

A template is *shipped content*, not user data, so the catalogue lives here in
code rather than in a database table. That means no migration against a live
database, no seeding step, and the wording is versioned and reviewable in git
alongside everything else. (User-authored templates would need a table; that is
deliberately not what this is.)

The body is HTML with `{{placeholder}}` markers, rendered to PDF through
PyMuPDF's Story, which flows text across as many pages as it needs. Storing a
pre-baked PDF instead would have been simpler but defeats the point: the
requirement is prewritten text the user can *edit*, and editing prose means it
has to reflow.

Jurisdiction is a placeholder, never assumed. A template that hard-codes a
governing law is wrong everywhere except one country.

IMPORTANT — these are generic drafting starting points, not legal advice. The
wording is deliberately plain and conventional. `LEGAL_DISCLAIMER` is surfaced
by the API so the UI can say so at the point of use.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape

from loguru import logger


LEGAL_DISCLAIMER = (
    "This template is a generic starting point, not legal advice. Have it "
    "reviewed by a qualified lawyer in the relevant jurisdiction before you "
    "rely on it."
)


@dataclass(frozen=True)
class Placeholder:
    key: str
    label: str
    # text | multiline | date | number | currency
    kind: str = "text"
    # Prefill from the user's vault when they have this field saved.
    vault_field: str | None = None
    required: bool = True
    help: str | None = None
    default: str | None = None
    # Only the document's own date should silently become today. A start date or
    # an offer expiry that quietly defaults to today is actively wrong — an
    # offer that expires the moment it is written — so date fields opt in.
    default_today: bool = False


@dataclass(frozen=True)
class TemplateSpec:
    slug: str
    title: str
    category: str
    summary: str
    body_html: str
    placeholders: tuple[Placeholder, ...] = field(default_factory=tuple)

    def placeholder_keys(self) -> set[str]:
        return {p.key for p in self.placeholders}


# ---- Shared placeholder definitions ---------------------------------------
#
# Defined once so the same concept has the same key across templates: a user
# who fills "company_name" for an offer letter shouldn't retype it for the NDA,
# and the vault mapping stays consistent.

# Deliberately granular. A template must declare only the fields its body
# actually shows — asking someone to type an address that never appears in the
# document is a small betrayal of their time, and the catalogue tests enforce
# the match in both directions.
_EMPLOYER = (
    Placeholder("company_name", "Company name", help="The employing entity's registered name."),
    Placeholder("company_signatory", "Who signs for the company"),
    Placeholder("company_signatory_title", "Their job title", required=False,
                default="Director"),
)

_COMPANY_ADDRESS = (
    Placeholder("company_address", "Company address", kind="multiline"),
)

_EMPLOYEE_NAME = (
    Placeholder("employee_name", "Employee full name", vault_field="full_legal_name"),
)

_EMPLOYEE_ADDRESS = (
    Placeholder("employee_address", "Employee address", kind="multiline",
                vault_field="street_address_line_1", required=False),
)

# Most templates want both parties in full.
_EMPLOYEE = (*_EMPLOYEE_NAME, *_EMPLOYEE_ADDRESS)

_GOVERNING_LAW = (
    Placeholder(
        "governing_law", "Governing law",
        help="The country or state whose law applies, e.g. 'England and Wales', "
             "'Nigeria', 'the State of Delaware'.",
    ),
)

_TODAY = Placeholder("agreement_date", "Date", kind="date", required=False,
                     default_today=True, help="Defaults to today.")


# ---- The catalogue --------------------------------------------------------

TEMPLATES: dict[str, TemplateSpec] = {}


def _register(spec: TemplateSpec) -> None:
    TEMPLATES[spec.slug] = spec


_register(TemplateSpec(
    slug="offer-letter",
    title="Employment Offer Letter",
    category="hiring",
    summary="Offer a role, with salary, start date and the conditions attached.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_EMPLOYEE, _TODAY,
        Placeholder("job_title", "Job title"),
        Placeholder("start_date", "Start date", kind="date"),
        Placeholder("salary", "Salary", kind="currency",
                    help="Include the currency and period, e.g. '£55,000 per year'."),
        Placeholder("working_hours", "Working hours", required=False,
                    default="9:00am to 5:00pm, Monday to Friday"),
        Placeholder("work_location", "Work location",
                    help="An address, 'Remote', or a hybrid arrangement."),
        Placeholder("probation_months", "Probation period (months)", kind="number",
                    required=False, default="3"),
        Placeholder("notice_period", "Notice period", required=False,
                    default="one month"),
        Placeholder("reporting_to", "Reports to", required=False),
        Placeholder("offer_expiry_date", "Offer valid until", kind="date",
                    required=False),
    ),
    body_html="""
<h1>Offer of Employment</h1>
<p class="meta">{{company_name}}<br/>{{company_address}}</p>
<p class="meta">{{agreement_date}}</p>

<p>{{employee_name}}<br/>{{employee_address}}</p>

<p>Dear {{employee_name}},</p>

<p>We are pleased to offer you the position of <b>{{job_title}}</b> at
{{company_name}}. This letter sets out the main terms of our offer.</p>

<h2>1. Position and reporting</h2>
<p>You will be employed as {{job_title}}, reporting to {{reporting_to}}. Your
duties will be those normally associated with the role, together with any other
reasonable duties we ask of you from time to time.</p>

<h2>2. Start date</h2>
<p>Your employment will begin on {{start_date}}, subject to the conditions in
section 7.</p>

<h2>3. Place of work and hours</h2>
<p>Your normal place of work will be {{work_location}}. Your normal working
hours will be {{working_hours}}, though you may need to work additional hours
where the role reasonably requires it.</p>

<h2>4. Salary</h2>
<p>Your salary will be {{salary}}, paid monthly in arrears by bank transfer,
subject to any deductions required by law.</p>

<h2>5. Probation period</h2>
<p>The first {{probation_months}} months of your employment will be a probation
period. During this period either party may end the employment on shorter
notice, as set out in your contract of employment.</p>

<h2>6. Notice</h2>
<p>After successful completion of your probation period, either party may end
the employment by giving {{notice_period}} written notice.</p>

<h2>7. Conditions</h2>
<p>This offer is conditional on:</p>
<ul>
  <li>your confirmation that you are legally entitled to work in the relevant
      jurisdiction;</li>
  <li>satisfactory references;</li>
  <li>your signing our confidentiality agreement and contract of employment.</li>
</ul>

<h2>8. Acceptance</h2>
<p>If you would like to accept this offer, please sign and return a copy of this
letter{{offer_expiry_clause}}. This letter is not a contract of employment; your
full terms will be set out in the contract of employment provided to you.</p>

<p>We are looking forward to working with you.</p>

<table class="sigs">
  <tr>
    <td>
      <p class="siglabel">For and on behalf of {{company_name}}</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
    <td>
      <p class="siglabel">Accepted by</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{employee_name}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
  </tr>
</table>
""",
))


_register(TemplateSpec(
    slug="employment-contract",
    title="Contract of Employment",
    category="hiring",
    summary="The full written terms for a permanent employee.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_EMPLOYEE, *_GOVERNING_LAW, _TODAY,
        Placeholder("job_title", "Job title"),
        Placeholder("start_date", "Start date", kind="date"),
        Placeholder("salary", "Salary", kind="currency"),
        Placeholder("working_hours", "Working hours", required=False,
                    default="9:00am to 5:00pm, Monday to Friday"),
        Placeholder("work_location", "Work location"),
        Placeholder("probation_months", "Probation period (months)", kind="number",
                    required=False, default="3"),
        Placeholder("notice_period", "Notice period after probation", required=False,
                    default="one month"),
        Placeholder("probation_notice", "Notice during probation", required=False,
                    default="one week"),
        Placeholder("holiday_days", "Paid holiday (days per year)", kind="number",
                    required=False, default="25"),
        Placeholder("pension_note", "Pension arrangement", kind="multiline",
                    required=False,
                    default="You will be enrolled in the company pension scheme in "
                            "accordance with applicable law."),
    ),
    body_html="""
<h1>Contract of Employment</h1>
<p class="meta">Dated {{agreement_date}}</p>

<p>This contract is between <b>{{company_name}}</b> of {{company_address}}
(the "Employer") and <b>{{employee_name}}</b> of {{employee_address}}
(the "Employee").</p>

<h2>1. Commencement</h2>
<p>The Employee's employment begins on {{start_date}} and continues until ended
by either party in accordance with this contract.</p>

<h2>2. Job title and duties</h2>
<p>The Employee is employed as {{job_title}}. The Employee will perform the
duties reasonably associated with that role, will act in the Employer's best
interests, and will comply with the Employer's policies as updated from time to
time.</p>

<h2>3. Place of work and hours</h2>
<p>The Employee's normal place of work is {{work_location}}. Normal working
hours are {{working_hours}}. The Employee may be required to work additional
hours where the role reasonably requires it.</p>

<h2>4. Remuneration</h2>
<p>The Employer will pay the Employee {{salary}}, monthly in arrears, subject to
deductions required by law. Salary will be reviewed periodically; a review does
not imply an increase.</p>

<h2>5. Probation period</h2>
<p>The first {{probation_months}} months are a probation period, during which
either party may end the employment on {{probation_notice}} written notice. The
Employer may extend the probation period once, by written notice.</p>

<h2>6. Holiday</h2>
<p>The Employee is entitled to {{holiday_days}} days of paid holiday per
holiday year, in addition to public holidays, to be taken at times approved in
advance by the Employer.</p>

<h2>7. Sickness</h2>
<p>The Employee must notify the Employer as soon as reasonably practicable if
unable to work. The Employee is entitled to statutory sick pay and to any
additional entitlement under the Employer's policies.</p>

<h2>8. Pension</h2>
<p>{{pension_note}}</p>

<h2>9. Confidentiality</h2>
<p>The Employee must not, during employment or afterwards, use or disclose the
Employer's confidential information except as required to perform the role or
by law. Confidential information includes business plans, financial information,
customer and supplier details, technical information and anything else
reasonably regarded as confidential.</p>

<h2>10. Intellectual property</h2>
<p>All intellectual property the Employee creates in the course of employment
belongs to the Employer. The Employee agrees to sign any document reasonably
required to give effect to this.</p>

<h2>11. Termination</h2>
<p>After the probation period, either party may end the employment by giving
{{notice_period}} written notice. The Employer may end the employment without
notice in cases of gross misconduct. The Employer may pay in lieu of notice.</p>

<h2>12. Return of property</h2>
<p>On termination the Employee must return all property belonging to the
Employer, including documents, equipment and access credentials, and must delete
Employer data held on personal devices.</p>

<h2>13. Data protection</h2>
<p>The Employer will process the Employee's personal data in accordance with
applicable data protection law and its privacy notice.</p>

<h2>14. Entire agreement</h2>
<p>This contract sets out the entire agreement between the parties relating to
the Employee's employment and replaces any earlier agreement or understanding,
including any offer letter, on the matters it covers.</p>

<h2>15. Governing law</h2>
<p>This contract is governed by the law of {{governing_law}}, and the parties
submit to the exclusive jurisdiction of its courts.</p>

<table class="sigs">
  <tr>
    <td>
      <p class="siglabel">For and on behalf of the Employer</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
    <td>
      <p class="siglabel">The Employee</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{employee_name}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
  </tr>
</table>
""",
))


_register(TemplateSpec(
    slug="employee-nda",
    title="Confidentiality Agreement (New Starter)",
    category="hiring",
    summary="A confidentiality undertaking for someone joining the business.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_EMPLOYEE_NAME, *_GOVERNING_LAW, _TODAY,
        Placeholder("job_title", "Job title", required=False),
        Placeholder("survival_years", "Confidentiality continues for (years after leaving)",
                    kind="number", required=False, default="3"),
    ),
    body_html="""
<h1>Confidentiality Agreement</h1>
<p class="meta">Dated {{agreement_date}}</p>

<p>Between <b>{{company_name}}</b> of {{company_address}} (the "Company") and
<b>{{employee_name}}</b> (the "Recipient").</p>

<h2>1. Purpose</h2>
<p>The Recipient is joining the Company as {{job_title}} and will have access to
confidential information. This agreement sets out how that information must be
treated.</p>

<h2>2. What is confidential</h2>
<p>"Confidential Information" means any non-public information the Recipient
learns through their engagement with the Company, in any form, including:
business and financial information; customer, supplier and employee
information; product plans, source code and technical material; pricing; and
anything a reasonable person would understand to be confidential.</p>

<h2>3. Obligations</h2>
<p>The Recipient will:</p>
<ul>
  <li>keep Confidential Information secret and secure;</li>
  <li>use it only to perform their duties for the Company;</li>
  <li>not disclose it to anyone outside the Company without authorisation;</li>
  <li>not copy or remove it except as required for their duties.</li>
</ul>

<h2>4. Exclusions</h2>
<p>This agreement does not apply to information that is or becomes public
without breach of this agreement, that the Recipient already lawfully knew, or
that the Recipient is required to disclose by law or a regulator — in which
case the Recipient will notify the Company promptly where lawful to do so.</p>

<h2>5. Protected disclosures</h2>
<p>Nothing in this agreement prevents the Recipient from making a disclosure
that applicable law protects, including reporting wrongdoing to a regulator or
law enforcement.</p>

<h2>6. Return of information</h2>
<p>On request, and in any event when their engagement ends, the Recipient will
return or securely delete all Confidential Information in their possession,
including copies held on personal devices.</p>

<h2>7. Duration</h2>
<p>These obligations apply during the Recipient's engagement and for
{{survival_years}} years afterwards. Obligations relating to trade secrets
continue for as long as the information remains a trade secret.</p>

<h2>8. No licence</h2>
<p>Nothing in this agreement transfers any intellectual property or grants any
licence beyond what is needed to perform the Recipient's duties.</p>

<h2>9. Governing law</h2>
<p>This agreement is governed by the law of {{governing_law}}.</p>

<table class="sigs">
  <tr>
    <td>
      <p class="siglabel">For and on behalf of the Company</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
    <td>
      <p class="siglabel">The Recipient</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{employee_name}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
  </tr>
</table>
""",
))


_register(TemplateSpec(
    slug="contractor-agreement",
    title="Independent Contractor Agreement",
    category="contracts",
    summary="Engage someone as a contractor rather than an employee.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_GOVERNING_LAW, _TODAY,
        Placeholder("contractor_name", "Contractor name", vault_field="full_legal_name"),
        Placeholder("contractor_address", "Contractor address", kind="multiline",
                    required=False),
        Placeholder("services", "Services to be provided", kind="multiline",
                    help="Describe the work. This becomes the scope clause."),
        Placeholder("start_date", "Start date", kind="date"),
        Placeholder("end_date", "End date", kind="date", required=False,
                    help="Leave blank for an ongoing engagement."),
        Placeholder("fees", "Fees", kind="currency",
                    help="e.g. '£500 per day' or '£8,000 per month'."),
        Placeholder("payment_terms", "Payment terms", required=False,
                    default="within 30 days of a valid invoice"),
        Placeholder("notice_period", "Notice period", required=False,
                    default="two weeks"),
    ),
    body_html="""
<h1>Independent Contractor Agreement</h1>
<p class="meta">Dated {{agreement_date}}</p>

<p>Between <b>{{company_name}}</b> of {{company_address}} (the "Client") and
<b>{{contractor_name}}</b> of {{contractor_address}} (the "Contractor").</p>

<h2>1. Services</h2>
<p>The Contractor will provide the following services:</p>
<p>{{services}}</p>

<h2>2. Term</h2>
<p>The engagement begins on {{start_date}} and continues until {{end_date}},
unless ended earlier under section 7.</p>

<h2>3. Fees and payment</h2>
<p>The Client will pay the Contractor {{fees}}. The Contractor will invoice the
Client, and the Client will pay {{payment_terms}}. Fees exclude any applicable
sales tax, which will be added where required.</p>

<h2>4. Status</h2>
<p>The Contractor is an independent contractor, not an employee, worker or
agent of the Client. The Contractor is responsible for their own taxes and
social contributions, and for any insurance appropriate to the services. Nothing
in this agreement creates a partnership or employment relationship.</p>

<h2>5. Substitution and subcontracting</h2>
<p>The Contractor may not subcontract or substitute another person to perform
the services without the Client's prior written consent.</p>

<h2>6. Confidentiality and intellectual property</h2>
<p>The Contractor will keep the Client's confidential information secret and use
it only to provide the services. All intellectual property created in providing
the services is assigned to the Client on creation, and the Contractor will sign
any document reasonably required to give effect to that assignment.</p>

<h2>7. Termination</h2>
<p>Either party may end this agreement by giving {{notice_period}} written
notice. Either party may end it immediately if the other materially breaches it
and fails to remedy the breach within 14 days of written notice. On termination
the Client will pay for services properly provided up to that date.</p>

<h2>8. Limitation of liability</h2>
<p>Neither party is liable for indirect or consequential loss. Nothing in this
agreement limits liability that cannot be limited by law.</p>

<h2>9. Governing law</h2>
<p>This agreement is governed by the law of {{governing_law}}.</p>

<table class="sigs">
  <tr>
    <td>
      <p class="siglabel">For and on behalf of the Client</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
    <td>
      <p class="siglabel">The Contractor</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{contractor_name}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
  </tr>
</table>
""",
))


_register(TemplateSpec(
    slug="ip-assignment",
    title="Intellectual Property Assignment",
    category="hiring",
    summary="Confirm that work created for the business belongs to the business.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_EMPLOYEE_NAME, *_GOVERNING_LAW, _TODAY,
        Placeholder("work_description", "Work covered", kind="multiline",
                    required=False,
                    default="All work created in the course of the Assignor's "
                            "engagement with the Company."),
    ),
    body_html="""
<h1>Intellectual Property Assignment</h1>
<p class="meta">Dated {{agreement_date}}</p>

<p>Between <b>{{company_name}}</b> of {{company_address}} (the "Company") and
<b>{{employee_name}}</b> (the "Assignor").</p>

<h2>1. Assignment</h2>
<p>The Assignor assigns to the Company, with full title guarantee, all present
and future intellectual property rights in the following work:</p>
<p>{{work_description}}</p>

<h2>2. Scope of rights</h2>
<p>This assignment covers copyright, database rights, design rights, patents,
trade marks, trade secrets and all other intellectual property rights, in every
jurisdiction, for their full term including renewals and extensions.</p>

<h2>3. Moral rights</h2>
<p>To the extent permitted by law, the Assignor waives any moral rights in the
work. Where those rights cannot be waived, the Assignor agrees not to assert
them against the Company or anyone authorised by it.</p>

<h2>4. Further assurance</h2>
<p>The Assignor will, at the Company's request and expense, sign any document
and do anything reasonably necessary to give the Company full benefit of this
assignment, including assisting with registration or enforcement.</p>

<h2>5. Pre-existing and third-party material</h2>
<p>The Assignor confirms that the work is their own and does not infringe the
rights of any third party, and will disclose to the Company any pre-existing or
third-party material incorporated into it, together with the terms on which it
may be used.</p>

<h2>6. Governing law</h2>
<p>This assignment is governed by the law of {{governing_law}}.</p>

<table class="sigs">
  <tr>
    <td>
      <p class="siglabel">For and on behalf of the Company</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
    <td>
      <p class="siglabel">The Assignor</p>
      <p class="sigline">Signature: _______________________________</p>
      <p class="sigmeta">{{employee_name}}</p>
      <p class="sigline">Date: ____________________</p>
    </td>
  </tr>
</table>
""",
))


_register(TemplateSpec(
    slug="probation-confirmation",
    title="Confirmation of Employment After Probation",
    category="hiring",
    summary="Confirm a new starter has passed probation, and what changes.",
    placeholders=(
        *_EMPLOYER, *_COMPANY_ADDRESS, *_EMPLOYEE, _TODAY,
        Placeholder("job_title", "Job title"),
        Placeholder("probation_end_date", "Probation ended on", kind="date"),
        Placeholder("notice_period", "Notice period from now on", required=False,
                    default="one month"),
        Placeholder("salary_change", "Salary change, if any", kind="multiline",
                    required=False,
                    default="Your salary remains unchanged."),
    ),
    body_html="""
<h1>Confirmation of Employment</h1>
<p class="meta">{{company_name}}<br/>{{company_address}}</p>
<p class="meta">{{agreement_date}}</p>

<p>{{employee_name}}<br/>{{employee_address}}</p>

<p>Dear {{employee_name}},</p>

<p>I am pleased to confirm that you have successfully completed your probation
period as {{job_title}}, which ended on {{probation_end_date}}. Your employment
with {{company_name}} continues on a permanent basis.</p>

<h2>What changes</h2>
<p>From the date of this letter, the notice period either party must give to end
your employment is {{notice_period}}, as set out in your contract of
employment.</p>

<p>{{salary_change}}</p>

<h2>What stays the same</h2>
<p>All other terms of your contract of employment remain unchanged, including
your duties, place of work and confidentiality obligations.</p>

<p>Thank you for your contribution so far. We are glad to have you with us.</p>

<p class="siglabel">Yours sincerely,</p>
<p class="sigline">Signature: _______________________________</p>
<p class="sigmeta">{{company_signatory}}<br/>{{company_signatory_title}}<br/>{{company_name}}</p>
""",
))


# ---- Rendering ------------------------------------------------------------

# Print-document typography. Serif body at 10.5pt with generous leading reads
# as a contract rather than a web page, and the signature table keeps both
# parties' blocks side by side without a page break between them.
_TEMPLATE_CSS = """
body { font-family: serif; font-size: 10.5pt; line-height: 1.5; }
h1 { font-size: 16pt; text-align: center; margin-bottom: 2pt; }
h2 { font-size: 11pt; margin-top: 12pt; margin-bottom: 2pt; }
p  { margin-top: 0pt; margin-bottom: 7pt; text-align: justify; }
ul { margin-top: 0pt; margin-bottom: 7pt; }
li { margin-bottom: 2pt; }
.meta { text-align: center; font-size: 9pt; margin-bottom: 14pt; }
.sigs { width: 100%; margin-top: 22pt; }
.sigs td { width: 50%; vertical-align: top; padding-right: 12pt; }
.siglabel { font-size: 9pt; margin-top: 14pt; margin-bottom: 10pt; }
.sigline { margin-bottom: 8pt; }
.sigmeta { font-size: 9pt; margin-bottom: 8pt; }
"""

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")

# What an unfilled optional placeholder becomes. A visible rule is better than
# an empty gap or a leftover "{{key}}" — the user can complete it by hand or in
# the editor, and it's obvious that something is missing.
_BLANK = "__________"


def get_template(slug: str) -> TemplateSpec:
    spec = TEMPLATES.get((slug or "").strip().lower())
    if not spec:
        raise ValueError(
            f"Unknown template {slug!r}. Available: {', '.join(sorted(TEMPLATES))}."
        )
    return spec


def resolve_values(
    spec: TemplateSpec,
    supplied: dict[str, str] | None,
    vault: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Work out the final value for every placeholder.

    Precedence: what the user typed → their vault → the placeholder's default →
    today's date for `kind="date"` fields. Returns `(values, missing_required)`
    so the caller can refuse the render with a useful message rather than
    producing a document full of blanks.
    """
    supplied = {k: (v or "").strip() for k, v in (supplied or {}).items()}
    vault = vault or {}
    values: dict[str, str] = {}
    missing: list[str] = []

    for ph in spec.placeholders:
        value = supplied.get(ph.key) or ""
        if not value and ph.vault_field:
            value = (vault.get(ph.vault_field) or "").strip()
        if not value and ph.default:
            value = ph.default
        if not value and ph.default_today:
            value = date.today().strftime("%d %B %Y")
        if not value and ph.required:
            missing.append(ph.label)
        values[ph.key] = value

    return values, missing


def fill_body(spec: TemplateSpec, values: dict[str, str]) -> str:
    """Substitute `{{key}}` markers, escaping every value.

    Values are user input and the result is parsed as HTML by Story, so an
    unescaped "&" or "<" would corrupt the document — and a pasted tag would be
    rendered as markup.
    """
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        # Composed clauses that only appear when their input was given.
        if key == "offer_expiry_clause":
            expiry = values.get("offer_expiry_date", "")
            return f" by {escape(expiry)}" if expiry else ""
        raw = values.get(key, "")
        if not raw:
            return _BLANK
        # Multi-line input (addresses, scope descriptions) keeps its breaks.
        return escape(raw).replace("\n", "<br/>")

    body = _PLACEHOLDER_RE.sub(sub, spec.body_html)
    # Leftover markers would be a bug in a template's own wording; surface it in
    # logs rather than shipping "{{foo}}" to a user's contract.
    for leftover in _PLACEHOLDER_RE.findall(body):
        logger.warning(f"template {spec.slug}: unresolved placeholder {leftover!r}")
    return body


def render_template_pdf(
    spec: TemplateSpec,
    values: dict[str, str],
    *,
    page_size: str = "a4",
) -> bytes:
    """Flow the filled template into a paginated PDF.

    Uses PyMuPDF's Story rather than the positioned-frame renderer: legal text
    is long and its length depends on what the user typed, so it has to break
    across as many pages as it needs. A fixed frame would clip or shrink it.
    """
    import fitz

    from app.common.services.pdf_authoring import resolve_page_size

    width, height = resolve_page_size(page_size)
    media = fitz.Rect(0, 0, width, height)
    # ~2cm margins.
    area = media + (57, 57, -57, -57)

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    story = fitz.Story(html=fill_body(spec, values), user_css=_TEMPLATE_CSS)

    more, pages = 1, 0
    while more:
        device = writer.begin_page(media)
        more, _filled = story.place(area)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages > 60:
            # Runaway guard: a template that never finishes placing would
            # otherwise spin until the request times out.
            logger.error(f"template {spec.slug} exceeded 60 pages — truncating")
            break
    writer.close()
    logger.info(f"rendered template {spec.slug}: {pages} page(s)")
    return buf.getvalue()


def template_catalogue() -> list[dict]:
    """The catalogue in the shape the gallery needs."""
    return [
        {
            "slug": s.slug,
            "title": s.title,
            "category": s.category,
            "summary": s.summary,
            "placeholder_count": len(s.placeholders),
        }
        for s in sorted(TEMPLATES.values(), key=lambda t: (t.category, t.title))
    ]

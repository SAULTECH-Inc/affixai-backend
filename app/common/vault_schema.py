"""Predefined Data Vault segments and field registry.

The vault stores user identity data across a fixed set of segments. Each segment
has a predefined list of fields the frontend can render as a form. All fields
are optional — users fill what they want. New fields require a code change here.

The registry is the single source of truth: routes validate that incoming
`field_name`s belong to their declared segment, and the frontend fetches the
registry to build its forms.

Each field also carries `aliases` — common labels you'd see for that field on
real documents (driver's licenses, passports, utility bills, forms). The
extraction layer (Phase 3) uses these to map OCR'd `Label: Value` pairs to
vault fields.
"""
from __future__ import annotations

from enum import Enum
from typing import TypedDict


class VaultSegment(str, Enum):
    PERSONAL = "personal"
    IDENTITY = "identity"
    ADDRESS = "address"
    CONTACT = "contact"
    EMPLOYMENT = "employment"
    EDUCATION = "education"
    FINANCIAL = "financial"
    NEXT_OF_KIN = "next_of_kin"


class FieldType(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    DATE = "date"
    NUMBER = "number"
    SELECT = "select"
    TEXTAREA = "textarea"


class FieldDef(TypedDict, total=False):
    name: str
    label: str
    type: FieldType
    placeholder: str
    options: list[str]
    description: str
    aliases: list[str]


PERSONAL_FIELDS: list[FieldDef] = [
    {"name": "title", "label": "Title", "type": FieldType.SELECT,
     "options": ["Mr", "Mrs", "Ms", "Miss", "Dr", "Prof", "Rev"],
     "aliases": ["Title", "Salutation", "Honorific"]},
    {"name": "first_name", "label": "First Name", "type": FieldType.TEXT,
     "aliases": ["First Name", "Given Name", "Given Names", "Forename", "First"]},
    {"name": "middle_name", "label": "Middle Name", "type": FieldType.TEXT,
     "aliases": ["Middle Name", "Middle", "Middle Names", "Other Names"]},
    {"name": "last_name", "label": "Last Name", "type": FieldType.TEXT,
     "aliases": ["Last Name", "Surname", "Family Name", "Last"]},
    {"name": "preferred_name", "label": "Preferred Name", "type": FieldType.TEXT,
     "aliases": ["Preferred Name", "Nickname", "Known As"]},
    {"name": "full_legal_name", "label": "Full Legal Name", "type": FieldType.TEXT,
     "description": "How your name appears on official documents — used for typed signatures.",
     "aliases": ["Full Name", "Legal Name", "Full Legal Name", "Name",
                 "Name of the Candidate", "Name of Candidate", "Candidate Name",
                 "Candidate's Name", "Applicant's Name", "Applicant Name",
                 "Name of the Applicant", "Name of Applicant",
                 "Full Name of the Candidate", "Name in Full", "Name (in full)",
                 # NDA / contract conventions:
                 "Typed or Printed Name", "Printed Name", "Print Name",
                 "Name (printed)", "Party Disclosing Information",
                 "Party Receiving Information", "Disclosing Party",
                 "Receiving Party"]},
    {"name": "initials", "label": "Initials", "type": FieldType.TEXT,
     "placeholder": "e.g. J.D.S.",
     "aliases": ["Initials"]},
    {"name": "age", "label": "Age", "type": FieldType.TEXT,
     "description": "Auto-computed from Date of Birth — you don't need to set this directly.",
     "aliases": ["Age", "Current Age", "Age (years)", "Age in years", "Years"]},
    {"name": "date_of_birth", "label": "Date of Birth", "type": FieldType.DATE,
     "aliases": ["Date of Birth", "DOB", "D.O.B.", "Birth Date", "Born", "Date Of Birth"]},
    {"name": "gender", "label": "Gender", "type": FieldType.SELECT,
     "options": ["Male", "Female", "Other", "Prefer not to say"],
     "aliases": ["Gender", "Sex"]},
    {"name": "marital_status", "label": "Marital Status", "type": FieldType.SELECT,
     "options": ["Single", "Married", "Divorced", "Widowed", "Separated"],
     "aliases": ["Marital Status", "Civil Status"]},
    {"name": "nationality", "label": "Nationality", "type": FieldType.TEXT,
     "aliases": ["Nationality", "Citizenship", "National"]},
    {"name": "place_of_birth", "label": "Place of Birth", "type": FieldType.TEXT,
     "aliases": ["Place of Birth", "POB", "Born In", "Birthplace"]},
    {"name": "mother_maiden_name", "label": "Mother's Maiden Name", "type": FieldType.TEXT,
     "aliases": ["Mother's Maiden Name", "Mother Maiden Name", "Maiden Name"]},
    {"name": "fathers_name", "label": "Father's Name", "type": FieldType.TEXT,
     "aliases": ["Father's Name", "Father Name", "Father", "Fathers Name"]},
    {"name": "mothers_name", "label": "Mother's Name", "type": FieldType.TEXT,
     "aliases": ["Mother's Name", "Mother Name", "Mother", "Mothers Name"]},
    {"name": "spouse_name", "label": "Spouse's Name", "type": FieldType.TEXT,
     "aliases": ["Spouse's Name", "Spouse Name", "Husband's Name", "Husband Name",
                 "Wife's Name", "Wife Name", "Spouse", "Partner"]},
]

IDENTITY_FIELDS: list[FieldDef] = [
    {"name": "national_id_number", "label": "National ID / NIN", "type": FieldType.TEXT,
     "aliases": ["National ID", "NIN", "National Identification Number", "ID Number",
                 "National ID Number", "Aadhaar", "Aadhaar No", "Aadhaar Number",
                 "Aadhar", "Aadhar No"]},
    {"name": "national_id_issue_date", "label": "National ID Issue Date",
     "type": FieldType.DATE,
     "aliases": ["Issue Date", "NIN Issue Date", "ID Issue Date", "Date Issued",
                 "Date of Issue"]},
    {"name": "tax_identification_number", "label": "Tax Identification Number (TIN/SSN)",
     "type": FieldType.TEXT,
     "aliases": ["TIN", "Tax ID", "SSN", "Social Security Number",
                 "Tax Identification Number", "Tax Identification No"]},
    {"name": "passport_number", "label": "Passport Number", "type": FieldType.TEXT,
     "aliases": ["Passport Number", "Passport No", "Passport No.", "Passport #", "Passport"]},
    {"name": "passport_country", "label": "Passport Country", "type": FieldType.TEXT,
     "aliases": ["Country of Issue", "Issuing Country", "Passport Country"]},
    {"name": "passport_issue_date", "label": "Passport Issue Date", "type": FieldType.DATE,
     "aliases": ["Passport Issue Date", "Passport Date of Issue"]},
    {"name": "passport_expiry_date", "label": "Passport Expiry Date", "type": FieldType.DATE,
     "aliases": ["Passport Expiry Date", "Passport Date of Expiry", "Passport Expires",
                 "Passport Valid Until"]},
    {"name": "drivers_license_number", "label": "Driver's License Number", "type": FieldType.TEXT,
     "aliases": ["Driver's License Number", "DL Number", "DLN", "License Number",
                 "Driving License Number"]},
    {"name": "drivers_license_state", "label": "Driver's License State/Country",
     "type": FieldType.TEXT,
     "aliases": ["License State", "Issuing State", "DL State"]},
    {"name": "drivers_license_expiry", "label": "Driver's License Expiry", "type": FieldType.DATE,
     "aliases": ["License Expiry", "DL Expiry", "License Expires"]},
    {"name": "voter_id_number", "label": "Voter ID Number", "type": FieldType.TEXT,
     "aliases": ["Voter ID", "Voter Card Number", "VIN", "Voter's ID"]},
]

ADDRESS_FIELDS: list[FieldDef] = [
    {"name": "street_address_line_1", "label": "Street Address", "type": FieldType.TEXT,
     "aliases": ["Street Address", "Address", "Address Line 1", "Residential Address",
                 "Home Address", "Street", "Address 1", "Permanent Address",
                 "Current Address", "Present Address",
                 "Address for correspondence", "Correspondence Address",
                 "Mailing Address"]},
    {"name": "street_address_line_2", "label": "Apartment / Suite / Unit", "type": FieldType.TEXT,
     "aliases": ["Address Line 2", "Apt", "Apartment", "Suite", "Unit", "Address 2"]},
    {"name": "city", "label": "City", "type": FieldType.TEXT,
     "aliases": ["City", "Town", "Locality"]},
    {"name": "state_province", "label": "State / Province", "type": FieldType.TEXT,
     "aliases": ["State", "Province", "Region", "State/Province"]},
    {"name": "postal_code", "label": "Postal Code", "type": FieldType.TEXT,
     "aliases": ["Postal Code", "Zip", "Zip Code", "Postcode", "Post Code",
                 "PIN Code", "PIN", "Pincode"]},
    {"name": "country", "label": "Country", "type": FieldType.TEXT,
     "aliases": ["Country", "Country of Residence"]},
    {"name": "residence_since", "label": "Residing Since", "type": FieldType.DATE,
     "aliases": ["Residing Since", "Resident Since", "Living Here Since"]},
    {"name": "address_type", "label": "Address Type", "type": FieldType.SELECT,
     "options": ["Home", "Mailing", "Work", "Other"],
     "aliases": ["Address Type"]},
]

CONTACT_FIELDS: list[FieldDef] = [
    {"name": "primary_email", "label": "Primary Email", "type": FieldType.EMAIL,
     "aliases": ["Email", "E-mail", "Email Address", "Primary Email"]},
    {"name": "secondary_email", "label": "Secondary Email", "type": FieldType.EMAIL,
     "aliases": ["Secondary Email", "Alternate Email", "Alt Email"]},
    {"name": "primary_phone", "label": "Primary Phone", "type": FieldType.PHONE,
     "aliases": ["Phone", "Phone Number", "Mobile", "Cell", "Cell Phone",
                 "Mobile Number", "Mobile No", "Mobile No.", "Mobile #",
                 "Telephone", "Tel", "Contact Number", "Primary Phone",
                 "Contact No", "Contact No."]},
    {"name": "secondary_phone", "label": "Secondary Phone", "type": FieldType.PHONE,
     "aliases": ["Secondary Phone", "Alternate Phone", "Other Phone",
                 "Telephone No", "Telephone No.", "Landline"]},
    {"name": "work_phone", "label": "Work Phone", "type": FieldType.PHONE,
     "aliases": ["Work Phone", "Office Phone", "Business Phone"]},
    {"name": "preferred_contact_method", "label": "Preferred Contact Method",
     "type": FieldType.SELECT,
     "options": ["Email", "Phone", "SMS", "WhatsApp"],
     "aliases": ["Preferred Contact Method", "Contact Preference"]},
]

EDUCATION_FIELDS: list[FieldDef] = [
    {"name": "institution_name", "label": "Institution", "type": FieldType.TEXT,
     "aliases": ["Institution", "Institution Name", "School", "School Name",
                 "University", "University Name", "College", "College Name",
                 "Academy", "Polytechnic", "Alma Mater"]},
    {"name": "course", "label": "Course / Discipline", "type": FieldType.TEXT,
     "aliases": ["Course", "Course Name", "Course of Study", "Course Studied",
                 "Discipline", "Field of Study", "Major", "Programme",
                 "Program", "Degree", "Degree Title", "Specialization",
                 "Subject", "Concentration", "Area of Study"]},
    {"name": "qualification", "label": "Qualification", "type": FieldType.SELECT,
     "options": ["High School", "Diploma", "Associate", "Bachelor's",
                 "Master's", "PhD / Doctorate", "Certificate", "Other"],
     "aliases": ["Qualification", "Degree Type", "Award", "Certification"]},
    {"name": "institution_address", "label": "Institution Address",
     "type": FieldType.TEXTAREA,
     "aliases": ["Institution Address", "School Address", "University Address",
                 "Campus Address", "Address"]},
    {"name": "city", "label": "City", "type": FieldType.TEXT,
     "aliases": ["City", "Institution City", "School City", "Town"]},
    {"name": "state", "label": "State / Province", "type": FieldType.TEXT,
     "aliases": ["State", "Institution State", "School State", "Province",
                 "Region"]},
    {"name": "country", "label": "Country", "type": FieldType.TEXT,
     "aliases": ["Country", "Institution Country", "School Country"]},
    {"name": "description", "label": "Description / Activities",
     "type": FieldType.TEXTAREA,
     "aliases": ["Description", "Activities", "Achievements", "Honours",
                 "Honors", "Notes"]},
    {"name": "start_date", "label": "Start Date", "type": FieldType.DATE,
     "aliases": ["Start Date", "From", "Date of Admission", "Admission Date",
                 "Began", "Commencement Date"]},
    {"name": "end_date", "label": "End Date", "type": FieldType.DATE,
     "aliases": ["End Date", "To", "Graduation Date", "Completion Date",
                 "Date of Graduation"]},
    {"name": "is_current", "label": "Currently studying here", "type": FieldType.SELECT,
     "options": ["No", "Yes"],
     "aliases": ["Currently Studying", "Current", "In Progress", "Ongoing"]},
]


EMPLOYMENT_FIELDS: list[FieldDef] = [
    {"name": "employer_name", "label": "Employer Name", "type": FieldType.TEXT,
     "aliases": ["Employer", "Employer Name", "Company", "Organization",
                 "Company Name", "Employer/Company"]},
    {"name": "job_title", "label": "Job Title", "type": FieldType.TEXT,
     "aliases": ["Job Title", "Position", "Role", "Designation", "Title"]},
    {"name": "department", "label": "Department", "type": FieldType.TEXT,
     "aliases": ["Department", "Dept", "Division"]},
    {"name": "employee_id", "label": "Employee ID", "type": FieldType.TEXT,
     "aliases": ["Employee ID", "Employee Number", "Staff ID", "Staff Number"]},
    {"name": "employment_type", "label": "Employment Type", "type": FieldType.SELECT,
     "options": ["Full-time", "Part-time", "Contract", "Freelance",
                 "Self-employed", "Unemployed"],
     "aliases": ["Employment Type", "Employment Status"]},
    {"name": "employment_start_date", "label": "Start Date", "type": FieldType.DATE,
     "aliases": ["Start Date", "Date of Joining", "Hired", "Hire Date",
                 "Employment Start Date"]},
    {"name": "employment_end_date", "label": "End Date", "type": FieldType.DATE,
     "aliases": ["End Date", "Date of Leaving", "Termination Date"]},
    {"name": "work_address", "label": "Work Address", "type": FieldType.TEXTAREA,
     "aliases": ["Work Address", "Office Address", "Business Address"]},
    {"name": "work_email", "label": "Work Email", "type": FieldType.EMAIL,
     "aliases": ["Work Email", "Office Email", "Business Email"]},
    {"name": "supervisor_name", "label": "Supervisor Name", "type": FieldType.TEXT,
     "aliases": ["Supervisor", "Manager", "Manager Name", "Reporting Manager"]},
    {"name": "annual_salary", "label": "Annual Salary", "type": FieldType.NUMBER,
     "aliases": ["Annual Salary", "Salary", "Yearly Salary", "Compensation",
                 "Existing Salary", "Current Salary", "Present Salary",
                 "Gross Salary", "Monthly Salary"]},
    {"name": "description", "label": "Description / Responsibilities",
     "type": FieldType.TEXTAREA,
     "aliases": ["Description", "Responsibilities", "Duties", "Key Achievements",
                 "Achievements", "Notes"]},
    {"name": "is_current", "label": "Currently working here",
     "type": FieldType.SELECT, "options": ["No", "Yes"],
     "aliases": ["Currently Working", "Current", "Present"]},
]

FINANCIAL_FIELDS: list[FieldDef] = [
    {"name": "bank_name", "label": "Bank Name", "type": FieldType.TEXT,
     "aliases": ["Bank Name", "Bank", "Financial Institution"]},
    {"name": "bank_account_name", "label": "Account Holder Name", "type": FieldType.TEXT,
     "aliases": ["Account Holder", "Account Holder Name", "Account Name",
                 "Name on Account"]},
    {"name": "bank_account_number", "label": "Account Number", "type": FieldType.TEXT,
     "aliases": ["Account Number", "Account No", "Account No.", "Acct No",
                 "Bank Account Number", "A/C No"]},
    {"name": "bank_routing_number", "label": "Routing / Sort Code", "type": FieldType.TEXT,
     "aliases": ["Routing Number", "Sort Code", "Routing/Sort Code", "ABA",
                 "Routing/ABA Number"]},
    {"name": "bank_swift_bic", "label": "SWIFT / BIC", "type": FieldType.TEXT,
     "aliases": ["SWIFT", "BIC", "SWIFT Code", "SWIFT/BIC", "BIC Code"]},
    {"name": "bank_iban", "label": "IBAN", "type": FieldType.TEXT,
     "aliases": ["IBAN", "IBAN Number", "International Bank Account Number"]},
    {"name": "currency", "label": "Currency", "type": FieldType.TEXT,
     "placeholder": "e.g. USD, NGN, EUR",
     "aliases": ["Currency"]},
    {"name": "annual_income", "label": "Annual Income", "type": FieldType.NUMBER,
     "aliases": ["Annual Income", "Income", "Yearly Income"]},
]

NEXT_OF_KIN_FIELDS: list[FieldDef] = [
    {"name": "next_of_kin_full_name", "label": "Full Name", "type": FieldType.TEXT,
     "aliases": ["Next of Kin Name", "Next of Kin Full Name", "Emergency Contact Name",
                 "NOK Name"]},
    {"name": "next_of_kin_relationship", "label": "Relationship", "type": FieldType.TEXT,
     "placeholder": "e.g. Spouse, Parent, Sibling",
     "aliases": ["Relationship", "NOK Relationship", "Emergency Contact Relationship"]},
    {"name": "next_of_kin_phone", "label": "Phone", "type": FieldType.PHONE,
     "aliases": ["Next of Kin Phone", "NOK Phone", "Emergency Contact Phone",
                 "Emergency Phone"]},
    {"name": "next_of_kin_email", "label": "Email", "type": FieldType.EMAIL,
     "aliases": ["Next of Kin Email", "NOK Email", "Emergency Contact Email"]},
    {"name": "next_of_kin_address", "label": "Address", "type": FieldType.TEXTAREA,
     "aliases": ["Next of Kin Address", "NOK Address", "Emergency Contact Address"]},
    {"name": "next_of_kin_date_of_birth", "label": "Date of Birth", "type": FieldType.DATE,
     "aliases": ["Next of Kin DOB", "NOK Date of Birth"]},
]


FIELD_REGISTRY: dict[VaultSegment, list[FieldDef]] = {
    VaultSegment.PERSONAL: PERSONAL_FIELDS,
    VaultSegment.IDENTITY: IDENTITY_FIELDS,
    VaultSegment.ADDRESS: ADDRESS_FIELDS,
    VaultSegment.CONTACT: CONTACT_FIELDS,
    VaultSegment.EMPLOYMENT: EMPLOYMENT_FIELDS,
    VaultSegment.EDUCATION: EDUCATION_FIELDS,
    VaultSegment.FINANCIAL: FINANCIAL_FIELDS,
    VaultSegment.NEXT_OF_KIN: NEXT_OF_KIN_FIELDS,
}


SEGMENT_LABELS: dict[VaultSegment, str] = {
    VaultSegment.PERSONAL: "Personal Information",
    VaultSegment.IDENTITY: "Identity Documents",
    VaultSegment.ADDRESS: "Address",
    VaultSegment.CONTACT: "Contact Information",
    VaultSegment.EMPLOYMENT: "Employment",
    VaultSegment.EDUCATION: "Education",
    VaultSegment.FINANCIAL: "Financial",
    VaultSegment.NEXT_OF_KIN: "Next of Kin",
}


# Sections that the frontend renders as a LIST of entries instead of a
# single form. The auto-affix resolver also flattens the "current" entry
# (falling back to most-recent) into the user's vault dict so label
# matchers like "Employer Name" → current job's employer name continue
# to work.
MULTI_ENTRY_SEGMENTS: set[VaultSegment] = {
    VaultSegment.EDUCATION,
    VaultSegment.EMPLOYMENT,
}


def is_multi_entry(segment: VaultSegment) -> bool:
    return segment in MULTI_ENTRY_SEGMENTS


def field_names_for(segment: VaultSegment) -> set[str]:
    return {f["name"] for f in FIELD_REGISTRY[segment]}


def is_valid_field(segment: VaultSegment, field_name: str) -> bool:
    return field_name in field_names_for(segment)


def serialize_registry() -> list[dict]:
    """Frontend-friendly registry dump."""
    return [
        {
            "segment": segment.value,
            "label": SEGMENT_LABELS[segment],
            "multi_entry": is_multi_entry(segment),
            "fields": [
                {
                    "name": f["name"],
                    "label": f["label"],
                    "type": f["type"].value,
                    "placeholder": f.get("placeholder"),
                    "options": f.get("options"),
                    "description": f.get("description"),
                }
                for f in fields
            ],
        }
        for segment, fields in FIELD_REGISTRY.items()
    ]


# ---- Extraction: map an arbitrary OCR'd label to a (segment, field) pair ----

def _normalize_label(text: str) -> str:
    """Lowercase, strip punctuation/whitespace from edges, collapse internal whitespace.

    Parens, brackets, and asterisks are folded to spaces so labels like
    `National Identification Number (NIN)` normalize comparably to the alias
    `National Identification Number`.
    """
    import re as _re

    cleaned = _re.sub(r"[()\[\]*]", " ", text)
    cleaned = cleaned.strip().rstrip(":.").strip().lower()
    return " ".join(cleaned.split())


def _candidate_strings(field: FieldDef) -> list[str]:
    out = [field["label"], field["name"].replace("_", " ")]
    out.extend(field.get("aliases", []) or [])
    return out


def _match_single(
    label: str,
    segment: VaultSegment | None,
    fuzzy_threshold: float,
    extra_fields: list[tuple[str, str, list[str]]] | None = None,
) -> tuple[VaultSegment, str, float] | None:
    """Inner match against a single label string.

    `extra_fields` lets callers append USER-DEFINED fields to the matching
    candidates without baking them into FIELD_REGISTRY. Each entry is
    (segment_key, field_name, aliases_including_display_label). The synthetic
    segment is returned as PERSONAL because callers only care about the
    field_name (which is what they look up in the vault dict).
    """
    try:
        from fuzzywuzzy import fuzz
    except ImportError:  # pragma: no cover
        fuzz = None  # type: ignore[assignment]

    normalized = _normalize_label(label)
    if not normalized:
        return None

    segments_to_search: list[VaultSegment] = (
        [segment] if segment is not None else list(FIELD_REGISTRY.keys())
    )

    best: tuple[float, VaultSegment, str] | None = None
    for seg in segments_to_search:
        for field in FIELD_REGISTRY[seg]:
            for candidate in _candidate_strings(field):
                cand_norm = _normalize_label(candidate)
                if cand_norm == normalized:
                    return seg, field["name"], 1.0
                if fuzz is not None:
                    score = fuzz.ratio(cand_norm, normalized) / 100.0
                    if best is None or score > best[0]:
                        best = (score, seg, field["name"])

    # Sweep through user-defined extras too. We treat custom fields as
    # PERSONAL for the segment value because nothing downstream of this
    # function looks at the segment (the call sites only use field_name).
    if extra_fields:
        for _seg_key, field_name, aliases in extra_fields:
            for candidate in aliases:
                cand_norm = _normalize_label(candidate)
                if cand_norm == normalized:
                    return VaultSegment.PERSONAL, field_name, 1.0
                if fuzz is not None:
                    score = fuzz.ratio(cand_norm, normalized) / 100.0
                    if best is None or score > best[0]:
                        best = (score, VaultSegment.PERSONAL, field_name)

    if best and best[0] >= fuzzy_threshold:
        return best[1], best[2], best[0]
    return None


def match_label_to_field(
    label: str,
    segment: VaultSegment | None = None,
    fuzzy_threshold: float = 0.85,
    extra_fields: list[tuple[str, str, list[str]]] | None = None,
) -> tuple[VaultSegment, str, float] | None:
    """Map a free-form label (from OCR) to a (segment, field_name, confidence).

    Strategy:
      1. Try the whole label first (normalized exact → fuzzy).
      2. If the label contains a slash (e.g. "SURNAME/NOM" on Nigerian IDs,
         "Last Name/Apellido" on US/Spanish forms), try each side
         independently and keep the best match.

    Returns None if best score < `fuzzy_threshold`.
    """
    best = _match_single(label, segment, fuzzy_threshold, extra_fields)

    if "/" in label:
        for part in label.split("/"):
            part = part.strip()
            if not part:
                continue
            candidate = _match_single(part, segment, fuzzy_threshold, extra_fields)
            if candidate and (best is None or candidate[2] > best[2]):
                best = candidate

    return best

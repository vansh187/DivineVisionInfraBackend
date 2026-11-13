"""Field mapping for the admin Inventory CSV export.

One row per plot. Plot fields come from divine_project_inventory. The buyer
fields come from the plot's booking application form (divine_documents
.form_data): for a website booking, the form attached to the booking payment;
for an offline buyer (sheet import), the buyer's form whose plot number matches.
The customer account fills in name / phone / email the form doesn't have.
Plots nobody has bought (no booking payment, no linked buyer) have no buyer
fields.

The form is read with the demand letter's own keys and helpers, plot numbers
are matched with the offline buyer import's rule, and the booking date is the
one the letters and instalment schedule use (the payment's date in IST), so
the CSV and those documents always agree.
"""
import csv
import io
import json
from datetime import date, datetime, timezone

from DivineService.service_demand_letter import (
    _BOOKING_DATE_KEYS, _PLOT_KEYS, _first, _s, _truthy,
    COMPANY, customer_address, project_office_address, project_profile,
)
from DivineService.service_offline_import import plot_key, project_slug
from DivineService.service_payment_plans import IST

_NAME_KEYS = ("applicantName", "applicant_name", "customer_name", "customerName", "name")
_PHONE_KEYS = ("mobile", "phone", "applicantMobile", "contact_no")
_ALT_PHONE_KEYS = ("alternate_phone", "alternatePhone", "alternateMobile", "alternative_number",
                   "alternativeNumber")
_EMAIL_KEYS = ("email", "applicantEmail", "applicant_email")
_PLOT_ADDRESS_KEYS = ("plot_address", "plotAddress")
_PLAN_KEYS = ("paymentPlanCode", "payment_plan_code")
# Where a form names its project. The document row's project_id is checked too.
_FORM_PROJECT_KEYS = ("projectId", "project_id", "project_name", "projectName", "project")

# Spreadsheet apps run a cell starting with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# CSV columns: (row key, header), in the order of the file. The client's
# sheet comes first, with its column names and order; the extra columns after
# it are only in the file when asked for (see DEFAULT_CSV_COLUMNS).
CLIENT_CSV_COLUMNS = (
    ("unit_number", "Plot No"), ("width_mtr", "Width (Mtr.)"), ("length_mtr", "Length (Mtr.)"),
    ("area_sqmt", "Area in Sq. Mtr."), ("area_sqyd", "Area in Sq. Yard"), ("status", "Plot Status"),
    ("project_name", "Project Name"), ("company_name", "Company Name"), ("block", "Block"),
    ("company_address", "Company Address"), ("payment_plan", "Payment Plan"), ("client_name", "Client Name"),
    ("phone", "Phone"), ("booking_date", "Booking Date"), ("alternative_number", "Alternative Number"),
    ("email", "Email ID"), ("co_applicant_email", "Co-applicant Email"), ("address", "Address"),
    ("co_applicant_address", "Co-applicant Address"), ("plot_address", "Plot Address"),
)
EXTRA_CSV_COLUMNS = (
    ("id", "Plot ID"), ("unit_type", "Type"), ("city", "City"), ("locality", "Locality"),
    ("sale_source", "Sale Source"), ("status_note", "Status Note"),
    ("last_changed_at", "Status Changed At"), ("last_changed_by_name", "Status Changed By"),
)
CSV_COLUMNS = CLIENT_CSV_COLUMNS + EXTRA_CSV_COLUMNS
CSV_COLUMN_KEYS = tuple(key for key, _ in CSV_COLUMNS)
DEFAULT_CSV_COLUMNS = tuple(key for key, _ in CLIENT_CSV_COLUMNS)

# Plot Status as the admin panel shows it.
STATUS_LABELS = {
    "available": "Available", "held": "On Hold", "reserved": "Reserved by Partner",
    "pending_kyc_review": "Booked (KYC Review)", "booked": "Booked", "sold": "Sold",
}


def _text(value) -> str:
    """One line of text: the letter's _s, with inner whitespace collapsed."""
    return " ".join(_s(value).split())


def _first_text(form: dict, keys) -> str:
    return _text(_first(form, keys))


def csv_safe(value):
    """Text a spreadsheet would run as a formula (CSV injection) gets a leading
    apostrophe, so it is shown as text. Numbers, dates and None pass through."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def load_form(value) -> dict:
    """form_data is a dict on Postgres (jsonb) and a JSON string on sqlite."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def project_key(value) -> str:
    """One key per project, whatever it's called: the inventory's "OPS Divine
    Greens", the website's "ops-divine-greens" and a frontend id like
    "divine-greens" all give "ops-divine-greens"."""
    slug = project_slug(str(value or ""))
    if "suraksha" in slug:
        return "suraksha-enclave"
    if "divine-greens" in slug:
        return "ops-divine-greens"
    return slug


def company_for_project(project_name: str) -> tuple:
    """(company name, company address) for the projects this company sells, as
    on their demand letters; blanks for any other project rather than a guess."""
    key = project_key(project_name)
    if key == "suraksha-enclave":
        return project_profile({"project_name": project_name}).get("company", ""), \
            project_office_address(project_name)
    if key == "ops-divine-greens":
        return COMPANY["name"], project_office_address(project_name)
    return "", ""


def default_plot_address(unit: dict) -> str:
    parts = [f"Plot No {unit.get('unit_number')}" if unit.get("unit_number") else "",
             _text(unit.get("project_name")), _text(unit.get("locality")), _text(unit.get("city"))]
    return ", ".join(p for p in parts if p)


class _Form:
    """A booking application form, parsed once, with what plot matching needs."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.data = load_form(doc.get("form_data"))
        self.plot_key = plot_key(_first(self.data, _PLOT_KEYS))
        # Every project the form or its document row names. The form passes the
        # project check if any of them is the plot's project (or none is given).
        self.projects = {project_key(v) for v in
                         [doc.get("project_id"), *(self.data.get(k) for k in _FORM_PROJECT_KEYS)]
                         if _text(v)}

    def is_for(self, unit: dict) -> bool:
        if not self.plot_key or self.plot_key != plot_key(unit.get("unit_number")):
            return False
        return not self.projects or project_key(unit.get("project_name")) in self.projects


def form_matches_unit(doc: dict, unit: dict) -> bool:
    return _Form(doc).is_for(unit)


def _plan_from_notes(notes) -> str:
    plan = load_form(notes).get("payment_plan")
    if isinstance(plan, dict):
        return _text(plan.get("payment_plan_code") or plan.get("code"))
    return ""


def _plan_from_form(form: dict) -> str:
    plan = form.get("payment_plan")
    if isinstance(plan, dict) and _text(plan.get("code") or plan.get("payment_plan_code")):
        return _text(plan.get("code") or plan.get("payment_plan_code"))
    return _first_text(form, _PLAN_KEYS)


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def ist_date(value) -> str:
    """A stored timestamp's calendar day in IST (naive = UTC), as the demand
    letters and instalment schedule count it. YYYY-MM-DD, or ""."""
    moment = _as_datetime(value)
    if moment is None:
        return value.isoformat() if isinstance(value, date) and not isinstance(value, datetime) else ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST).date().isoformat()


def form_date(value) -> str:
    """A date typed on the form: ISO, or DD-MM-YYYY / DD/MM/YYYY / DD.MM.YYYY."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _co_applicant_address(form: dict) -> str:
    keys = (("coApplicantPermanentAddress", "coApplicantCorrespondenceAddress")
            if _truthy(form.get("coApplicantCorrespondenceSameAsPermanent"))
            else ("coApplicantCorrespondenceAddress", "coApplicantPermanentAddress"))
    return _first_text(form, keys)


class ExportLinks:
    """Indexes admin_export_links() output for per-plot lookups. Each form is
    parsed once; an owner's forms are indexed by plot number."""

    def __init__(self, links: dict):
        self.payments = {str(p["payment_id"]): p for p in links.get("payments", [])}
        self.forms_by_payment = {}
        for doc in links.get("forms_by_payment", []):        # newest first: keep the first
            self.forms_by_payment.setdefault(str(doc["payment_id"]), _Form(doc))
        self.forms_by_owner_plot = {}
        for doc in links.get("forms_by_owner", []):          # newest first, per (owner, plot)
            form = _Form(doc)
            if form.plot_key:
                self.forms_by_owner_plot.setdefault((str(doc["owner_id"]), form.plot_key), []).append(form)
        self.customers = {str(c["id"]): c for c in links.get("customers", [])}

    def owner_form(self, owner_id, unit: dict):
        candidates = self.forms_by_owner_plot.get((str(owner_id), plot_key(unit.get("unit_number"))), [])
        return next((f for f in candidates if f.is_for(unit)), None)


def _payment_customer(payment: dict):
    if not payment:
        return None
    if payment.get("customer_id"):
        return payment["customer_id"]
    return payment.get("owner_id") if payment.get("owner_role") == "customer" else None


def buyer_fields(unit: dict, links: ExportLinks) -> dict:
    """The plot's buyer columns; all None for a plot nobody has bought."""
    payment_id = unit.get("booked_payment_id")
    payment = links.payments.get(str(payment_id)) if payment_id else None
    customer_id = _payment_customer(payment) or unit.get("booked_by")
    empty = {k: None for k in ("payment_plan", "client_name", "phone", "alternative_number", "email",
                               "address", "co_applicant_email", "co_applicant_address", "booking_date")}
    if not payment_id and not customer_id:
        return {**empty, "plot_address": None}

    form = links.forms_by_payment.get(str(payment_id)) if payment_id else None
    if form is None and customer_id:
        form = links.owner_form(customer_id, unit)
    data = form.data if form else {}
    customer = (links.customers.get(str(customer_id)) if customer_id else None) or {}
    customer_name = _text(f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}")

    # A website booking's date is its payment's date in IST, as on its demand
    # letter and schedule. An offline buyer has no payment: their form's date.
    booking_date = (ist_date((payment or {}).get("payment_created_date"))
                    or form_date(_first(data, _BOOKING_DATE_KEYS))
                    or ist_date(unit.get("booked_at")))
    values = {
        "payment_plan": _plan_from_notes((payment or {}).get("notes")) or _plan_from_form(data),
        "client_name": _first_text(data, _NAME_KEYS) or customer_name,
        "phone": _first_text(data, _PHONE_KEYS) or _text(customer.get("phone")),
        "alternative_number": _first_text(data, _ALT_PHONE_KEYS),
        "email": _first_text(data, _EMAIL_KEYS) or _text(customer.get("email")),
        "address": customer_address(data),
        "co_applicant_email": _first_text(data, ("coApplicantEmail",)),
        "co_applicant_address": _co_applicant_address(data),
        "booking_date": booking_date,
        "plot_address": _first_text(data, _PLOT_ADDRESS_KEYS),
    }
    return {k: (v or None) for k, v in values.items()}


def export_row(unit: dict, links: ExportLinks) -> dict:
    company_name, company_address = company_for_project(unit.get("project_name"))
    buyer = buyer_fields(unit, links)
    return {
        **buyer,
        "company_name": company_name or None,
        "company_address": company_address or None,
        "plot_address": buyer.get("plot_address") or default_plot_address(unit) or None,
    }


def safe_row(row: dict) -> dict:
    """Every text value of an export row made CSV-safe (see csv_safe)."""
    return {k: csv_safe(v) for k, v in row.items()}


def _csv_cell(key, value):
    if value is None:
        return ""
    if key == "status":
        return STATUS_LABELS.get(value, value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(IST).strftime("%Y-%m-%d %H:%M")      # as the panel shows it
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) if abs(value) < 1e15 else str(value)
    return value


def to_csv(rows, columns=None) -> str:
    """The export as CSV text: a UTF-8 BOM (so Excel reads the encoding), the
    header, then one line per row. ``columns``: the row keys to include (any
    order, unknown keys ignored); the file keeps CSV_COLUMNS order. Default:
    the client's sheet columns. Rows must already be safe_row()-ed."""
    wanted = set(columns) if columns else set(DEFAULT_CSV_COLUMNS)
    chosen = [(key, header) for key, header in CSV_COLUMNS if key in wanted]
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([header for _, header in chosen])
    for row in rows:
        writer.writerow([_csv_cell(key, row.get(key)) for key, _ in chosen])
    return out.getvalue()

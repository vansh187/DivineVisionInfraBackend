"""Field mapping for the admin Inventory CSV export.

One row per plot. Plot fields come from divine_project_inventory. The buyer
fields come from the plot's booking application form (divine_documents
.form_data): for a website booking, the form attached to the booking payment;
for an offline buyer (sheet import), the buyer's form whose plot number matches.
The customer account fills in name / phone / email the form doesn't have.
Plots nobody has bought (no booking payment, no linked buyer) have no buyer
fields.
"""
import json
import os
import re
from datetime import date, datetime

from DivineService.service_demand_letter import COMPANY, customer_address, project_profile

_DOC_PLOT_KEYS = ("unit_number", "unitNumber", "unit_no", "unitNo",
                  "plot_number", "plotNumber", "plot_no", "plotNo")
_NAME_KEYS = ("applicantName", "applicant_name", "customer_name", "customerName", "name")
_PHONE_KEYS = ("mobile", "phone", "applicantMobile", "contact_no")
_ALT_PHONE_KEYS = ("alternate_phone", "alternatePhone", "alternateMobile", "alternative_number",
                   "alternativeNumber")
_EMAIL_KEYS = ("email", "applicantEmail", "applicant_email")
_PLOT_ADDRESS_KEYS = ("plot_address", "plotAddress")
_BOOKING_DATE_KEYS = ("booking_date", "bookingDate")
_PLAN_KEYS = ("paymentPlanCode", "payment_plan_code")

# The project's developer, as on its demand letters. The address is the
# company's office for that project (the same one refund emails point to).
_SURAKSHA_OFFICE_DEFAULT = ("Suraksha Enclave Site Office - Village Garhi Kesri, Sector-15, "
                            "Ganaur, Sonipat, Haryana")
_OPS_OFFICE_DEFAULT = "OPS Divine Greens - Sec-16, Taraori, Karnal, Haryana 132116"


def _s(value) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def _first(form: dict, keys) -> str:
    for key in keys:
        value = _s(form.get(key))
        if value:
            return value
    return ""


def plot_key(value) -> str:
    """'B1-B', 'b1b', 'B 1-B' -> 'B1B' - same rule as the offline buyer import."""
    return re.sub(r"[\s\-_/.]", "", str(value or "")).upper()


def project_slug(project_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (project_name or "").lower()).strip("-")


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


def company_for_project(project_name: str) -> tuple:
    """(company name, company address) for the projects this company sells;
    blanks for any other project rather than a guess."""
    name = (project_name or "").lower()
    if "suraksha" in name:
        profile = project_profile({"project_name": project_name})
        return profile.get("company", ""), os.getenv("DIVINE_SURAKSHA_OFFICE_ADDRESS", _SURAKSHA_OFFICE_DEFAULT)
    if "divine greens" in name:
        return COMPANY["name"], os.getenv("DIVINE_PROJECT_ADDRESS", _OPS_OFFICE_DEFAULT)
    return "", ""


def default_plot_address(unit: dict) -> str:
    parts = [f"Plot No {unit.get('unit_number')}" if unit.get("unit_number") else "",
             _s(unit.get("project_name")), _s(unit.get("locality")), _s(unit.get("city"))]
    return ", ".join(p for p in parts if p)


def form_matches_unit(doc: dict, unit: dict) -> bool:
    form = load_form(doc.get("form_data"))
    plot = next((form.get(k) for k in _DOC_PLOT_KEYS if form.get(k)), None)
    if not plot or plot_key(plot) != plot_key(unit.get("unit_number")):
        return False
    project = doc.get("project_id") or form.get("projectId") or form.get("project_name")
    return not project or project_slug(str(project)) == project_slug(unit.get("project_name"))


def _plan_from_notes(notes) -> str:
    notes = load_form(notes)
    plan = notes.get("payment_plan")
    if isinstance(plan, dict):
        return _s(plan.get("payment_plan_code") or plan.get("code"))
    return ""


def _plan_from_form(form: dict) -> str:
    plan = form.get("payment_plan")
    if isinstance(plan, dict) and _s(plan.get("code") or plan.get("payment_plan_code")):
        return _s(plan.get("code") or plan.get("payment_plan_code"))
    return _first(form, _PLAN_KEYS)


def _iso_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _s(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _co_applicant_address(form: dict) -> str:
    same = form.get("coApplicantCorrespondenceSameAsPermanent")
    keys = (("coApplicantPermanentAddress", "coApplicantCorrespondenceAddress")
            if same is True or _s(same).lower() in ("true", "1", "yes")
            else ("coApplicantCorrespondenceAddress", "coApplicantPermanentAddress"))
    return _first(form, keys)


class ExportLinks:
    """Indexes admin_export_links() output for per-plot lookups."""

    def __init__(self, links: dict):
        self.payments = {str(p["payment_id"]): p for p in links.get("payments", [])}
        self.forms_by_payment = {}
        for doc in links.get("forms_by_payment", []):        # newest first: keep the first
            self.forms_by_payment.setdefault(str(doc["payment_id"]), doc)
        self.forms_by_owner = {}
        for doc in links.get("forms_by_owner", []):
            self.forms_by_owner.setdefault(str(doc["owner_id"]), []).append(doc)
        self.customers = {str(c["id"]): c for c in links.get("customers", [])}


def buyer_fields(unit: dict, links: ExportLinks) -> dict:
    """The plot's buyer columns; all None for a plot nobody has bought."""
    payment_id = unit.get("booked_payment_id")
    payment = links.payments.get(str(payment_id)) if payment_id else None
    customer_id = (payment or {}).get("customer_id") or unit.get("booked_by")
    empty = {k: None for k in ("payment_plan", "client_name", "phone", "alternative_number", "email",
                               "address", "co_applicant_email", "co_applicant_address", "booking_date")}
    if not payment_id and not customer_id:
        return {**empty, "plot_address": None}

    doc = links.forms_by_payment.get(str(payment_id)) if payment_id else None
    if doc is None and customer_id:
        doc = next((d for d in links.forms_by_owner.get(str(customer_id), []) if form_matches_unit(d, unit)), None)
    form = load_form(doc.get("form_data")) if doc else {}
    customer = links.customers.get(str(customer_id)) if customer_id else None
    customer = customer or {}
    customer_name = _s(f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}")

    booking_date = (_iso_date((payment or {}).get("booking_created_date"))
                    or _iso_date(unit.get("booked_at"))
                    or _iso_date(_first(form, _BOOKING_DATE_KEYS)))
    values = {
        "payment_plan": _plan_from_notes((payment or {}).get("notes")) or _plan_from_form(form),
        "client_name": _first(form, _NAME_KEYS) or customer_name,
        "phone": _first(form, _PHONE_KEYS) or _s(customer.get("phone")),
        "alternative_number": _first(form, _ALT_PHONE_KEYS),
        "email": _first(form, _EMAIL_KEYS) or _s(customer.get("email")),
        "address": customer_address(form),
        "co_applicant_email": _first(form, ("coApplicantEmail",)),
        "co_applicant_address": _co_applicant_address(form),
        "booking_date": booking_date,
        "plot_address": _first(form, _PLOT_ADDRESS_KEYS),
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

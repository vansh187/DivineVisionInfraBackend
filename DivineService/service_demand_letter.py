"""Server-side Demand Letter PDF for a plot booking. Built from the booking
application's stored form_data (which carries the derived payment schedule,
optionally overlaid with live milestone statuses) plus fixed per-project
company / bank details.

Layout follows the client's reference letter ("Payments - Demand Letter.pdf"):
company name + project logo header, customer block / company block, subject
line, the UNPAID instalments only (paid ones are left out of the table, per the
client), then Total Plot / Received / Outstanding / Overdue (+ in words), the
remit sentence, bank details and the interest note.

fpdf2 core font -> amounts use "Rs." (no rupee glyph); never raises out of the
caller's control flow (a render failure yields a short fallback PDF).
"""
import logging
import os
from datetime import date, datetime, timedelta

from fpdf import FPDF

from DivineService.service_payment_schedule import build_payment_schedule
from DivineService.loan_report_data import amount_in_words_indian

logger = logging.getLogger(__name__)

_ASSETS = os.path.join(os.path.dirname(__file__), "assets")

# OPS Divine Greens (KCG Resorts) - unchanged defaults, still env-overridable.
COMPANY = {
    "name": os.getenv("DIVINE_COMPANY_NAME", "KCG Resorts Pvt. Ltd."),
    "project": os.getenv("DIVINE_PROJECT_NAME", "OPS Divine Greens"),
    "email": os.getenv("DIVINE_COMPANY_EMAIL", "sales1@divinevisioninfra.com"),
    "web": os.getenv("DIVINE_COMPANY_WEB", "www.divinevisioninfra.com"),
    "state": "Haryana",
    "state_code": "06",
    "gstin": os.getenv("DIVINE_COMPANY_GSTIN", "06AAECK2303D1Z8"),
    "cin": os.getenv("DIVINE_COMPANY_CIN", "55101HR2009PTC039831"),
    "bank_account": os.getenv("DIVINE_BANK_ACCOUNT", "370305500283"),
    "bank_name": os.getenv("DIVINE_BANK_NAME", "ICICI Bank Ltd."),
    "bank_branch": os.getenv("DIVINE_BANK_BRANCH", "Sector-7, Karnal"),
    "bank_ifsc": os.getenv("DIVINE_BANK_IFSC", "ICIC0003703"),
    "mobiles": os.getenv("DIVINE_COMPANY_MOBILES", "+91-92549 72701, +91-74282 91303"),
    "interest_pa": os.getenv("DIVINE_DELAY_INTEREST_PA", "18"),
    "address_line": os.getenv("DIVINE_PROJECT_ADDRESS",
                              "OPS Divine Greens - Sec-16, Taraori, Karnal, Haryana 132116"),
}

_OPS_PROFILE = {
    "company": COMPANY["name"],
    "project": COMPANY["project"],
    "email": COMPANY["email"],
    "web": COMPANY["web"],
    "state": COMPANY["state"],
    "state_code": COMPANY["state_code"],
    "gstin": COMPANY["gstin"],
    "cin": COMPANY["cin"],
    "bank_account": COMPANY["bank_account"],
    "account_holder": COMPANY["name"].upper(),
    "bank_name": COMPANY["bank_name"],
    "bank_branch": COMPANY["bank_branch"],
    "bank_ifsc": COMPANY["bank_ifsc"],
    "interest_pa": COMPANY["interest_pa"],
    # Subject line already names the project - drop a leading "OPS Divine Greens - ".
    "location": COMPANY["address_line"].split(" - ", 1)[-1],
    "footer": f"E-Mail : {COMPANY['email']} | Mob : {COMPANY['mobiles']}",
    "logo": None,
}

# Suraksha Enclave (Rama Krishna Buildwell) - exactly as on the client's
# reference demand letter. GSTIN check digit verified; the ICICI account number
# starts with the branch code 1651 matching IFSC ICIC0001651.
_SURAKSHA_PROFILE = {
    "company": "Rama Krishna Buildwell Pvt. Ltd.",
    "project": "Suraksha Enclave",
    "email": "sales1@divinevisioninfra.com",
    "web": "https://www.divinevisioninfra.com/",
    "state": "Haryana",
    "state_code": "06",
    "gstin": "06AADCR1976J1Z4",
    "cin": "U45201DL2005PTC140802",
    "bank_account": "165105003309",
    "account_holder": "RAMA KRISHNA BUILDWELL PVT LTD",
    "bank_name": "ICICI Bank",
    "bank_branch": "GURGAON - UNIVERSAL TRADE TOWER, HARYANA",
    "bank_ifsc": "ICIC0001651",
    "interest_pa": "18",
    "location": "Sec-15, Near Railway Coach Factory Barhi, Ganaur, Sonipat, Haryana",
    "footer": ("E-Mail : sales1@divinevisioninfra.com, crm3@divinevisioninfra.com | "
               "Mob : +91-7428291303, 7428291306"),
    "logo": os.path.join(_ASSETS, "suraksha_enclave_logo.png"),
}

_NAME_KEYS = ("applicant_name", "applicantName", "customer_name", "customerName", "name")
_FATHER_KEYS = ("father_name", "fatherName", "guardian_name", "guardianName", "s_o", "so", "care_of", "co")
_ADDRESS_KEYS = ("applicant_address", "applicantAddress", "address", "customer_address",
                 "correspondence_address", "correspondenceAddress",
                 "permanent_address", "permanentAddress")
# The booking form (documentStore.ts) stores the plot as `unitNo`.
_PLOT_KEYS = ("unit_number", "unitNumber", "unit_no", "unitNo",
              "plot_number", "plotNumber", "plot_no", "plotNo")
_AREA_KEYS = ("plot_area_sq_yd", "plotAreaSqYd", "area_sqyd", "areaSqYd", "plot_area", "area")
# The booking form stores the project as `projectId` ("suraksha-enclave" | "ops-divine-greens").
_PROJECT_KEYS = ("project_name", "projectName", "project", "township", "township_name",
                 "township_label", "townshipLabel", "project_id", "projectId")
_LOCATION_KEYS = ("project_location", "projectLocation", "site_address")
_GENDER_KEYS = ("gender", "applicant_gender", "applicantGender")
_BOOKING_DATE_KEYS = ("booking_date", "bookingDate", "date")
_TOTAL_KEYS = ("total_consideration", "totalConsideration", "total_amount", "totalAmount",
               "total_price", "consideration", "sale_value")
_RECEIVED_KEYS = ("amount_received", "amountReceived", "received_amount", "paid_amount")
_SCHEDULE_KEYS = ("payment_schedule", "paymentSchedule", "payment_plan", "schedule", "installments")

PAYABLE_WITHIN_DAYS = 7


def _first(form, keys):
    for k in keys:
        v = form.get(k) if isinstance(form, dict) else None
        if v not in (None, "", []):
            return v
    return None


def _s(v):
    return str(v).strip() if v not in (None, "") else ""


def _num(v):
    try:
        text = str(v).replace(",", "").replace("Rs.", "").replace("Rs", "").replace("₹", "")
        n = float(text.replace("/-", "").strip())
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _indian(n):
    """12,34,567 style grouping, no currency prefix."""
    n = int(round(n or 0))
    neg, n = n < 0, abs(n)
    s = str(n)
    if len(s) <= 3:
        body = s
    else:
        body, s = s[-3:], s[:-3]
        while len(s) > 2:
            body, s = s[-2:] + "," + body, s[:-2]
        if s:
            body = s + "," + body
    return ("-" if neg else "") + body


def _inr(n):
    if n is None:
        return "-"
    return "Rs. " + _indian(n) if n >= 0 else "-Rs. " + _indian(-n)


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    text = _s(v)[:10]
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    return None


def _fmt_date(v):
    d = _to_date(v)
    return d.strftime("%d %b %Y") if d else (_s(v) or "-")


def _latin(v):
    return str(v).encode("latin-1", "replace").decode("latin-1")


def _words(amount):
    """'Rupees Seventeen Lakh ... Only' from the shared Indian-words helper."""
    text = (amount_in_words_indian(amount) or "").strip()
    if text.endswith(" Rupees"):
        text = text[: -len(" Rupees")]
    return f"Rupees {text} Only" if text else "-"


def project_profile(form: dict) -> dict:
    """Company / bank letterhead for the booking's project."""
    # First present key wins - callers put the booking row's project in
    # project_name, ahead of whatever the form snapshot says.
    project = _s(_first(form if isinstance(form, dict) else {}, _PROJECT_KEYS)).lower()
    return _SURAKSHA_PROFILE if "suraksha" in project else _OPS_PROFILE


def _truthy(v) -> bool:
    return v is True or _s(v).lower() in ("true", "1", "yes", "y", "on")


def customer_address(form: dict) -> str:
    """Address the letter is sent to: the correspondence address, unless the
    applicant ticked 'same as permanent' (then the permanent one), one line."""
    if _truthy(form.get("correspondenceSameAsPermanent") or form.get("correspondence_same_as_permanent")):
        keys = ("permanent_address", "permanentAddress") + _ADDRESS_KEYS
    else:
        keys = _ADDRESS_KEYS
    return " ".join(_s(_first(form, keys)).split())


def _is_paid(row: dict) -> bool:
    return _s(row.get("status")).lower() == "paid"


class _Letter(FPDF):
    footer_text = ""

    def footer(self):
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.65)
        self.line(19, 277.6, 184.4, 277.6)
        self.set_y(278.5)
        self.set_font("Helvetica", "", 7.2)
        self.set_text_color(0, 0, 0)
        self.cell(0, 4, _latin(self.footer_text), align="C")


def _fallback_pdf(customer_id: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "DEMAND LETTER", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _latin(
        f"Customer ID: {customer_id}\n\nThis demand letter could not be generated from the "
        f"booking record right now. Please contact {COMPANY['email']} or "
        f"{COMPANY['mobiles']} and our team will share it."
    ))
    return bytes(pdf.output())


def generate_demand_letter_pdf(form_data: dict, customer_id: str, issued_on=None) -> bytes:
    """Never raises - a render failure yields a short fallback notice PDF instead."""
    try:
        return _render_demand_letter_pdf(form_data, customer_id, issued_on)
    except Exception as e:
        logger.warning("demand_letter_render_failed customer_id=%s error=%s", customer_id, e)
        try:
            return _fallback_pdf(customer_id)
        except Exception:  # pragma: no cover - last resort
            return b"%PDF-1.4\n%%EOF\n"


def demand_letter_figures(form: dict, issued: date) -> dict:
    """The numbers the letter prints. Pure; exposed for tests."""
    total = _num(_first(form, _TOTAL_KEYS))
    received = _num(_first(form, _RECEIVED_KEYS))
    rows = _first(form, _SCHEDULE_KEYS)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not rows:
        plan = build_payment_schedule(total, received or 0, _first(form, _BOOKING_DATE_KEYS))
        rows = plan["rows"]
        total = plan["total_receivable"] if total is None else total
        received = plan["total_received"] if received is None else received

    total_plot = total if total is not None else sum((_num(r.get("amount")) or 0) for r in rows)
    total_plot = total_plot or 0
    if received is None:
        received = sum((_num(r.get("amount")) or 0) for r in rows if _is_paid(r))
    outstanding = max(0, total_plot - received)

    if any(_s(r.get("status")) for r in rows):
        # Per-row statuses (live milestones / the derived plan) are authoritative.
        unpaid = [r for r in rows if not _is_paid(r)]
    else:
        # Older snapshots carry no status: treat instalments as settled in due
        # order for as long as the received amount fully covers them.
        unpaid, covered = [], received
        for r in sorted(rows, key=lambda r: _to_date(r.get("due_date")) or date.max):
            amt = _num(r.get("amount")) or 0
            if covered >= amt > 0:
                covered -= amt
            else:
                covered = -1          # stop settling once one instalment isn't covered
                unpaid.append(r)
    overdue = sum((_num(r.get("amount")) or 0) for r in unpaid
                  if (_to_date(r.get("due_date")) or issued) < issued)
    overdue = min(overdue, outstanding)

    # What we ask for: everything overdue, payable within a week; with nothing
    # overdue yet, the next unpaid instalment by its own due date.
    if overdue > 0:
        demand, payable_by = overdue, issued + timedelta(days=PAYABLE_WITHIN_DAYS)
    elif unpaid:
        nxt = min(unpaid, key=lambda r: _to_date(r.get("due_date")) or date.max)
        demand = min(_num(nxt.get("amount")) or 0, outstanding)
        payable_by = _to_date(nxt.get("due_date")) or issued + timedelta(days=PAYABLE_WITHIN_DAYS)
    else:
        demand, payable_by = outstanding, issued + timedelta(days=PAYABLE_WITHIN_DAYS)

    return {
        "rows": unpaid, "total_plot": total_plot, "received": received,
        "outstanding": outstanding, "overdue": overdue,
        "demand": demand, "payable_by": payable_by,
    }


def _render_demand_letter_pdf(form_data: dict, customer_id: str, issued_on=None) -> bytes:
    form = form_data if isinstance(form_data, dict) else {}
    issued = _to_date(issued_on) or date.today()
    prof = project_profile(form)

    name = _s(_first(form, _NAME_KEYS)) or "-"
    gender = _s(_first(form, _GENDER_KEYS)).lower()
    salutation = "Mr." if gender in ("male", "m") else "Ms." if gender in ("female", "f") else ""
    address = customer_address(form)
    plot_no = _s(_first(form, _PLOT_KEYS)) or "-"
    area = _s(_first(form, _AREA_KEYS)) or "-"
    location = _s(_first(form, _LOCATION_KEYS)) or prof["location"]
    fig = demand_letter_figures(form, issued)

    L, R = 19.0, 184.4          # margins as on the reference (54pt / 522.8pt)
    W = R - L
    pdf = _Letter(format="A4")
    pdf.footer_text = prof["footer"]
    pdf.set_margins(L, 12, 210 - R)
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()
    pdf.set_text_color(0, 0, 0)

    # Header: company name left, project logo right, heavy rule underneath.
    if prof["logo"] and os.path.exists(prof["logo"]):
        pdf.image(prof["logo"], x=143.2, y=15.0, h=17.1)
    pdf.set_xy(L, 22)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(115, 8, _latin(prof["company"]))
    if not prof["logo"]:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(R - pdf.get_x(), 8, _latin(prof["project"]), align="R")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.65)
    pdf.line(L, 32.7, R, 32.7)

    pdf.set_xy(L, 37.5)
    pdf.set_font("Helvetica", "BU", 9.6)
    pdf.cell(W, 5, "DEMAND LETTER", align="C")

    rx = 130.5
    pdf.set_xy(rx, 42.5)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(R - rx, 4, _latin(f"Dated: {issued.strftime('%d %b %Y')}"))

    # Customer block (left)
    pdf.set_xy(L, 51.5)
    pdf.set_font("Helvetica", "B", 8)
    pdf.multi_cell(105, 4, _latin(f"{salutation} {name}".strip().upper()))
    if address:
        pdf.set_xy(L, max(pdf.get_y() + 0.8, 56.2))
        pdf.multi_cell(105, 4, _latin(f"R/o-{address}"))
    left_bottom = pdf.get_y()

    # Company block (right)
    pdf.set_font("Helvetica", "", 7.2)
    for y, text in ((51.5, f"Email: {prof['email']}"),
                    (56.2, f"Web: {prof['web']}"),
                    (61.8, f"State: {prof['state']}   State Code: {prof['state_code']}"),
                    (66.5, f"GSTIN: {prof['gstin']}"),
                    (73.8, f"CIN No.: {prof['cin']}")):
        pdf.set_xy(rx, y)
        pdf.cell(R - rx, 4, _latin(text))

    pdf.set_xy(L, max(83.0, left_bottom + 4))
    pdf.set_font("Helvetica", "B", 8)
    pdf.multi_cell(W, 3.6, align="L", text=_latin(
        f'Subject:- Demand against Plot No. {plot_no} having Area {area} Sq.Yrd. in '
        f'"{prof["project"]}" situated at {location}'
    ))
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(W, 3.6, "Dear Sir/Madam,")
    pdf.ln(1)
    pdf.multi_cell(W, 3.4, align="L", text=_latin(
        "This has reference to your booking of above mentioned Plot. This is to inform you that "
        "the following amount stands due as per the payment plan opted by you."
    ))
    pdf.ln(7)

    # Schedule table - unpaid instalments only.
    col = (61.0, 23.0, 26.8, 26.3, W - 137.1)
    lead = col[0] + col[1] + col[2]
    rh = 4.7
    pdf.set_line_width(0.2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(231, 230, 230)
    for w, h in zip(col, ("Particulars", "Head", "Due Date", "Installment", "Total Installment")):
        pdf.cell(w, rh, h, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    if not fig["rows"]:
        pdf.cell(W, rh, "No instalment is pending.", border=1, align="C")
        pdf.ln()
    for r in fig["rows"]:
        amt = _num(r.get("amount")) or 0
        pdf.cell(col[0], rh, _latin(_s(r.get("label")) or "-"), border=1)
        pdf.cell(col[1], rh, _latin(_s(r.get("head")) or "Basic Price"), border=1, align="C")
        pdf.cell(col[2], rh, _latin(_fmt_date(r.get("due_date"))), border=1, align="C")
        pdf.cell(col[3], rh, _indian(amt), border=1, align="R")
        pdf.cell(col[4], rh, _indian(amt), border=1, align="R")
        pdf.ln()

    pdf.set_font("Helvetica", "B", 8)
    for label, value in (("Total Plot Amount", fig["total_plot"]),
                         ("Total Received Amount", fig["received"]),
                         ("Total Outstanding Amount", fig["outstanding"]),
                         ("Total Overdue Amount", fig["overdue"])):
        pdf.cell(lead, rh, label, border=1)
        pdf.cell(W - lead, rh, _indian(value), border=1, align="R")
        pdf.ln()

    # Overdue in words: label bottom-left, highlighted words cell on the right.
    words_text = _latin(_words(fig["overdue"]))
    words_lines = len(pdf.multi_cell(W - lead - 1, 3.6, words_text, dry_run=True, output="LINES"))
    words_h = max(12.4, words_lines * 3.6 + 1.6)
    if pdf.get_y() + words_h > pdf.page_break_trigger:
        pdf.add_page()
    y = pdf.get_y()
    pdf.cell(lead, words_h, "", border=1)
    pdf.set_xy(L, y + words_h - 4.6)
    pdf.cell(lead, 4.6, "Total Overdue Amount (In Words)")
    pdf.set_xy(L + lead, y)
    pdf.set_fill_color(255, 255, 153)
    pdf.cell(W - lead, words_h, "", border=1, fill=True)
    pdf.set_xy(L + lead + 0.5, y + 0.6)
    pdf.set_text_color(0, 0, 255)
    pdf.multi_cell(W - lead - 1, 3.6, words_text, align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(L, y + words_h + 4.5)

    pdf.set_font("Helvetica", "", 8)
    if fig["demand"] > 0:
        remit = (f"You are requested to remit the total dues of Rs. {_indian(fig['demand'])}/- "
                 f"({_words(fig['demand'])}) in favour of \"{prof['company']}\" payable on or before "
                 f"{fig['payable_by'].strftime('%d %b %Y')}.")
    else:
        remit = (f"As on {issued.strftime('%d %b %Y')}, no amount is due against the above "
                 f"mentioned Plot. Thank you for your timely payments.")
    pdf.multi_cell(W, 3.4, align="L", text=_latin(remit))
    pdf.ln(12)

    pdf.set_font("Helvetica", "", 7.2)
    for ln in (f"Bank Account Number: {prof['bank_account']}",
               f"Name of Account Holder: {prof['account_holder']}",
               f"Name of Bank: {prof['bank_name']}",
               f"Branch Name: {prof['bank_branch']}",
               f"IFSC Code: {prof['bank_ifsc']}"):
        pdf.cell(W, 4.6, _latin(ln), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4.5)

    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(W, 3.4, align="L", text=_latin(
        f"Kindly note that in case of non-receipt of due amount within stipulated time, interest "
        f"@{prof['interest_pa']}% p.a. shall be charged as per company's policy on the delayed payments."
    ))
    pdf.ln(6.5)
    pdf.cell(W, 4.6, "Thanking you & assuring you of our best services always.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(W, 4.6, _latin(f"For {prof['company']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(11)
    pdf.set_font("Helvetica", "", 7.2)
    pdf.cell(W, 4, "This is system generated document no signature required.")

    return bytes(pdf.output())

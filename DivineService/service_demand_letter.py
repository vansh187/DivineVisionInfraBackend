"""Server-side Demand Letter PDF for a plot booking. Built from the booking
application's stored form_data (which now carries the derived payment schedule)
plus fixed company / bank details. fpdf2 core font -> amounts use "Rs." (no
rupee glyph); never raises out of the caller's control flow beyond ValueError.
"""
import io
import os
from datetime import date, datetime

from fpdf import FPDF

from DivineService.service_payment_schedule import build_payment_schedule
from DivineService.loan_report_data import amount_in_words_indian

COMPANY = {
    "name": os.getenv("DIVINE_COMPANY_NAME", "KCG Resorts Pvt. Ltd."),
    "project": os.getenv("DIVINE_PROJECT_NAME", "OPS Divine Greens"),
    "email": os.getenv("DIVINE_COMPANY_EMAIL", "crm2@divinevisioninfra.com"),
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

_NAME_KEYS = ("applicant_name", "applicantName", "customer_name", "customerName", "name")
_FATHER_KEYS = ("father_name", "fatherName", "s_o", "so", "guardian_name", "care_of", "co")
_ADDRESS_KEYS = ("applicant_address", "applicantAddress", "address", "customer_address", "correspondence_address")
_PLOT_KEYS = ("unit_number", "unitNumber", "plot_number", "plotNumber", "plot_no", "plotNo")
_AREA_KEYS = ("plot_area_sq_yd", "plotAreaSqYd", "area_sqyd", "areaSqYd", "plot_area", "area")
_PROJECT_KEYS = ("project_name", "projectName", "project", "township", "township_name")
_LOCATION_KEYS = ("project_location", "projectLocation", "site_address", "location")
_BOOKING_DATE_KEYS = ("booking_date", "bookingDate", "date")
_TOTAL_KEYS = ("total_consideration", "totalConsideration", "total_amount", "totalAmount",
               "total_price", "consideration", "sale_value")
_RECEIVED_KEYS = ("amount_received", "amountReceived", "received_amount", "paid_amount")
_SCHEDULE_KEYS = ("payment_schedule", "paymentSchedule", "payment_plan", "schedule", "installments")


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
        return float(str(v).replace(",", "").replace("Rs.", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def _inr(n):
    if n is None:
        return "-"
    n = int(round(n))
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
    return ("-" if neg else "") + "Rs. " + body


def _fmt_date(v):
    if isinstance(v, (date, datetime)):
        d = v.date() if isinstance(v, datetime) else v
        return d.strftime("%d %b %Y")
    text = _s(v)
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, f).strftime("%d %b %Y")
        except ValueError:
            continue
    return text or "-"


def _latin(v):
    return str(v).encode("latin-1", "replace").decode("latin-1")


class _Letter(FPDF):
    def footer(self):
        self.set_y(-16)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4, _latin(f"E-Mail : {COMPANY['email']}  |  Mob : {COMPANY['mobiles']}"),
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, _latin(COMPANY["address_line"]), align="C")


def generate_demand_letter_pdf(form_data: dict, customer_id: str, issued_on=None) -> bytes:
    form = form_data if isinstance(form_data, dict) else {}
    issued = issued_on or date.today()
    issued_str = issued.strftime("%d %b %Y") if isinstance(issued, (date, datetime)) else _s(issued)

    project = _s(_first(form, _PROJECT_KEYS)) or COMPANY["project"]
    name = _s(_first(form, _NAME_KEYS)) or "-"
    father = _s(_first(form, _FATHER_KEYS))
    address = _s(_first(form, _ADDRESS_KEYS))
    plot_no = _s(_first(form, _PLOT_KEYS)) or "-"
    area = _s(_first(form, _AREA_KEYS)) or "-"
    location = _s(_first(form, _LOCATION_KEYS)) or COMPANY["address_line"]
    booking_date = _first(form, _BOOKING_DATE_KEYS)

    total = _num(_first(form, _TOTAL_KEYS))
    received = _num(_first(form, _RECEIVED_KEYS))
    rows = _first(form, _SCHEDULE_KEYS)
    if not (isinstance(rows, list) and rows):
        plan = build_payment_schedule(total, received or 0, booking_date)
        rows = plan["rows"]
        total = plan["total_receivable"] if total is None else total
        received = plan["total_received"] if received is None else received

    total_receivable = total if total is not None else sum((_num(r.get("amount")) or 0) for r in rows)
    total_received = received if received is not None else 0
    outstanding = (total_receivable or 0) - (total_received or 0)

    pdf = _Letter(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(18, 16, 18)

    # Header
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(120, 10, _latin(COMPANY["name"].upper()))
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(60, 90, 60)
    pdf.cell(0, 10, _latin(project), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(30, 30, 30)
    pdf.set_line_width(0.6)
    pdf.line(18, pdf.get_y() + 1, 192, pdf.get_y() + 1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "BU", 13)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 8, "DEMAND LETTER", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Customer (left) + company (right) blocks
    y0 = pdf.get_y()
    pdf.set_font("Helvetica", "B", 10)
    pdf.multi_cell(95, 5, _latin(name), new_x="RIGHT", new_y="TOP")
    pdf.set_xy(18, pdf.get_y() + 5)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(70, 70, 70)
    cust_lines = []
    if father:
        cust_lines.append(f"S/O {father}")
    if address:
        cust_lines.append(address)
    cust_lines.append(f"Customer ID: {customer_id}")
    pdf.multi_cell(95, 4.5, _latin("\n".join(cust_lines)))

    pdf.set_xy(115, y0)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 4.5, _latin(f"Dated: {issued_str}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(115)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(70, 70, 70)
    for ln in (
        COMPANY["name"],
        f"Email: {COMPANY['email']}", f"Web: {COMPANY['web']}",
        f"State: {COMPANY['state']}   State Code: {COMPANY['state_code']}",
        f"GSTIN: {COMPANY['gstin']}", f"CIN No.: {COMPANY['cin']}",
    ):
        pdf.set_x(115)
        pdf.cell(0, 4.3, _latin(ln), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.set_text_color(20, 20, 20)
    pdf.multi_cell(0, 5, _latin(
        f'Subject:- Demand against Plot No. {plot_no} having Area {area} Sq.Yrd. in "{project}" '
        f"situated at {location}"
    ))
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(50, 50, 50)
    pdf.multi_cell(0, 5, _latin(
        "Dear Sir/Madam,\nThis has reference to your booking of the above mentioned Plot. This is to "
        "inform you that the following amount stands due as per the payment plan opted by you."
    ))
    pdf.ln(3)

    # Schedule table: Particulars | Head | Due Date | Installment | Total Installment  (sum 174)
    col = (60, 22, 28, 32, 32)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(235, 238, 240)
    pdf.set_text_color(20, 20, 20)
    for w, h in zip(col, ("Particulars", "Head", "Due Date", "Installment", "Total Installment")):
        pdf.cell(w, 7, h, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    running = 0
    for i, r in enumerate(rows):
        amt = _num(r.get("amount")) or 0
        running += amt
        pdf.cell(col[0], 6.5, _latin(_s(r.get("label")) or "-"), border=1)
        pdf.cell(col[1], 6.5, _latin("Basic Price" if i == 0 else ""), border=1, align="C")
        pdf.cell(col[2], 6.5, _latin(_fmt_date(r.get("due_date"))), border=1, align="C")
        pdf.cell(col[3], 6.5, _latin(_inr(amt)), border=1, align="R")
        pdf.cell(col[4], 6.5, _latin(_inr(running)), border=1, align="R")
        pdf.ln()

    lead = col[0] + col[1] + col[2] + col[3]

    def _total_row(label, value, in_words=False):
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(lead, 7, _latin(label), border=1)
        pdf.set_font("Helvetica", "" if in_words else "B", 7.5 if in_words else 8)
        pdf.cell(col[4], 7, _latin(value if in_words else _inr(value)),
                 border=1, align="L" if in_words else "R")
        pdf.ln()

    _total_row("Total Receivable Amount", total_receivable)
    _total_row("Total Received Amount", total_received)
    _total_row("Total Outstanding Amount", outstanding)
    _total_row("Total Outstanding Amount (In Words)",
               amount_in_words_indian(outstanding) or "-", in_words=True)
    pdf.ln(9)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(50, 50, 50)
    pdf.multi_cell(0, 5, _latin(
        f'You are requested to remit the dues as per the payment plan opted by you in favour of '
        f'"{COMPANY["name"]}" as and when they fall due.'
    ))
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 8.5)
    for ln in (
        f"Bank Account Number: {COMPANY['bank_account']}",
        f"Name of Account Holder: {COMPANY['name']}",
        f"Name of Bank: {COMPANY['bank_name']}",
        f"Branch Name: {COMPANY['bank_branch']}",
        f"IFSC Code: {COMPANY['bank_ifsc']}",
    ):
        pdf.cell(0, 4.5, _latin(ln), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.multi_cell(0, 5, _latin(
        f"Kindly note that in case of non-receipt of due amount within stipulated time, interest "
        f"@{COMPANY['interest_pa']} % p.a. shall be charged as per company's policy on the delayed payments."
    ))
    pdf.ln(3)
    pdf.multi_cell(0, 5, "Thanking you & assuring you of our best services always.")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, _latin(f"For {COMPANY['name']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, "This is a system generated document, no signature required.")

    return bytes(pdf.output())

"""Server-rendered payment receipt / slip PDF for a settled payment (booking
amount or an instalment). Mirrors service_demand_letter: fpdf2 core font, "Rs."
money, and a try/except wrapper that yields a short fallback notice PDF instead
of ever raising into the caller.
"""
import io
import logging
import os
from datetime import date, datetime

from fpdf import FPDF

from DivineService.loan_report_data import amount_in_words_indian

logger = logging.getLogger(__name__)

_COMPANY = {
    "name": os.getenv("DIVINE_COMPANY_NAME", "KCG Resorts Pvt. Ltd."),
    "project": os.getenv("DIVINE_PROJECT_NAME", "OPS Divine Greens"),
    "email": os.getenv("DIVINE_COMPANY_EMAIL", "sales1@divinevisioninfra.com"),
    "mobiles": os.getenv("DIVINE_COMPANY_MOBILES", "+91-92549 72701, +91-74282 91303"),
    "web": os.getenv("DIVINE_COMPANY_WEB", "www.divinevisioninfra.com"),
    "address_line": os.getenv("DIVINE_PROJECT_ADDRESS",
                              "OPS Divine Greens - Sec-16, Taraori, Karnal, Haryana 132116"),
}


def _num(value):
    try:
        n = float(str(value).replace(",", "").replace("Rs.", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None
    return None if n != n else n


def _inr(n):
    n = _num(n)
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
    text = str(v or "").strip()
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:len("%Y-%m-%dT%H:%M:%S") if "T" in text else len(text)], f).strftime("%d %b %Y")
        except ValueError:
            continue
    return text[:10] or "-"


def _latin(v):
    return str(v if v is not None else "").encode("latin-1", "replace").decode("latin-1")


def _get(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict) and n in obj and obj[n] not in (None, ""):
            return obj[n]
        v = getattr(obj, n, None)
        if v not in (None, ""):
            return v
    return default


class _Receipt(FPDF):
    def footer(self):
        self.set_y(-16)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4, _latin("This is a system-generated receipt and does not require a signature."),
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, _latin(f"E-Mail : {_COMPANY['email']}  |  Mob : {_COMPANY['mobiles']}"), align="C")


def receipt_filename(payment_id: str) -> str:
    pid = str(payment_id or "receipt").replace("/", "_")[:24]
    return f"payment-receipt-{pid}.pdf"


def _fallback_pdf(payment_id: str) -> bytes:
    try:
        pdf = FPDF(format="A4")
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "PAYMENT RECEIPT", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _latin(
            f"Payment reference: {payment_id}\n\nYour payment has been received. A detailed "
            f"receipt could not be rendered right now - please contact {_COMPANY['email']} "
            f"or {_COMPANY['mobiles']} and our team will share it."
        ))
        return bytes(pdf.output())
    except Exception:  # pragma: no cover - last resort
        return b"%PDF-1.4\n%%EOF\n"


def generate_receipt_pdf(payment, milestone=None, customer=None, issued_on=None) -> bytes:
    """Never raises. `payment` / `milestone` / `customer` may be dicts or row objects."""
    payment_id = _get(payment, "id", default="")
    try:
        return _render(payment, milestone, customer, issued_on)
    except Exception as e:
        logger.warning("payment_receipt_render_failed payment_id=%s error=%s", payment_id, e)
        return _fallback_pdf(payment_id)


def _render(payment, milestone, customer, issued_on) -> bytes:
    amount = _num(_get(payment, "amount")) or 0
    method = str(_get(payment, "method", default="razorpay")).lower()
    method_label = {"cash": "Cash", "razorpay": "Online (Razorpay)"}.get(method, method.title() or "-")
    reference = _get(payment, "razorpay_payment_id", "razorpay_order_id", default=None) or _get(payment, "id", default="-")
    paid_on = issued_on or _get(payment, "paid_on", "created_date") or date.today()

    name = _get(customer, "first_name", "full_name", "name", default=None)
    last = _get(customer, "last_name", default=None)
    if name and last:
        name = f"{name} {last}"
    name = name or _get(payment, "owner_id", default="Customer")

    purpose = str(_get(payment, "purpose", default="other")).lower()
    if milestone is not None:
        m_label = _get(milestone, "label", default="Instalment")
        m_no = _get(milestone, "milestone_no", default=None)
        for_line = f"{m_label}" + (f" (Instalment {int(m_no)})" if m_no not in (None, "") else "")
        project = _get(milestone, "project_id", default=None) or _COMPANY["project"]
    elif purpose == "plot_booking":
        for_line = "Plot booking amount"
        project = _get(payment, "project_id", default=None) or _COMPANY["project"]
    else:
        for_line = "Payment"
        project = _COMPANY["project"]

    pdf = _Receipt(format="A4")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 8, _latin(_COMPANY["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, _latin(_COMPANY["address_line"]), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 9, "PAYMENT RECEIPT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    rows = [
        ("Receipt No.", str(_get(payment, "id", default="-"))),
        ("Date", _fmt_date(paid_on)),
        ("Received From", _latin(name)),
        ("Project", _latin(project)),
        ("Payment For", _latin(for_line)),
        ("Payment Mode", method_label),
        ("Reference", _latin(reference)),
    ]
    pdf.set_font("Helvetica", "", 10)
    for label, value in rows:
        pdf.set_text_color(110, 110, 110)
        pdf.cell(45, 7, _latin(label), new_x="RIGHT", new_y="TOP")
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(45, 10, "Amount Paid", border=0, fill=True, new_x="RIGHT", new_y="TOP")
    pdf.cell(0, 10, _latin(_inr(amount)), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")

    words = amount_in_words_indian(int(round(amount))) if amount else None
    if words:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(0, 6, _latin(f"({words})"), new_x="LMARGIN", new_y="NEXT")

    if milestone is not None:
        received = _num(_get(milestone, "_amount_received_after", default=None))
        outstanding = _num(_get(milestone, "_outstanding_after", default=None))
        if received is not None or outstanding is not None:
            pdf.ln(2)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(110, 110, 110)
            if received is not None:
                pdf.cell(0, 5, _latin(f"Total received so far: {_inr(received)}"),
                         new_x="LMARGIN", new_y="NEXT")
            if outstanding is not None:
                pdf.cell(0, 5, _latin(f"Balance outstanding: {_inr(outstanding)}"),
                         new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())

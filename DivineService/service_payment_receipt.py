"""Server-rendered payment receipt / slip PDF for a settled payment (booking
amount or an instalment). Layout matches the client-approved reference receipt
(logo + company header, PAYMENT RECEIPT title, amount-received panel, Received
From block, Payment-for table). Mirrors service_demand_letter: fpdf2 core font,
"Rs." money, and a try/except wrapper that yields a short fallback notice PDF
instead of ever raising into the caller.
"""
import logging
import os
from datetime import date, datetime
from pathlib import Path

from fpdf import FPDF

from DivineService.loan_report_data import amount_in_words_indian
from DivineService.service_demand_letter import _first, _ADDRESS_KEYS, _AREA_KEYS, _PLOT_KEYS

logger = logging.getLogger(__name__)

_COMPANY = {
    "name": os.getenv("DIVINE_COMPANY_NAME", "KCG Resorts Pvt. Ltd."),
    "project": os.getenv("DIVINE_PROJECT_NAME", "OPS Divine Greens"),
    "email": os.getenv("DIVINE_COMPANY_EMAIL", "sales1@divinevisioninfra.com"),
    "mobiles": os.getenv("DIVINE_COMPANY_MOBILES", "+91-92549 72701, +91-74282 91303"),
    "web": os.getenv("DIVINE_COMPANY_WEB", "www.divinevisioninfra.com"),
    "gstin": os.getenv("DIVINE_COMPANY_GSTIN", "06AAECK2303D1Z8"),
    "address_line": os.getenv("DIVINE_PROJECT_ADDRESS",
                              "OPS Divine Greens - Sec-16, Taraori, Karnal, Haryana 132116"),
}

# Shared brand asset - same file service_email.py embeds in emails. Override with
# DIVINE_LOGO_PATH the same way that module does, so both stay in sync from one env var.
_DEFAULT_LOGO_PATH = str(Path(__file__).resolve().parent / "assets" / "divine_logo.png")

_AMOUNT_PANEL_RGB = (96, 153, 61)   # green "Amount Received" panel
_LABEL_RGB = (120, 120, 120)
_INK_RGB = (20, 20, 20)
_RULE_RGB = (210, 210, 210)
_TABLE_HEADER_FILL = (243, 243, 243)


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
    neg = n < 0
    n = abs(n)
    whole = int(n)
    paise = round((n - whole) * 100)
    s = str(whole)
    if len(s) <= 3:
        body = s
    else:
        body, s = s[-3:], s[:-3]
        while len(s) > 2:
            body, s = s[-2:] + "," + body, s[:-2]
        if s:
            body = s + "," + body
    suffix = f".{paise:02d}" if paise else ""
    return ("-" if neg else "") + "Rs." + body + suffix


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


def _words_no_rupees_suffix(n: int) -> str:
    words = amount_in_words_indian(n)
    return words[: -len(" Rupees")] if words.endswith(" Rupees") else words


def _amount_words_line(amount) -> str:
    """'Indian Rupee Three Thousand Seven Hundred Sixty-Three and Fifty-Eight
    Paise Only' - amount_in_words_indian() only does whole rupees and always
    appends its own ' Rupees' suffix, so the whole/paise parts are composed
    separately here rather than reusing its suffix."""
    n = _num(amount)
    if not n:
        return "-"
    whole = int(n)
    paise = int(round((n - whole) * 100))
    words = _words_no_rupees_suffix(whole) if whole else "Zero"
    if paise:
        paise_words = _words_no_rupees_suffix(paise)
        return f"Indian Rupee {words} and {paise_words} Paise Only"
    return f"Indian Rupee {words} Only"


class _Receipt(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_LABEL_RGB)
        self.cell(0, 5, str(self.page_no()), align="R")


def receipt_filename(payment_id: str) -> str:
    pid = str(payment_id or "receipt").replace("/", "_")[:24]
    return f"payment-receipt-{pid}.pdf"


def _invoice_number(payment_id: str) -> str:
    """No standalone invoicing system exists - this payment/instalment IS the
    invoice line, so a short friendly reference is derived from the payment id
    rather than inventing a real invoice sequence."""
    tail = str(payment_id or "").replace("-", "")[-6:].upper() or "000000"
    return f"INV-{tail}"


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


def generate_receipt_pdf(payment, milestone=None, customer=None, issued_on=None,
                          booking=None, form_data: dict = None) -> bytes:
    """Never raises. `payment` / `milestone` / `customer` / `booking` may be dicts
    or row objects. `form_data` is the booking-application's stored form data (used
    for Plot Number / Area / Address) - optional, the fields simply blank without it."""
    payment_id = _get(payment, "id", default="")
    try:
        return _render(payment, milestone, customer, issued_on, booking, form_data or {})
    except Exception as e:
        logger.warning("payment_receipt_render_failed payment_id=%s error=%s", payment_id, e)
        return _fallback_pdf(payment_id)


def _render(payment, milestone, customer, issued_on, booking, form_data: dict) -> bytes:
    amount = _num(_get(payment, "amount")) or 0
    method = str(_get(payment, "method", default="zoho")).lower()
    method_label = {"cash": "Cash", "zoho": "Online (Zoho Payments)", "razorpay": "Online (Razorpay)",
                     "rtgs_neft": "RTGS / NEFT"}.get(method, method.replace("_", " ").title() or "-")
    paid_on = issued_on or _get(payment, "paid_on", "created_date") or date.today()

    name = _get(customer, "first_name", "full_name", "name", default=None)
    last = _get(customer, "last_name", default=None)
    if name and last:
        name = f"{name} {last}"
    name = name or _get(payment, "owner_id", default="Customer")
    phone = _get(customer, "phone", default="-")

    plot_number = _get(booking, "unit_number", default=None) or _first(form_data, _PLOT_KEYS) or "-"
    area = _first(form_data, _AREA_KEYS) or "-"
    address = _first(form_data, _ADDRESS_KEYS) or "-"

    payment_id = _get(payment, "id", default="-")
    invoice_number = _invoice_number(payment_id)
    words_line = _amount_words_line(amount)

    pdf = _Receipt(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    left, right = 18, 192
    content_w = right - left

    # ---- Header: logo (left) + company block (right) ---------------------
    logo_path = os.getenv("DIVINE_LOGO_PATH") or _DEFAULT_LOGO_PATH
    header_top = 16
    company_x = left
    if os.path.isfile(logo_path):
        try:
            pdf.image(logo_path, x=left, y=header_top, w=46)
            company_x = left + 46 + 8
        except Exception as e:  # a corrupt/unreadable logo must not break the receipt
            logger.warning("payment_receipt_logo_embed_failed path=%s error=%s", logo_path, e)

    pdf.set_xy(company_x, header_top)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_INK_RGB)
    pdf.cell(right - company_x, 7, _latin(_COMPANY["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(company_x)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_LABEL_RGB)
    pdf.multi_cell(right - company_x, 4.5, _latin(_COMPANY["address_line"]))
    pdf.set_x(company_x)
    pdf.cell(right - company_x, 4.5, _latin(f"GSTIN: {_COMPANY['gstin']}"), new_x="LMARGIN", new_y="NEXT")

    header_bottom = max(pdf.get_y(), header_top + 46 * 66 / 298)
    pdf.set_y(header_bottom + 4)
    pdf.set_draw_color(*_RULE_RGB)
    pdf.set_line_width(0.3)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())
    pdf.ln(8)

    # ---- Title --------------------------------------------------------------
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(*_INK_RGB)
    pdf.cell(content_w, 8, "PAYMENT RECEIPT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # ---- Two-column body: fields (left) + amount panel (right) -------------
    panel_w = 56
    fields_w = content_w - panel_w - 10
    body_top = pdf.get_y()

    def _field(label, value, multiline=False):
        pdf.set_x(left)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_LABEL_RGB)
        pdf.cell(fields_w, 5, _latin(label), new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(left)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*_INK_RGB)
        if multiline:
            pdf.multi_cell(fields_w, 6, _latin(value))
        else:
            pdf.cell(fields_w, 6, _latin(value), new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(left)
        pdf.set_draw_color(*_RULE_RGB)
        pdf.line(left, pdf.get_y() + 1, left + fields_w, pdf.get_y() + 1)
        pdf.ln(5)

    _field("Payment Received Date", _fmt_date(paid_on))
    _field("Payment Mode", method_label)
    _field("Amount Received In Words", words_line, multiline=True)
    fields_bottom = pdf.get_y()

    panel_x = left + fields_w + 10
    panel_h = max(fields_bottom - body_top, 30)
    pdf.set_fill_color(*_AMOUNT_PANEL_RGB)
    pdf.rect(panel_x, body_top, panel_w, panel_h, style="F")
    pdf.set_xy(panel_x, body_top + panel_h / 2 - 10)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(panel_w, 6, "Amount Received", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(panel_x)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(panel_w, 8, _latin(_inr(amount)), align="C")

    pdf.set_y(max(fields_bottom, body_top + panel_h) + 8)

    # ---- Received From --------------------------------------------------
    pdf.set_x(left)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_LABEL_RGB)
    pdf.cell(content_w, 5, "Received From", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    pdf.set_x(left)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*_INK_RGB)
    pdf.cell(content_w, 6, _latin(f"Name: {name}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    for label, value in (("Phone", phone), ("Plot Number", plot_number), ("Area", area)):
        pdf.set_x(left)
        pdf.cell(content_w, 5.5, _latin(f"{label}: {value}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(left)
    pdf.multi_cell(content_w, 5.5, _latin(f"Address: {address}"))
    pdf.ln(4)

    pdf.set_x(left)
    pdf.set_draw_color(*_RULE_RGB)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())
    pdf.ln(7)

    # ---- Payment for table --------------------------------------------------
    pdf.set_x(left)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_INK_RGB)
    pdf.cell(content_w, 7, "Payment for", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    col_w = (content_w * 0.24, content_w * 0.24, content_w * 0.26, content_w * 0.26)
    headers = ("Invoice Number", "Invoice Date", "Invoice Amount", "Payment Amount")
    pdf.set_x(left)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_LABEL_RGB)
    pdf.set_fill_color(*_TABLE_HEADER_FILL)
    for w, h in zip(col_w, headers):
        pdf.cell(w, 8, _latin(h), fill=True)
    pdf.ln(9)

    pdf.set_x(left)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_INK_RGB)
    row_values = (invoice_number, _fmt_date(paid_on), _inr(amount), _inr(amount))
    for w, v in zip(col_w, row_values):
        pdf.cell(w, 8, _latin(v))
    pdf.ln(10)
    pdf.set_x(left)
    pdf.set_draw_color(*_RULE_RGB)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())

    # ---- Instalment running totals (not in the reference layout, but useful
    # context specific to our instalment plans - kept below the table so the
    # core receipt above still matches the client-approved design exactly). ----
    if milestone is not None:
        received = _num(_get(milestone, "_amount_received_after", default=None))
        outstanding = _num(_get(milestone, "_outstanding_after", default=None))
        if received is not None or outstanding is not None:
            pdf.ln(4)
            pdf.set_x(left)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*_LABEL_RGB)
            if received is not None:
                pdf.set_x(left)
                pdf.cell(content_w, 5, _latin(f"Total received so far: {_inr(received)}"), new_x="LMARGIN", new_y="NEXT")
            if outstanding is not None:
                pdf.set_x(left)
                pdf.cell(content_w, 5, _latin(f"Balance outstanding: {_inr(outstanding)}"), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())

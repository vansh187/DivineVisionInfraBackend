"""Server-side Provisional Allotment Letter PDF for a booked plot - the admin
Booking Detail screen's "Allotment Letter" download. A straight port of the
customer site's client-side generateAllotmentLetterPdf
(react-landing/src/services/customerProfilePdf.ts) so both sides hand out the
same letter: same branded header, allottee + plot details, RERA paragraph,
signatory block and indicative payment schedule. Built on fpdf2 in points with
the same coordinates; core Helvetica has no rupee glyph, so amounts use "Rs.".
"""
import logging
from datetime import date, datetime

from fpdf import FPDF

from DivineService.service_demand_letter import _fmt_date, _inr, _latin, _num, _s

logger = logging.getLogger(__name__)

_PAGE_W, _PAGE_H = 595.28, 841.89
_MARGIN = 56
_LABEL_COL = 150
_BOTTOM_LIMIT = _PAGE_H - 48

_CHROME = (44, 62, 80)
_INK = (13, 31, 41)
_MUTED = (89, 115, 128)
_HAIRLINE = (204, 217, 224)
_HEADER_SUBTLE = (217, 227, 235)

_TERMS = (
    "This provisional allotment is subject to realisation of the booking amount and timely payment "
    "of all instalments as per the payment schedule. The allotment may be cancelled and earnest money "
    "forfeited as per applicable RERA rules in case of breach. This letter does not by itself create "
    "any right, title or interest in the plot until the Agreement for Sale is executed and registered."
)


class _Letter:
    """Top-down cursor over one fpdf2 page set; ``y`` is the next text baseline."""

    def __init__(self):
        self.pdf = FPDF(unit="pt", format="A4")
        self.pdf.set_auto_page_break(False)
        self.pdf.add_page()
        self.y = 0.0

    def _font(self, size, bold=False, color=_INK):
        self.pdf.set_font("Helvetica", "B" if bold else "", size)
        self.pdf.set_text_color(*color)

    def _draw(self, x, value, size, bold=False, color=_INK):
        self._font(size, bold, color)
        self.pdf.text(x, self.y, _latin(value))

    def _ensure_room(self, needed):
        if self.y + needed > _BOTTOM_LIMIT:
            self.pdf.add_page()
            self.y = _MARGIN

    def _wrap(self, value, size, max_width, bold=False):
        self._font(size, bold)
        lines, line = [], ""
        for word in _latin(value).split():
            candidate = f"{line} {word}" if line else word
            if line and self.pdf.get_string_width(candidate) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines or [""]

    def header(self, kicker, issued):
        self.pdf.set_fill_color(*_CHROME)
        self.pdf.rect(0, 0, _PAGE_W, 96, style="F")
        self.y = 44
        self._draw(_MARGIN, "DIVINE VISION INFRA", 16, bold=True, color=(255, 255, 255))
        self.y = 66
        self._draw(_MARGIN, kicker, 9, color=_HEADER_SUBTLE)
        self.y = 82
        self._draw(_MARGIN, f"Issued {issued}", 9, color=_HEADER_SUBTLE)
        self.y = 140

    def text(self, value, size=10.5, bold=False, color=_INK, gap=None):
        self._ensure_room(size + 8)
        self._draw(_MARGIN, value, size, bold, color)
        self.y += gap if gap is not None else size + 8

    def paragraph(self, value, size=10.5):
        for line in self._wrap(value, size, _PAGE_W - _MARGIN * 2):
            self._ensure_room(size + 5)
            self._draw(_MARGIN, line, size)
            self.y += size + 5
        self.y += 6

    def info_row(self, label, value):
        lines = self._wrap(value or "-", 10.5, _PAGE_W - _MARGIN * 2 - _LABEL_COL)
        self._ensure_room(15 * len(lines))
        self._draw(_MARGIN, label, 10, bold=True, color=_MUTED)
        for line in lines:
            self._draw(_MARGIN + _LABEL_COL, line, 10.5)
            self.y += 15
        self.y += 4

    def signatory(self):
        self._ensure_room(60)
        self.y += 24
        self.pdf.set_draw_color(*_HAIRLINE)
        self.pdf.set_line_width(0.75)
        self.pdf.line(_MARGIN, self.y, _MARGIN + 180, self.y)
        self.y += 14
        self.text("For Divine Vision Infra", size=9.5, color=_MUTED)
        self.text("Authorised Signatory", size=9.5, color=_MUTED)

    def schedule_row(self, lead, label, amount, bold=False):
        self._ensure_room(14)
        amount_x = _PAGE_W - _MARGIN - 90
        if lead:
            self._draw(_MARGIN, lead, 9, bold=True, color=_MUTED)
        self._draw(_MARGIN + 40, label, 9, bold=bold)
        self._draw(amount_x, amount, 9, bold=bold)
        self.y += 14

    def output(self) -> bytes:
        return bytes(self.pdf.output())


def _long_date(value) -> str:
    """"15 October 2026" - the en-IN long form the customer site prints."""
    d = value.date() if isinstance(value, datetime) else value
    return d.strftime("%d %B %Y") if isinstance(d, date) else _s(value)


def _percent_text(row, index) -> str:
    percent = _num(row.get("percent"))
    return f"{round(percent)}%" if percent is not None else str(index + 1)


def _render(data: dict, issued_on) -> bytes:
    name = _s(data.get("name"))
    township = _s(data.get("township"))
    rows = [r for r in (data.get("schedule") or []) if isinstance(r, dict)]
    total = _num(data.get("total_amount"))

    letter = _Letter()
    letter.header("Provisional Allotment Letter", _long_date(issued_on or date.today()))
    letter.text("Provisional Allotment Letter", size=18, bold=True, gap=28)
    letter.paragraph(
        f"Dear {name or 'Applicant'}, we are pleased to confirm the provisional allotment of the "
        f"residential plot detailed below in {township or 'our township'}, subject to the terms and "
        f"conditions of the booking application form and the Agreement for Sale."
    )

    letter.text("Allottee details", size=12, bold=True, gap=20)
    letter.info_row("Name", name)
    letter.info_row("Gender", _s(data.get("gender")))
    letter.info_row("Address", _s(data.get("address")))
    letter.info_row("Email", _s(data.get("email")))
    letter.info_row("Phone", _s(data.get("phone")))
    letter.y += 8

    letter.text("Plot details", size=12, bold=True, gap=20)
    letter.info_row("Township", township)
    letter.info_row("Plot / Unit no.", _s(data.get("unit_no")))
    letter.info_row("Plot area", _s(data.get("plot_area")))
    letter.info_row("Unit type", _s(data.get("unit_type")))
    letter.info_row("Total consideration", _inr(total) if total else "As per price list")
    letter.y += 12

    letter.paragraph(_TERMS, size=9.5)
    letter.signatory()

    if rows:
        letter.y += 18
        letter.text("Indicative payment schedule", size=11, bold=True, gap=16)
        cumulative = 0
        for i, row in enumerate(rows):
            amount = _num(row.get("amount")) or 0
            cumulative += amount
            letter.schedule_row(
                _percent_text(row, i),
                f"{_s(row.get('label')) or '-'}  (by {_fmt_date(row.get('due_date'))})",
                _inr(amount) if amount > 0 else "-",
            )
        if total or cumulative:
            letter.schedule_row("", "Total", _inr(total or cumulative), bold=True)

    return letter.output()


def _fallback_pdf() -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "PROVISIONAL ALLOTMENT LETTER", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, "This allotment letter could not be generated from the booking record right now. "
                         "Please try again shortly.")
    return bytes(pdf.output())


def generate_allotment_letter_pdf(data: dict, issued_on=None) -> bytes:
    """``data`` keys: name, gender, address, email, phone, township, unit_no,
    plot_area, unit_type, total_amount, schedule (list of {label, percent,
    due_date, amount}). Never raises - a render failure yields a short
    fallback notice PDF instead, same contract as generate_demand_letter_pdf."""
    try:
        return _render(data if isinstance(data, dict) else {}, issued_on)
    except Exception as e:
        logger.warning("allotment_letter_render_failed error=%s", e)
        try:
            return _fallback_pdf()
        except Exception:  # pragma: no cover - last resort
            return b"%PDF-1.4\n%%EOF\n"

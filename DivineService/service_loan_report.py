"""Builds the downloadable Home Loan Eligibility Summary PDF from a loan-profile
snapshot dict. Rendered on demand from the stored JSON snapshot - the PDF bytes
themselves are never persisted, so template changes never require regenerating
old reports.
"""
from datetime import datetime, timezone

from fpdf import FPDF

from DivineService.loan_knowledge import get_document_checklist

DISCLAIMER = (
    "This report is an indicative financial-assistance summary based solely on information "
    "provided by the user. It is not a loan sanction, credit decision, financial advice, or "
    "guarantee of approval. Actual loan eligibility, interest rates, documents and approval "
    "depend on the lender's policies and verification."
)


def _fmt_currency(value) -> str:
    if value is None:
        return "-"
    return f"Rs. {value:,.0f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{value}%"


class _ReportPDF(FPDF):
    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(20, 20, 20)
        self.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(200, 200, 200)
        self.line(self.get_x(), self.get_y(), self.get_x() + 190, self.get_y())
        self.ln(3)

    def field_row(self, label: str, value: str):
        self.set_font("Helvetica", "", 11)
        self.set_text_color(60, 60, 60)
        self.cell(70, 7, label)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(20, 20, 20)
        self.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    def bullet(self, text: str):
        self.set_font("Helvetica", "", 10.5)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 6, f"- {text}", new_x="LMARGIN", new_y="NEXT")


def generate_eligibility_report_pdf(snapshot: dict) -> bytes:
    pdf = _ReportPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(15, 15, 15)
    pdf.cell(0, 12, "Home Loan Eligibility Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Generated on {datetime.now(timezone.utc).strftime('%d %b %Y')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    profile = snapshot.get("profile") or {}
    calculation = snapshot.get("last_calculation") or {}
    eligibility = calculation.get("eligibility") or {}
    affordability = calculation.get("affordability") or {}
    emi_result = calculation.get("emi") or {}

    pdf.section_title("Applicant Information")
    pdf.field_row("Monthly income", _fmt_currency(profile.get("monthly_income")))
    pdf.field_row("Co-applicant income", _fmt_currency(profile.get("co_applicant_income")))
    pdf.field_row("Existing monthly EMI", _fmt_currency(profile.get("existing_emi")))
    pdf.field_row("Age", str(profile.get("age") or "-"))
    pdf.field_row("Employment type", (profile.get("employment_type") or "-").replace("_", " ").title())
    pdf.ln(3)

    pdf.section_title("Property Details")
    pdf.field_row("Property value", _fmt_currency(profile.get("property_price")))
    pdf.field_row("Down payment", _fmt_currency(profile.get("down_payment")))
    pdf.field_row("Required financing", _fmt_currency(
        affordability.get("required_loan") if affordability else profile.get("requested_loan")
    ))
    pdf.ln(3)

    pdf.section_title("Loan Calculation")
    pdf.field_row("Estimated loan amount", _fmt_currency(emi_result.get("principal") or profile.get("requested_loan")))
    pdf.field_row("Interest rate used", _fmt_pct(emi_result.get("annual_rate_pct") or eligibility.get("interest_rate_used")))
    pdf.field_row("Tenure", f"{profile.get('tenure_years') or emi_result.get('tenure_years') or '-'} years")
    pdf.field_row("Estimated EMI", _fmt_currency(emi_result.get("emi") or affordability.get("required_emi")))
    pdf.field_row("Total interest", _fmt_currency(emi_result.get("total_interest")))
    pdf.field_row("Total repayment", _fmt_currency(emi_result.get("total_payment")))
    pdf.ln(3)

    if eligibility:
        pdf.section_title("Eligibility")
        loan_range = eligibility.get("eligible_loan_range") or {}
        pdf.field_row("Estimated eligibility range",
                      f"{_fmt_currency(loan_range.get('low'))} - {_fmt_currency(loan_range.get('high'))}")
        pdf.field_row("Affordability category", eligibility.get("category") or "-")
        capacity = eligibility.get("comfortable_emi_range") or {}
        pdf.field_row("EMI capacity", f"{_fmt_currency(capacity.get('low'))} - {_fmt_currency(capacity.get('high'))}")
        if affordability.get("loan_to_property_ratio") is not None:
            pdf.field_row("Loan-to-property ratio", f"{affordability['loan_to_property_ratio'] * 100:.1f}%")
        pdf.ln(3)

    suggestions = (affordability.get("suggestions") if affordability else None) or []
    if suggestions:
        pdf.section_title("Recommendations")
        for s in suggestions:
            pdf.bullet(s)
        pdf.ln(2)

    employment_type = profile.get("employment_type")
    if employment_type:
        checklist = get_document_checklist(employment_type)
        if checklist.get("documents"):
            pdf.section_title("Document Checklist")
            for doc in checklist["documents"]:
                pdf.bullet(doc)
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(120, 120, 120)
            pdf.multi_cell(0, 5, checklist["note"], new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 5, DISCLAIMER, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())

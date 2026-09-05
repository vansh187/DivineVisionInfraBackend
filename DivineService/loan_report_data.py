"""Flattens a stored loan-report snapshot into the field set needed to render the
branded eligibility report - consumed by the frontend (pdf-lib) via
GET /loan/report/{id} and by the chatbot turn's structured_result, and later by
the backend PDF generator itself. Pure functions: no DB, no I/O, never raises.
"""
import os
from datetime import date, datetime, timezone

# Single source of truth for the report URLs / filename, shared by the chatbot
# tool (service_chatbot) and the REST layer (loan_api) so they can never drift.
PUBLIC_API_BASE_URL = (
    os.getenv("DIVINE_PUBLIC_API_URL") or "https://divinevisioninfrabackend.onrender.com"
).rstrip("/")


def report_pdf_filename(report_id: str) -> str:
    return f"home-loan-eligibility-{report_id}.pdf"


def report_download_url(report_id: str) -> str:
    return f"{PUBLIC_API_BASE_URL}/loan/report/{report_id}/download"


def report_data_url(report_id: str, session_id: str = None) -> str:
    url = f"{PUBLIC_API_BASE_URL}/loan/report/{report_id}"
    # The session id gates the contact-PII fields on GET /loan/report/{id}; include
    # it so the widget's own re-fetch link returns the full payload.
    return f"{url}?session_id={session_id}" if session_id else url

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _three(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_two(rest))
    return " ".join(parts)


def amount_in_words_indian(value) -> str:
    """4250000 -> 'Forty-Two Lakh Fifty Thousand Rupees'. Indian numbering
    (Thousand / Lakh / Crore). Rounds to whole rupees. '' on bad input."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return "Zero Rupees"
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, hundreds = divmod(n, 1_000)
    segments = []
    if crore:
        segments.append(f"{_three(crore) if crore >= 100 else _two(crore)} Crore")
    if lakh:
        segments.append(f"{_two(lakh)} Lakh")
    if thousand:
        segments.append(f"{_two(thousand)} Thousand")
    if hundreds:
        segments.append(_three(hundreds))
    return " ".join(s for s in segments if s).strip() + " Rupees"


def _num(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _iso(value) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return str(value)


def _clean(value):
    text = (value or "")
    text = text.strip() if isinstance(text, str) else str(text).strip()
    return text or None


def build_report_data(snapshot: dict, report_id: str, issued_at=None, include_applicant: bool = True) -> dict:
    """Snapshot shape: {"profile": {...}, "last_calculation": {"eligibility"|"emi"|
    "affordability": {...}}, "applicant": {"name","phone","email"}}. Any part may be
    missing - every field degrades to None rather than raising.

    include_applicant=False blanks the contact-PII fields (name/phone/email) for
    callers that have not proven ownership of the report.
    """
    def _obj(value):
        return value if isinstance(value, dict) else {}

    snapshot = _obj(snapshot)
    profile = _obj(snapshot.get("profile"))
    calc = _obj(snapshot.get("last_calculation"))
    elig = _obj(calc.get("eligibility"))
    emi = _obj(calc.get("emi"))
    aff = _obj(calc.get("affordability"))
    applicant = _obj(snapshot.get("applicant")) if include_applicant else {}

    loan_range = elig.get("eligible_loan_range") or {}
    range_low = _num(loan_range.get("low"))
    range_high = _num(loan_range.get("high"))
    eligible_amount = (
        range_high or range_low
        or _num(profile.get("requested_loan")) or _num(emi.get("principal"))
    )

    rate = _num(emi.get("annual_rate_pct")) or _num(elig.get("interest_rate_used"))
    rate_illustrative = bool(emi.get("illustrative_rate_used") or elig.get("illustrative_rate_used"))

    tenure_years = _num(profile.get("tenure_years")) or _num(emi.get("tenure_years"))
    tenure_months = emi.get("tenure_months")
    if tenure_months is None and tenure_years:
        tenure_months = int(round(tenure_years * 12))

    foir = _num(elig.get("foir_cap"))
    foir_pct = round(foir * 100, 1) if foir is not None and 0 < foir <= 1 else foir

    monthly_income = _num(profile.get("monthly_income"))
    co_income = _num(profile.get("co_applicant_income"))
    combined_income = _num(elig.get("combined_income"))
    if combined_income is None and (monthly_income or co_income):
        combined_income = (monthly_income or 0.0) + (co_income or 0.0)

    schedule = emi.get("schedule") or []
    first_year = [
        {k: row.get(k) for k in ("month", "principal_component", "interest_component", "balance")}
        for row in schedule[:12] if isinstance(row, dict)
    ]

    return {
        "report_id": report_id,
        "issued_at": _iso(issued_at),
        "applicant": {
            "name": _clean(applicant.get("name")),
            "phone": _clean(applicant.get("phone")),
            "email": _clean(applicant.get("email")),
        },
        "eligible_amount": eligible_amount,
        "eligible_amount_words": amount_in_words_indian(eligible_amount) if eligible_amount else None,
        "eligible_loan_range": (
            {"low": range_low, "high": range_high} if (range_low or range_high) else None
        ),
        "eligibility_category": _clean(elig.get("category")),
        "rate_pct": rate,
        "rate_is_illustrative": rate_illustrative,
        "tenure_years": tenure_years,
        "tenure_months": tenure_months,
        "emi": _num(emi.get("emi")) or _num(aff.get("required_emi")),
        "total_interest": _num(emi.get("total_interest")),
        "total_payment": _num(emi.get("total_payment")),
        "monthly_income": monthly_income,
        "co_applicant_income": co_income,
        "combined_income": combined_income,
        "existing_obligations": _num(profile.get("existing_emi")),
        "foir_pct": foir_pct,
        "employment_type": (
            (profile.get("employment_type") or "").replace("_", " ").title() or None
        ),
        "amortization_first_year": first_year or None,
    }

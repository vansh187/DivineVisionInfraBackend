"""Configurable, rule-based home-loan eligibility and affordability estimation.

This is explicitly NOT a bank eligibility/approval decision - it is an indicative
estimate based solely on the figures the visitor provides, using a configurable
affordability ratio (FOIR). Every result must be surfaced to the user as an
estimate/range, never a guarantee.
"""
import os

from DivineService.service_loan_calculator import calculate_emi, max_principal_for_emi

# Fraction of combined monthly income considered "comfortably available" for EMI
# obligations (Fixed Obligation to Income Ratio). Configurable via env var so ops
# can tune it without a code change.
FOIR_CAP_DEFAULT = float(os.getenv("LOAN_FOIR_CAP", "0.50"))

# Used only when the visitor hasn't stated a rate - always disclosed to the user
# as "illustrative" whenever it's the one actually used for a calculation.
ILLUSTRATIVE_RATE_DEFAULT = float(os.getenv("LOAN_ILLUSTRATIVE_RATE_PCT", "8.5"))

MIN_AGE = 21
MAX_AGE = 70


def _validate_income_inputs(monthly_income: float, existing_emi: float, age: float = None):
    if monthly_income is None or monthly_income < 0:
        raise ValueError("monthly_income must be zero or greater")
    if existing_emi is not None and existing_emi < 0:
        raise ValueError("existing_emi must be zero or greater")
    if age is not None and not (MIN_AGE <= age <= MAX_AGE):
        raise ValueError(f"age must be between {MIN_AGE} and {MAX_AGE}")


def calculate_loan_eligibility(monthly_income: float, co_applicant_income: float = None,
                                existing_emi: float = None, requested_loan: float = None,
                                tenure_years: float = 20, interest_rate: float = None,
                                credit_score_band: str = None, age: float = None,
                                foir_cap: float = None) -> dict:
    existing_emi = existing_emi or 0.0
    _validate_income_inputs(monthly_income, existing_emi, age)

    foir_cap = foir_cap or FOIR_CAP_DEFAULT
    rate_used = interest_rate or ILLUSTRATIVE_RATE_DEFAULT
    illustrative_rate_used = interest_rate is None

    combined_income = monthly_income + (co_applicant_income or 0.0)
    available_emi_capacity = max(0.0, combined_income * foir_cap - existing_emi)

    comfortable_emi_low = round(available_emi_capacity * 0.9, 2)
    comfortable_emi_high = round(available_emi_capacity, 2)

    eligible_loan_low = max_principal_for_emi(comfortable_emi_low, rate_used, tenure_years)
    eligible_loan_high = max_principal_for_emi(comfortable_emi_high, rate_used, tenure_years)

    reasons = []
    if combined_income >= 150000:
        reasons.append("Strong household income")
    elif combined_income >= 75000:
        reasons.append("Moderate household income")
    else:
        reasons.append("Household income is on the lower side for the requested financing")

    if existing_emi > 0:
        reasons.append("Existing EMI slightly reduces eligibility")

    if co_applicant_income:
        reasons.append("Co-applicant income improves eligibility")

    if requested_loan is not None:
        if requested_loan <= eligible_loan_low:
            category = "Strong"
        elif requested_loan <= eligible_loan_high:
            category = "Moderate"
        else:
            category = "Low"
            reasons.append("Requested financing appears high compared with current income")
    elif combined_income >= 150000:
        category = "Strong"
    elif combined_income >= 75000:
        category = "Moderate"
    else:
        category = "Low"

    return {
        "combined_income": round(combined_income, 2),
        "existing_emi": round(existing_emi, 2),
        "available_emi_capacity": round(available_emi_capacity, 2),
        "comfortable_emi_range": {"low": comfortable_emi_low, "high": comfortable_emi_high},
        "eligible_loan_range": {"low": eligible_loan_low, "high": eligible_loan_high},
        "requested_loan": requested_loan,
        "category": category,
        "reasons": reasons,
        "foir_cap": foir_cap,
        "interest_rate_used": rate_used,
        "illustrative_rate_used": illustrative_rate_used,
        "credit_score_band": credit_score_band,
    }


def calculate_affordability(property_price: float, down_payment: float, monthly_income: float,
                             co_applicant_income: float = None, existing_emi: float = None,
                             tenure_years: float = 20, interest_rate: float = None,
                             foir_cap: float = None) -> dict:
    if property_price is None or property_price <= 0:
        raise ValueError("property_price must be greater than 0")
    down_payment = down_payment or 0.0
    if down_payment > property_price:
        raise ValueError("down_payment cannot exceed property_price")

    rate_used = interest_rate or ILLUSTRATIVE_RATE_DEFAULT
    illustrative_rate_used = interest_rate is None
    required_loan = round(property_price - down_payment, 2)

    required_emi = calculate_emi(required_loan, rate_used, tenure_years)["emi"] if required_loan > 0 else 0.0

    eligibility = calculate_loan_eligibility(
        monthly_income=monthly_income, co_applicant_income=co_applicant_income,
        existing_emi=existing_emi, requested_loan=required_loan, tenure_years=tenure_years,
        interest_rate=interest_rate, foir_cap=foir_cap,
    )
    capacity = eligibility["available_emi_capacity"]
    gap = round(max(0.0, required_emi - capacity), 2)

    suggestions = []
    if gap > 0:
        suggestions.append("Increase down payment")
        suggestions.append("Reduce loan amount")
        suggestions.append("Consider a longer tenure")
        if not co_applicant_income:
            suggestions.append("Add eligible co-applicant income")
        suggestions.append("Consider properties within a lower price range")

    return {
        "property_price": property_price,
        "down_payment": down_payment,
        "required_loan": required_loan,
        "required_emi": required_emi,
        "emi_capacity": capacity,
        "gap": gap,
        "affordable": gap == 0,
        "interest_rate_used": rate_used,
        "illustrative_rate_used": illustrative_rate_used,
        "suggestions": suggestions,
        "loan_to_property_ratio": round(required_loan / property_price, 4) if property_price else None,
    }

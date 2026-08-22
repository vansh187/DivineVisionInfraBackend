"""Pure home-loan EMI math. No I/O, no LLM, no DB - deterministic and unit-testable
in isolation. The AI layer (service_chatbot.py) calls these functions as tools; it
never performs this arithmetic itself.
"""

MIN_TENURE_YEARS = 1
MAX_TENURE_YEARS = 30
MIN_RATE_PCT = 0.01
MAX_RATE_PCT = 30


def _validate_loan_inputs(principal: float, annual_rate_pct: float, tenure_years: float):
    if principal is None or principal <= 0:
        raise ValueError("principal must be greater than 0")
    if annual_rate_pct is None or not (MIN_RATE_PCT <= annual_rate_pct < MAX_RATE_PCT):
        raise ValueError(f"annual_rate_pct must be between {MIN_RATE_PCT} and {MAX_RATE_PCT}")
    if tenure_years is None or not (MIN_TENURE_YEARS <= tenure_years <= MAX_TENURE_YEARS):
        raise ValueError(f"tenure_years must be between {MIN_TENURE_YEARS} and {MAX_TENURE_YEARS}")


def calculate_emi(principal: float, annual_rate_pct: float, tenure_years: float) -> dict:
    """Standard reducing-balance EMI: EMI = P*r*(1+r)^n / ((1+r)^n - 1)."""
    _validate_loan_inputs(principal, annual_rate_pct, tenure_years)

    monthly_rate = annual_rate_pct / 12 / 100
    months = round(tenure_years * 12)

    factor = (1 + monthly_rate) ** months
    emi = principal * monthly_rate * factor / (factor - 1)
    total_payment = emi * months
    total_interest = total_payment - principal

    return {
        "principal": round(principal, 2),
        "annual_rate_pct": annual_rate_pct,
        "tenure_years": tenure_years,
        "tenure_months": months,
        "emi": round(emi, 2),
        "total_interest": round(total_interest, 2),
        "total_payment": round(total_payment, 2),
    }


def build_amortization_schedule(principal: float, annual_rate_pct: float, tenure_years: float) -> list:
    """Month-by-month principal/interest/balance breakdown."""
    _validate_loan_inputs(principal, annual_rate_pct, tenure_years)

    monthly_rate = annual_rate_pct / 12 / 100
    months = round(tenure_years * 12)
    emi = calculate_emi(principal, annual_rate_pct, tenure_years)["emi"]

    schedule = []
    balance = principal
    for month in range(1, months + 1):
        interest_component = balance * monthly_rate
        principal_component = emi - interest_component
        balance = max(0.0, balance - principal_component)
        schedule.append({
            "month": month,
            "principal_component": round(principal_component, 2),
            "interest_component": round(interest_component, 2),
            "balance": round(balance, 2),
        })
    return schedule


def max_principal_for_emi(emi: float, annual_rate_pct: float, tenure_years: float) -> float:
    """Inverse of calculate_emi: the largest principal serviceable by a given EMI."""
    if emi is None or emi <= 0:
        return 0.0
    if annual_rate_pct is None or annual_rate_pct <= 0 or tenure_years is None or tenure_years <= 0:
        return 0.0

    monthly_rate = annual_rate_pct / 12 / 100
    months = round(tenure_years * 12)
    factor = (1 + monthly_rate) ** months
    principal = emi * (factor - 1) / (monthly_rate * factor)
    return round(principal, 2)


def compare_tenures(principal: float, annual_rate_pct: float, tenure_options_years: list) -> list:
    """EMI/interest/total-payment comparison across multiple tenure options."""
    if not tenure_options_years:
        raise ValueError("tenure_options_years must be a non-empty list")
    return [calculate_emi(principal, annual_rate_pct, years) for years in tenure_options_years]


def calculate_required_income(desired_loan: float, annual_rate_pct: float, tenure_years: float, foir_cap: float) -> float:
    """Monthly income that would let this EMI fit within foir_cap (0 < foir_cap <= 1)."""
    if not (0 < foir_cap <= 1):
        raise ValueError("foir_cap must be between 0 and 1")
    emi = calculate_emi(desired_loan, annual_rate_pct, tenure_years)["emi"]
    return round(emi / foir_cap, 2)


def calculate_required_downpayment(property_price: float, max_eligible_loan: float) -> float:
    """The shortfall the buyer must cover in cash given their max eligible loan."""
    if property_price is None or property_price <= 0:
        raise ValueError("property_price must be greater than 0")
    return round(max(0.0, property_price - max(0.0, max_eligible_loan or 0.0)), 2)

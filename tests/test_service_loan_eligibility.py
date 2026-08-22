import pytest

from DivineService.service_loan_eligibility import calculate_loan_eligibility, calculate_affordability


def test_eligibility_strong_income_no_requested_loan():
    result = calculate_loan_eligibility(monthly_income=180000, existing_emi=15000)
    assert result["combined_income"] == 180000
    assert result["available_emi_capacity"] == pytest.approx(180000 * 0.5 - 15000)
    assert result["category"] == "Strong"
    assert "Existing EMI slightly reduces eligibility" in result["reasons"]
    assert result["illustrative_rate_used"] is True


def test_eligibility_combines_co_applicant_income():
    solo = calculate_loan_eligibility(monthly_income=120000, existing_emi=0)
    combined = calculate_loan_eligibility(monthly_income=120000, co_applicant_income=70000, existing_emi=0)
    assert combined["combined_income"] == 190000
    assert combined["eligible_loan_range"]["high"] > solo["eligible_loan_range"]["high"]
    assert "Co-applicant income improves eligibility" in combined["reasons"]


def test_eligibility_category_low_when_requested_loan_exceeds_range():
    result = calculate_loan_eligibility(monthly_income=80000, existing_emi=10000, requested_loan=9000000, tenure_years=20)
    assert result["category"] == "Low"
    assert "Requested financing appears high compared with current income" in result["reasons"]


def test_eligibility_category_strong_when_requested_loan_within_low_end():
    result = calculate_loan_eligibility(monthly_income=200000, existing_emi=0, requested_loan=1000000, tenure_years=20)
    assert result["category"] == "Strong"


def test_eligibility_category_matches_income_band_when_no_requested_loan():
    # Without a requested_loan, category must stay consistent with the income-band reason
    # text generated alongside it (previously this stayed "Strong" by default even when the
    # reason said "Moderate household income").
    moderate = calculate_loan_eligibility(monthly_income=80000, existing_emi=0)
    assert moderate["category"] == "Moderate"
    assert "Moderate household income" in moderate["reasons"]

    low = calculate_loan_eligibility(monthly_income=50000, existing_emi=0)
    assert low["category"] == "Low"

    strong = calculate_loan_eligibility(monthly_income=160000, existing_emi=0)
    assert strong["category"] == "Strong"


def test_eligibility_explicit_rate_marks_not_illustrative():
    result = calculate_loan_eligibility(monthly_income=150000, interest_rate=9.0)
    assert result["illustrative_rate_used"] is False
    assert result["interest_rate_used"] == 9.0


def test_eligibility_rejects_negative_income():
    with pytest.raises(ValueError):
        calculate_loan_eligibility(monthly_income=-1000)


def test_eligibility_rejects_out_of_range_age():
    with pytest.raises(ValueError):
        calculate_loan_eligibility(monthly_income=100000, age=15)


def test_affordability_no_gap_when_capacity_covers_emi():
    result = calculate_affordability(
        property_price=6000000, down_payment=3000000, monthly_income=200000, existing_emi=0, tenure_years=20,
    )
    assert result["gap"] == 0
    assert result["affordable"] is True
    assert result["suggestions"] == []


def test_affordability_gap_and_suggestions_when_shortfall():
    result = calculate_affordability(
        property_price=12000000, down_payment=2500000, monthly_income=160000, existing_emi=12000, tenure_years=20,
    )
    assert result["gap"] > 0
    assert result["affordable"] is False
    assert "Increase down payment" in result["suggestions"]
    assert "Add eligible co-applicant income" in result["suggestions"]


def test_affordability_omits_co_applicant_suggestion_when_already_present():
    result = calculate_affordability(
        property_price=12000000, down_payment=1000000, monthly_income=160000,
        co_applicant_income=70000, existing_emi=12000, tenure_years=20,
    )
    if result["gap"] > 0:
        assert "Add eligible co-applicant income" not in result["suggestions"]


def test_affordability_rejects_downpayment_exceeding_property_price():
    with pytest.raises(ValueError):
        calculate_affordability(property_price=5000000, down_payment=6000000, monthly_income=100000)


def test_affordability_rejects_non_positive_property_price():
    with pytest.raises(ValueError):
        calculate_affordability(property_price=0, down_payment=0, monthly_income=100000)


def test_affordability_loan_to_property_ratio():
    result = calculate_affordability(property_price=10000000, down_payment=2000000, monthly_income=300000, existing_emi=0)
    assert result["loan_to_property_ratio"] == pytest.approx(0.8)

import pytest

from DivineService.service_loan_calculator import (
    calculate_emi, build_amortization_schedule, max_principal_for_emi,
    compare_tenures, calculate_required_income, calculate_required_downpayment,
)


def test_calculate_emi_known_reference_value():
    # 50 lakh @ 8.5% for 240 months -> well-known reference EMI ~ 43,391
    result = calculate_emi(5000000, 8.5, 20)
    assert result["emi"] == pytest.approx(43391.16, abs=0.5)
    assert result["tenure_months"] == 240
    assert result["total_payment"] == pytest.approx(result["emi"] * 240, abs=1.0)
    assert result["total_interest"] == pytest.approx(result["total_payment"] - 5000000, abs=0.01)


@pytest.mark.parametrize("principal,rate,tenure", [(0, 8.5, 20), (-100, 8.5, 20)])
def test_calculate_emi_rejects_non_positive_principal(principal, rate, tenure):
    with pytest.raises(ValueError):
        calculate_emi(principal, rate, tenure)


def test_calculate_emi_rejects_out_of_range_tenure():
    with pytest.raises(ValueError):
        calculate_emi(1000000, 8.5, 31)
    with pytest.raises(ValueError):
        calculate_emi(1000000, 8.5, 0.5)


def test_calculate_emi_rejects_out_of_range_rate():
    with pytest.raises(ValueError):
        calculate_emi(1000000, 0, 20)
    with pytest.raises(ValueError):
        calculate_emi(1000000, 35, 20)


def test_amortization_schedule_sums_to_principal_plus_interest():
    principal, rate, tenure = 2000000, 9.0, 15
    schedule = build_amortization_schedule(principal, rate, tenure)
    emi_result = calculate_emi(principal, rate, tenure)

    assert len(schedule) == tenure * 12
    total_principal_paid = sum(row["principal_component"] for row in schedule)
    total_interest_paid = sum(row["interest_component"] for row in schedule)
    assert total_principal_paid == pytest.approx(principal, abs=1.0)
    assert total_interest_paid == pytest.approx(emi_result["total_interest"], abs=1.0)
    assert schedule[-1]["balance"] == pytest.approx(0.0, abs=1.0)


def test_max_principal_for_emi_round_trips_with_calculate_emi():
    principal, rate, tenure = 8000000, 8.5, 20
    emi = calculate_emi(principal, rate, tenure)["emi"]
    back_solved = max_principal_for_emi(emi, rate, tenure)
    assert back_solved == pytest.approx(principal, rel=0.001)


def test_max_principal_for_emi_handles_zero_or_negative_emi():
    assert max_principal_for_emi(0, 8.5, 20) == 0.0
    assert max_principal_for_emi(-100, 8.5, 20) == 0.0


def test_compare_tenures_returns_one_row_per_option():
    rows = compare_tenures(5000000, 8.5, [15, 20, 25])
    assert [r["tenure_years"] for r in rows] == [15, 20, 25]
    # Shorter tenure -> higher EMI, lower total interest.
    assert rows[0]["emi"] > rows[1]["emi"] > rows[2]["emi"]
    assert rows[0]["total_interest"] < rows[1]["total_interest"] < rows[2]["total_interest"]


def test_compare_tenures_rejects_empty_options():
    with pytest.raises(ValueError):
        compare_tenures(5000000, 8.5, [])


def test_calculate_required_income():
    required = calculate_required_income(desired_loan=5000000, annual_rate_pct=8.5, tenure_years=20, foir_cap=0.5)
    emi = calculate_emi(5000000, 8.5, 20)["emi"]
    assert required == pytest.approx(emi / 0.5, abs=0.01)


def test_calculate_required_downpayment():
    assert calculate_required_downpayment(12000000, 8000000) == 4000000
    assert calculate_required_downpayment(12000000, 15000000) == 0  # floored at 0, never negative


def test_calculate_required_downpayment_rejects_invalid_property_price():
    with pytest.raises(ValueError):
        calculate_required_downpayment(0, 8000000)

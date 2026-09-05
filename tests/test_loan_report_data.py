from DivineService.loan_report_data import amount_in_words_indian, build_report_data


# ---- amount_in_words_indian ----

def test_words_lakh_thousand():
    assert amount_in_words_indian(4250000) == "Forty-Two Lakh Fifty Thousand Rupees"


def test_words_crore():
    assert amount_in_words_indian(12500000) == "One Crore Twenty-Five Lakh Rupees"


def test_words_with_hundreds():
    assert amount_in_words_indian(103500) == "One Lakh Three Thousand Five Hundred Rupees"


def test_words_edge_cases():
    assert amount_in_words_indian(0) == "Zero Rupees"
    assert amount_in_words_indian(-5) == "Zero Rupees"
    assert amount_in_words_indian(None) == ""
    assert amount_in_words_indian("abc") == ""
    assert amount_in_words_indian(4250000.4) == "Forty-Two Lakh Fifty Thousand Rupees"


# ---- build_report_data ----

_SNAPSHOT = {
    "profile": {
        "monthly_income": 120000, "co_applicant_income": 40000, "existing_emi": 8000,
        "tenure_years": 20, "employment_type": "self_employed",
    },
    "last_calculation": {
        "emi": {"emi": 34713, "annual_rate_pct": 8.5, "tenure_months": 240,
                "total_interest": 4331120, "total_payment": 8331120, "illustrative_rate_used": True,
                "schedule": [{"month": i, "principal_component": 1, "interest_component": 2, "balance": 3}
                             for i in range(1, 25)]},
        "eligibility": {"eligible_loan_range": {"low": 3600000, "high": 4250000},
                        "combined_income": 160000, "foir_cap": 0.5, "category": "comfortable"},
    },
    "applicant": {"name": " Vansh ", "phone": "7276971875", "email": ""},
}


def test_build_report_data_happy_path():
    d = build_report_data(_SNAPSHOT, "rid-1", "2026-09-05T10:00:00+00:00")
    assert d["report_id"] == "rid-1"
    assert d["issued_at"] == "2026-09-05T10:00:00+00:00"
    assert d["applicant"] == {"name": "Vansh", "phone": "7276971875", "email": None}
    assert d["eligible_amount"] == 4250000
    assert d["eligible_amount_words"] == "Forty-Two Lakh Fifty Thousand Rupees"
    assert d["eligible_loan_range"] == {"low": 3600000, "high": 4250000}
    assert d["rate_pct"] == 8.5
    assert d["rate_is_illustrative"] is True
    assert d["tenure_months"] == 240
    assert d["tenure_years"] == 20
    assert d["emi"] == 34713
    assert d["total_interest"] == 4331120
    assert d["monthly_income"] == 120000
    assert d["combined_income"] == 160000
    assert d["existing_obligations"] == 8000
    assert d["foir_pct"] == 50.0
    assert d["eligibility_category"] == "comfortable"
    assert d["employment_type"] == "Self Employed"
    assert len(d["amortization_first_year"]) == 12


def test_build_report_data_tolerates_missing_and_malformed_parts():
    d = build_report_data({"profile": None, "last_calculation": {"emi": 999}}, "rid-2")
    assert d["report_id"] == "rid-2"
    assert d["eligible_amount"] is None
    assert d["eligible_amount_words"] is None
    assert d["emi"] is None
    assert d["applicant"] == {"name": None, "phone": None, "email": None}
    assert d["amortization_first_year"] is None
    assert d["issued_at"]  # falls back to now()


def test_build_report_data_derives_tenure_months_and_combined_income():
    snap = {"profile": {"monthly_income": 100000, "co_applicant_income": 25000, "tenure_years": 15},
            "last_calculation": {"emi": {"emi": 10000, "annual_rate_pct": 9}}}
    d = build_report_data(snap, "rid-3")
    assert d["tenure_months"] == 180
    assert d["combined_income"] == 125000

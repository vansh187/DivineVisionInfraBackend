import pytest

from DivineService.loan_utils import normalize_indian_amount


@pytest.mark.parametrize("raw,expected", [
    ("1.5 lakh", 150000.0),
    ("₹1.5L", 150000.0),
    ("95 lakhs", 9500000.0),
    ("1 crore", 10000000.0),
    ("80k", 80000.0),
    ("150000", 150000.0),
    ("₹75 lakh", 7500000.0),
    ("1.2 crore", 12000000.0),
    (150000, 150000.0),
    (150000.0, 150000.0),
])
def test_normalize_indian_amount(raw, expected):
    assert normalize_indian_amount(raw) == expected


def test_normalize_indian_amount_none_and_empty():
    assert normalize_indian_amount(None) is None
    assert normalize_indian_amount("") is None
    assert normalize_indian_amount("no numbers here") is None

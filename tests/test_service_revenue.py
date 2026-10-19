"""Unit coverage for serviceRevenue against a mocked persistence layer -
complements the real-database coverage in test_persistence_revenue.py and the
end-to-end HTTP coverage in test_admin_revenue_api.py."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_revenue import serviceRevenue


def _service():
    persistence = MagicMock()
    return serviceRevenue(persistence=persistence), persistence


def _row(**kw):
    base = {
        "transaction_id": "pay-1", "booking_id": "BKG-2026-000001", "customer_id": "C00001",
        "customer_first_name": "Meera", "customer_last_name": "Pillai",
        "project_name": "Palm County", "unit_number": "P-01",
        "amount": 3120000, "currency": "INR", "method": "razorpay",
        "revenue_status": "captured", "created_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        "total_count": 1,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- list_transactions ----------

def test_list_transactions_maps_rows_and_pagination():
    svc, persistence = _service()
    persistence.list_transactions.return_value = [_row()]

    result = svc.list_transactions(page=1, page_size=20)

    assert result["pagination"] == {"page": 1, "page_size": 20, "total_items": 1, "total_pages": 1}
    item = result["items"][0]
    assert item["transaction_id"] == "pay-1"
    assert item["customer_name"] == "Meera Pillai"
    assert item["status"] == "captured"
    assert item["amount"] == 3120000.0


def test_list_transactions_falls_back_to_count_when_empty():
    svc, persistence = _service()
    persistence.list_transactions.return_value = []
    persistence.count_transactions.return_value = 0

    result = svc.list_transactions()

    assert result["items"] == []
    assert result["pagination"]["total_items"] == 0
    persistence.count_transactions.assert_called_once()


def test_list_transactions_rejects_invalid_status():
    svc, _ = _service()
    try:
        svc.list_transactions(status="not_a_real_status")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_status"


def test_list_transactions_rejects_invalid_method():
    svc, _ = _service()
    try:
        svc.list_transactions(method="bitcoin")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_method"


def test_list_transactions_rejects_invalid_page():
    svc, _ = _service()
    try:
        svc.list_transactions(page=0)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_pagination"


def test_list_transactions_rejects_date_from_after_date_to():
    svc, _ = _service()
    try:
        svc.list_transactions(date_from="2026-09-20", date_to="2026-09-01")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "date_from_after_date_to"


def test_list_transactions_rejects_malformed_date():
    svc, _ = _service()
    try:
        svc.list_transactions(date_from="not-a-date")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_date_from"


def test_list_transactions_wraps_unexpected_persistence_failure():
    """A raw DB/driver failure must never reach the caller verbatim - it's a
    financial endpoint, so no internal detail (table names, SQL, stack info)
    can leak through an unhandled exception."""
    svc, persistence = _service()
    persistence.list_transactions.side_effect = RuntimeError("connection reset by peer")
    try:
        svc.list_transactions()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "list_transactions_failed"


def test_list_transactions_null_customer_name_when_names_blank():
    svc, persistence = _service()
    persistence.list_transactions.return_value = [_row(customer_first_name=None, customer_last_name=None)]
    result = svc.list_transactions()
    assert result["items"][0]["customer_name"] is None


# ---------- get_transaction ----------

def test_get_transaction_not_found_for_blank_id():
    svc, persistence = _service()
    try:
        svc.get_transaction("   ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"
    persistence.get_transaction.assert_not_called()


def test_get_transaction_not_found_when_persistence_returns_none():
    svc, persistence = _service()
    persistence.get_transaction.return_value = None
    try:
        svc.get_transaction("pay-missing")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_get_transaction_includes_gateway_reference_fields():
    svc, persistence = _service()
    persistence.get_transaction.return_value = _row(razorpay_payment_id="pay_x", utr_number=None)
    item = svc.get_transaction("pay-1")
    assert item["razorpay_payment_id"] == "pay_x"
    assert item["utr_number"] is None


def test_get_transaction_wraps_unexpected_persistence_failure():
    svc, persistence = _service()
    persistence.get_transaction.side_effect = RuntimeError("boom")
    try:
        svc.get_transaction("pay-1")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "get_transaction_failed"


# ---------- get_summary ----------

def test_get_summary_maps_totals():
    svc, persistence = _service()
    persistence.get_summary.return_value = SimpleNamespace(
        total_transactions=4, gross_amount=6700000, net_amount=4600000,
        captured_amount=3120000, cash_amount=980000,
        refund_pending_amount=500000, refunded_amount=2100000,
    )
    summary = svc.get_summary()
    assert summary["total_transactions"] == 4
    assert summary["gross_amount"] == 6700000.0
    assert summary["refunded_amount"] == 2100000.0


def test_get_summary_defaults_to_zeroes_when_no_row():
    svc, persistence = _service()
    persistence.get_summary.return_value = None
    summary = svc.get_summary()
    assert summary["total_transactions"] == 0
    assert summary["gross_amount"] == 0.0


def test_get_summary_rejects_invalid_date_range():
    svc, _ = _service()
    try:
        svc.get_summary(date_from="2026-09-20", date_to="2026-09-01")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "date_from_after_date_to"


def test_get_summary_wraps_unexpected_persistence_failure():
    svc, persistence = _service()
    persistence.get_summary.side_effect = RuntimeError("boom")
    try:
        svc.get_summary()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "get_summary_failed"

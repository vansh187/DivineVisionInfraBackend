"""Unit coverage for serviceRefund against mocked persistence layers."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_refund import serviceRefund


def _service():
    refund_persistence = MagicMock()
    payment_persistence = MagicMock()
    return serviceRefund(
        persistence=refund_persistence, payment_persistence=payment_persistence,
    ), refund_persistence, payment_persistence


def _row(**kw):
    base = {
        "id": "pay-1", "booking_id": "BKG-1", "customer_id": "C00001",
        "customer_first_name": "Meera", "customer_last_name": "Pillai",
        "project_name": "Palm County", "unit_number": "P-01",
        "amount": 100000, "currency": "INR", "method": "cash",
        "display_status": "cash_refund_pending",
        "refund_initiated_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        "refund_completed_date": None, "total_count": 1,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_list_refunds_maps_rows_and_pagination():
    svc, refunds, _ = _service()
    refunds.list_refunds.return_value = [_row()]

    result = svc.list_refunds(page=1, page_size=20)

    assert result["pagination"] == {"page": 1, "page_size": 20, "total_items": 1, "total_pages": 1}
    item = result["items"][0]
    assert item["id"] == "pay-1"
    assert item["customer_name"] == "Meera Pillai"
    assert item["status"] == "cash_refund_pending"
    assert item["amount"] == 100000.0


def test_list_refunds_falls_back_to_count_when_empty():
    svc, refunds, _ = _service()
    refunds.list_refunds.return_value = []
    refunds.count_refunds.return_value = 0

    result = svc.list_refunds()

    assert result["items"] == []
    assert result["pagination"]["total_items"] == 0
    refunds.count_refunds.assert_called_once()


def test_list_refunds_rejects_invalid_filters():
    svc, _, _ = _service()

    for kwargs, detail in [
        ({"status": "not_real"}, "invalid_status"),
        ({"method": "bitcoin"}, "invalid_method"),
        ({"page": 0}, "invalid_pagination"),
    ]:
        try:
            svc.list_refunds(**kwargs)
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == detail


def test_get_refund_includes_detail_fields():
    svc, refunds, _ = _service()
    refunds.get_refund.return_value = _row(
        method="zoho", display_status="processing",
        zoho_payment_id="pay_gateway", zoho_refund_id=None,
        utr_number=None, refund_note="gateway timeout",
        created_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    item = svc.get_refund("pay-1")

    assert item["gateway_payment_id"] == "pay_gateway"
    assert item["refund_note"] == "gateway timeout"
    assert item["created_at"].year == 2026


def test_get_refund_uses_legacy_razorpay_payment_id_for_pre_cutover_rows():
    svc, refunds, _ = _service()
    refunds.get_refund.return_value = _row(
        method="razorpay", display_status="bank_transfer_pending",
        razorpay_payment_id="pay_legacy", zoho_payment_id=None, zoho_refund_id=None,
        utr_number=None, refund_note="manual - gateway retired",
        created_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    item = svc.get_refund("pay-1")

    assert item["gateway_payment_id"] == "pay_legacy"


def test_get_refund_not_found_for_blank_id():
    svc, refunds, _ = _service()
    try:
        svc.get_refund("  ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"
    refunds.get_refund.assert_not_called()


def test_mark_collected_requires_manual_refund():
    svc, _, payments = _service()
    payments.get_by_id.return_value = SimpleNamespace(method="zoho")

    try:
        svc.mark_collected("pay-1", admin_id="DV1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_a_manual_refund"


def test_mark_collected_accepts_legacy_razorpay_refund():
    """Unlike a live 'zoho' refund, a pre-cutover razorpay refund IS treated as
    a manual refund now that the Razorpay gateway is retired."""
    svc, refunds, payments = _service()
    payments.get_by_id.return_value = SimpleNamespace(method="razorpay")
    payments.mark_manual_refund_collected.return_value = SimpleNamespace(id="pay-1")
    refunds.get_refund.return_value = _row(method="razorpay", display_status="bank_transfer_completed")

    svc.mark_collected("pay-1", admin_id="DV1")

    payments.mark_manual_refund_collected.assert_called_once()


def test_mark_collected_claims_pending_manual_refund_and_returns_detail():
    svc, refunds, payments = _service()
    payments.get_by_id.return_value = SimpleNamespace(method="cash")
    payments.mark_manual_refund_collected.return_value = SimpleNamespace(id="pay-1")
    refunds.get_refund.return_value = _row(display_status="cash_collected", refund_completed_date=datetime.now(timezone.utc))

    result = svc.mark_collected("pay-1", admin_id="DV5001", note="Paid from site office")

    payments.mark_manual_refund_collected.assert_called_once()
    assert "confirmed by DV5001" in payments.mark_manual_refund_collected.call_args.kwargs["note"]
    assert "Paid from site office" in payments.mark_manual_refund_collected.call_args.kwargs["note"]
    assert result["status"] == "cash_collected"


def test_mark_collected_rejects_when_atomic_claim_fails():
    svc, _, payments = _service()
    payments.get_by_id.return_value = SimpleNamespace(method="rtgs_neft")
    payments.mark_manual_refund_collected.return_value = None

    try:
        svc.mark_collected("pay-1", admin_id="DV5001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_not_pending"


def test_unexpected_persistence_errors_are_wrapped():
    svc, refunds, _ = _service()
    refunds.list_refunds.side_effect = RuntimeError("raw sql detail")

    try:
        svc.list_refunds()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "list_refunds_failed"

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from DivineService.service_broker_commission import serviceBrokerCommission


def _record(**overrides):
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    values = {
        "id": "com_00112233445566778899aabbccddeeff",
        "broker_id": "brk_123",
        "serial_number": "SN-1001",
        "unit_address": "Plot 42, OPS Divine Greens, Indore",
        "customer_name": "Rahul Sharma",
        "township": "OPS Divine Greens",
        "sale_value": "4500000.00",
        "commission_amount": "45000.00",
        "status": "paid",
        "transaction_mode": "cash",
        "created_at": now,
        "paid_at": now,
        "rejected_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service():
    persistence = MagicMock()
    return serviceBrokerCommission(persistence), persistence


def test_create_paid_commission_sets_backend_generated_values():
    svc, persistence = _service()
    persistence.create_commission.return_value = _record()

    result = svc.create_paid_commission(
        brokerId=" brk_123 ",
        serialNumber=" SN-1001 ",
        unitAddress=" Plot 42, OPS Divine Greens, Indore ",
        customerName=" Rahul Sharma ",
        township=" OPS Divine Greens ",
        saleValue=4500000,
        commissionAmount=45000,
        transactionMode="cash",
    )

    _, kwargs = persistence.create_commission.call_args
    assert kwargs["id"].startswith("com_")
    assert kwargs["broker_id"] == "brk_123"
    assert kwargs["status"] == "paid"
    assert kwargs["transaction_mode"] == "cash"
    assert kwargs["razorpay_order_id"] is None
    assert kwargs["paid_at"] == kwargs["created_at"]
    assert kwargs["rejected_at"] is None
    assert result["commissionAmount"] == 45000.0
    assert result["createdAt"].endswith("Z")


def test_create_paid_commission_rejects_broker_booking_mode():
    svc, _ = _service()

    try:
        svc.create_paid_commission(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="booking",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "transactionMode_must_be_cash"


@patch("DivineService.service_broker_commission.razorpay.Client")
def test_initiate_admin_razorpay_commission_uses_pending_booking_mode(mock_client_cls):
    mock_client = MagicMock()
    mock_client.order.create.return_value = {"id": "order_commission_123"}
    mock_client_cls.return_value = mock_client
    svc, persistence = _service()
    svc._key_id = "rzp_test_key"
    svc._key_secret = "rzp_test_secret"
    persistence.create_commission.return_value = _record(status="pending", transaction_mode="booking", paid_at=None)

    commission, payment = svc.initiate_admin_razorpay_commission(
        brokerId="brk_123",
        serialNumber="SN-1001",
        unitAddress="Plot 42",
        commissionAmount=45000,
        transactionMode="booking",
    )

    _, kwargs = persistence.create_commission.call_args
    assert kwargs["status"] == "pending"
    assert kwargs["transaction_mode"] == "booking"
    assert kwargs["razorpay_order_id"] == "order_commission_123"
    assert kwargs["paid_at"] is None
    assert commission["status"] == "pending"
    assert commission["transactionMode"] == "booking"
    assert payment["razorpayOrderId"] == "order_commission_123"
    assert payment["razorpayKeyId"] == "rzp_test_key"
    assert payment["amountPaise"] == 4500000


def test_initiate_admin_razorpay_commission_rejects_cash_mode():
    svc, _ = _service()

    try:
        svc.initiate_admin_razorpay_commission(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="cash",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "admin_transactionMode_must_be_booking"


def test_initiate_admin_razorpay_commission_requires_gateway_config():
    svc, _ = _service()
    svc._key_id = None
    svc._key_secret = None

    try:
        svc.initiate_admin_razorpay_commission(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="booking",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_not_configured"


def test_create_paid_commission_rejects_missing_required_fields():
    svc, _ = _service()

    try:
        svc.create_paid_commission(
            brokerId="",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="cash",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "brokerId_required"


def test_create_paid_commission_rejects_invalid_money():
    svc, _ = _service()

    try:
        svc.create_paid_commission(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=0,
            transactionMode="cash",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_commissionAmount"


def test_list_for_broker_returns_commissions_and_summary():
    svc, persistence = _service()
    persistence.list_by_broker.return_value = [_record()]
    persistence.summary_by_broker.return_value = [
        SimpleNamespace(status="paid", total="45000.00"),
        SimpleNamespace(status="pending", total="1000.00"),
    ]

    result = svc.list_for_broker(" brk_123 ")

    persistence.list_by_broker.assert_called_once_with("brk_123")
    persistence.summary_by_broker.assert_called_once_with("brk_123")
    assert result["success"] is True
    assert len(result["commissions"]) == 1
    assert result["summary"] == {"pending": 1000.0, "paid": 45000.0, "rejected": 0.0}

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
    assert kwargs["zoho_payments_session_id"] is None
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


def test_initiate_admin_commission_payment_uses_pending_booking_mode():
    gateway = MagicMock()
    gateway.create_payment_session.return_value = {
        "payments_session_id": "session_commission_123", "access_key": "key_commission_123",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_commission_123",
        "amount": "45000.00", "currency": "INR",
    }
    persistence = MagicMock()
    svc = serviceBrokerCommission(persistence, gateway=gateway)
    persistence.create_commission.return_value = _record(status="pending", transaction_mode="booking", paid_at=None)

    commission, payment = svc.initiate_admin_commission_payment(
        brokerId="brk_123",
        serialNumber="SN-1001",
        unitAddress="Plot 42",
        commissionAmount=45000,
        transactionMode="booking",
        success_url="https://fe.example.com/success",
        failure_url="https://fe.example.com/failure",
    )

    _, kwargs = persistence.create_commission.call_args
    assert kwargs["status"] == "pending"
    assert kwargs["transaction_mode"] == "booking"
    assert kwargs["zoho_payments_session_id"] == "session_commission_123"
    assert kwargs["paid_at"] is None
    assert commission["status"] == "pending"
    assert commission["transactionMode"] == "booking"
    assert payment["zohoPaymentsSessionId"] == "session_commission_123"
    assert payment["zohoAccessKey"] == "key_commission_123"
    # The critical, highest-risk assertion in this whole migration: the gateway
    # must receive a decimal amount, never Razorpay's amount*100 paise convention.
    _, session_kwargs = gateway.create_payment_session.call_args
    assert session_kwargs["amount"] == 45000


def test_initiate_admin_commission_payment_rejects_cash_mode():
    svc, _ = _service()

    try:
        svc.initiate_admin_commission_payment(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="cash",
            success_url="https://fe.example.com/success",
            failure_url="https://fe.example.com/failure",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "admin_transactionMode_must_be_booking"


def test_initiate_admin_commission_payment_requires_gateway_config():
    gateway = MagicMock()
    gateway.create_payment_session.side_effect = RuntimeError("payment_not_configured")
    persistence = MagicMock()
    svc = serviceBrokerCommission(persistence, gateway=gateway)

    try:
        svc.initiate_admin_commission_payment(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="booking",
            success_url="https://fe.example.com/success",
            failure_url="https://fe.example.com/failure",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_not_configured"


def test_initiate_admin_commission_payment_requires_redirect_urls():
    svc, _ = _service()

    try:
        svc.initiate_admin_commission_payment(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="booking",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_redirect_urls_not_configured"


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


def test_create_paid_commission_propagates_integrity_error_as_is():
    from sqlalchemy.exc import IntegrityError

    svc, persistence = _service()
    persistence.create_commission.side_effect = IntegrityError("stmt", "params", Exception("dup"))

    try:
        svc.create_paid_commission(
            brokerId="brk_123",
            serialNumber="SN-1001",
            unitAddress="Plot 42",
            commissionAmount=45000,
            transactionMode="cash",
        )
        assert False, "expected IntegrityError"
    except IntegrityError:
        pass


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

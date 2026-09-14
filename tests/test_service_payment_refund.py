"""initiate_refund - the KYC-reject/cancel refund trigger. Real money, so every
branch (already-refunded guard, gateway success/failure, manual cash/rtgs_neft
paths, and the edge cases around a payment that was never actually charged or
carries no amount) is covered here without needing a live Razorpay account."""
import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_payment import servicePayment


def _service():
    persistence = MagicMock()
    svc = servicePayment(persistence, MagicMock(), MagicMock())
    svc._key_id, svc._key_secret = "rzp_test_fake", "fake_secret"
    return svc, persistence


def _row(**kw):
    base = {
        "id": "pay1", "status": "paid", "method": "razorpay", "amount": 2450000,
        "razorpay_payment_id": "rzp_pay_x", "refund_status": "none",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_initiate_refund_raises_not_found():
    svc, persistence = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.initiate_refund("pay1", reason="rejected")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_initiate_refund_raises_payment_not_paid():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(status="created")
    try:
        svc.initiate_refund("pay1", reason="rejected")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "payment_not_paid"


def test_initiate_refund_raises_already_initiated_when_pending():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="pending")
    try:
        svc.initiate_refund("pay1", reason="rejected")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_already_initiated"
    persistence.update_refund_status.assert_not_called()


def test_initiate_refund_raises_already_initiated_when_completed():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="completed")
    try:
        svc.initiate_refund("pay1", reason="rejected")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_already_initiated"


def test_initiate_refund_allows_retry_after_a_previously_failed_attempt():
    """refund_status='failed' is the one non-'none' state that still permits a
    fresh attempt - an admin retrying a refund that failed once must not be
    permanently blocked by the already-initiated guard."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="failed")
    persistence.update_refund_status.return_value = _row(refund_status="completed")
    with patch.object(servicePayment, "_client") as mock_client:
        mock_client.return_value.payment.refund.return_value = {"id": "rfnd_1"}
        svc.initiate_refund("pay1", reason="retry")
    persistence.update_refund_status.assert_called_once()


@patch.object(servicePayment, "_client")
def test_initiate_refund_razorpay_success_calls_gateway_with_paise_amount(mock_client):
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(amount=2450000)
    persistence.update_refund_status.return_value = _row(refund_status="completed")
    mock_client.return_value.payment.refund.return_value = {"id": "rfnd_abc123"}

    svc.initiate_refund("pay1", reason="KYC rejected")

    mock_client.return_value.payment.refund.assert_called_once_with("rzp_pay_x", {"amount": 245000000})
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["refund_amount"] == 2450000
    assert kwargs["razorpay_refund_id"] == "rfnd_abc123"
    assert kwargs["refund_completed_date"] is not None


@patch.object(servicePayment, "_client")
def test_initiate_refund_razorpay_gateway_failure_marks_pending_not_failed(mock_client):
    """A gateway error must never silently lose track of an owed refund - it
    settles 'pending' (needs manual retry), never leaves refund_status='none'
    and never raises out to the caller (reject/cancel must still succeed)."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row()
    persistence.update_refund_status.return_value = _row(refund_status="pending")
    mock_client.return_value.payment.refund.side_effect = RuntimeError("gateway timeout")

    svc.initiate_refund("pay1", reason="KYC rejected")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "manual retry" in kwargs["refund_note"]


def test_initiate_refund_razorpay_without_a_captured_payment_id_completes_as_zero():
    """A plot-booking order that was cancelled before Razorpay ever actually
    captured a payment (razorpay_payment_id never set) has nothing to refund at
    the gateway - this must complete cleanly, not crash trying to call refund()
    with no payment id."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(razorpay_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="completed", refund_amount=0)

    svc.initiate_refund("pay1", reason="cancelled before capture")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["refund_amount"] == 0


def test_initiate_refund_cash_settles_pending_with_office_collection_note():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="cash", razorpay_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1", reason="Docs mismatch")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "collect the cash refund from our office" in kwargs["refund_note"]
    assert "Docs mismatch" in kwargs["refund_note"]


def test_initiate_refund_rtgs_neft_settles_pending_with_manual_transfer_note():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="rtgs_neft", razorpay_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "NEFT/RTGS" in kwargs["refund_note"]


def test_initiate_refund_handles_none_amount_without_crashing():
    """A payment row with no amount (shouldn't happen, but must never 500) -
    the cash/rtgs_neft manual path just records refund_amount=None."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="cash", amount=None, razorpay_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending", refund_amount=None)

    svc.initiate_refund("pay1")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_amount"] is None


@patch.object(servicePayment, "_client")
def test_initiate_refund_razorpay_none_amount_omits_amount_kwarg(mock_client):
    """No amount on the row -> don't send amount to Razorpay at all (their API
    then refunds the full captured amount), rather than crashing on
    int(round(None * 100))."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(amount=None)
    persistence.update_refund_status.return_value = _row(refund_status="completed")
    mock_client.return_value.payment.refund.return_value = {"id": "rfnd_1"}

    svc.initiate_refund("pay1")

    mock_client.return_value.payment.refund.assert_called_once_with("rzp_pay_x", {})

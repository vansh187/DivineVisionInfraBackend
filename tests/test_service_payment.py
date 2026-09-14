import hashlib
import hmac
import json
import os
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from razorpay.errors import SignatureVerificationError
from DivineService.service_payment import servicePayment

_WEBHOOK_SECRET = "whsec_test_fake"


def _sign(body: str, secret: str = _WEBHOOK_SECRET) -> str:
    return hmac.new(key=secret.encode("utf-8"), msg=body.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()


def _webhook_body(event: str, order_id: str = "order_x", payment_id: str = "pay_x") -> str:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "status": "captured"}}},
    })


def _service(configured=True):
    persistence = MagicMock()
    svc = servicePayment(persistence)
    if configured:
        svc._key_id = "rzp_test_fake"
        svc._key_secret = "fake_secret"
    else:
        svc._key_id = None
        svc._key_secret = None
    return svc, persistence


# ---------- create_order ----------

def test_create_order_raises_when_not_configured():
    svc, _ = _service(configured=False)
    try:
        svc.create_order(100.0, owner_id="C00001", owner_role="customer")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_not_configured"


def test_create_order_rejects_zero_or_negative_amount():
    svc, _ = _service()
    for bad in (0, -5):
        try:
            svc.create_order(bad, owner_id="C00001", owner_role="customer")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_amount"


def test_create_order_rejects_amount_too_large():
    svc, _ = _service()
    try:
        svc.create_order(50_000_000, owner_id="C00001", owner_role="customer")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "amount_too_large"


@patch("DivineService.service_payment.razorpay.Client")
def test_create_order_wraps_razorpay_failure(mock_client_cls):
    mock_client = MagicMock()
    mock_client.order.create.side_effect = Exception("Amount exceeds maximum allowed")
    mock_client_cls.return_value = mock_client
    svc, _ = _service()
    try:
        svc.create_order(100.0, owner_id="C00001", owner_role="customer")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e).startswith("payment_order_failed:")
        assert str(e) == "payment_order_failed:Exception:amount_exceeds_maximum_allowed"


@patch("DivineService.service_payment.razorpay.Client")
def test_create_order_happy_path_converts_rupees_to_paise(mock_client_cls):
    mock_client = MagicMock()
    mock_client.order.create.return_value = {"id": "order_abc123"}
    mock_client_cls.return_value = mock_client
    persistence = MagicMock()
    persistence.create_payment.return_value = MagicMock(id="pay1", razorpay_order_id="order_abc123", amount=1500.50, currency="INR", status="created")
    svc = servicePayment(persistence)
    svc._key_id = "rzp_test_fake"
    svc._key_secret = "fake_secret"

    record, key_id = svc.create_order(1500.50, owner_id="C00001", owner_role="customer")

    assert key_id == "rzp_test_fake"
    mock_client.order.create.assert_called_once()
    call_kwargs = mock_client.order.create.call_args.args[0]
    assert call_kwargs["amount"] == 150050  # paise
    assert call_kwargs["currency"] == "INR"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["razorpay_order_id"] == "order_abc123"
    assert kwargs["status"] == "created"
    assert kwargs["owner_id"] == "C00001"


# ---------- verify_payment ----------

def test_verify_payment_raises_not_found_for_unknown_order():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = None
    try:
        svc.verify_payment("order_x", "pay_x", "sig_x", owner_id="C00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_verify_payment_raises_forbidden_for_other_owner():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(owner_id="C99999")
    try:
        svc.verify_payment("order_x", "pay_x", "sig_x", owner_id="C00001")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


@patch("DivineService.service_payment.razorpay.Client")
def test_verify_payment_marks_paid_on_valid_signature(mock_client_cls):
    mock_client = MagicMock()
    mock_client.utility.verify_payment_signature.return_value = True
    mock_client_cls.return_value = mock_client
    persistence = MagicMock()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="paid")
    svc = servicePayment(persistence)
    svc._key_id = "rzp_test_fake"
    svc._key_secret = "fake_secret"

    record, verified = svc.verify_payment("order_x", "pay_x", "sig_x", owner_id="C00001")

    assert verified is True
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "paid"


@patch("DivineService.service_payment.razorpay.Client")
def test_verify_payment_marks_failed_on_invalid_signature(mock_client_cls):
    mock_client = MagicMock()
    mock_client.utility.verify_payment_signature.side_effect = SignatureVerificationError("bad sig")
    mock_client_cls.return_value = mock_client
    persistence = MagicMock()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="failed")
    svc = servicePayment(persistence)
    svc._key_id = "rzp_test_fake"
    svc._key_secret = "fake_secret"

    record, verified = svc.verify_payment("order_x", "pay_x", "forged_sig", owner_id="C00001")

    assert verified is False
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "failed"


# ---------- get ----------

def test_get_raises_not_found():
    svc, persistence = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.get("pay1", requester_id="C00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_get_raises_forbidden_for_other_owner():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(owner_id="C99999")
    try:
        svc.get("pay1", requester_id="C00001")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_get_returns_record_for_owner():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(owner_id="C00001", id="pay1")
    record = svc.get("pay1", requester_id="C00001")
    assert record.id == "pay1"


def test_apply_booking_to_inventory_holds_without_zoho_push():
    persistence = MagicMock()
    inventory = MagicMock()
    booking_persistence = MagicMock()
    unit = MagicMock(project_name="Divine Greens", unit_number="A-112")
    inventory.hold_for_kyc_review.return_value = unit
    booking_persistence.create_booking.return_value = MagicMock(id="BKG-1")
    svc = servicePayment(
        persistence=persistence,
        inventory_persistence=inventory,
        booking_persistence=booking_persistence,
    )
    svc._push_booking_contact_to_zoho = MagicMock()
    record = MagicMock(
        id="pay1", purpose="plot_booking", inventory_id="INV-1",
        owner_id="C00001", owner_role="customer", method="razorpay", amount=2500000,
    )

    result = svc._apply_booking_to_inventory(record)

    assert result.inventory_status == "pending_kyc_review"
    booking_persistence.create_booking.assert_called_once()
    svc._push_booking_contact_to_zoho.assert_not_called()


def test_notify_booking_confirmed_pushes_to_zoho_after_approval():
    persistence = MagicMock()
    record = MagicMock(id="pay1")
    persistence.get_by_id.return_value = record
    svc = servicePayment(persistence=persistence)
    svc._push_booking_contact_to_zoho = MagicMock()
    booking = MagicMock(id="BKG-1")

    svc.notify_booking_confirmed("pay1", booking=booking)

    svc._push_booking_contact_to_zoho.assert_called_once_with(record, booking=booking)


@patch("DivineService.service_zoho.serviceZoho")
def test_push_booking_contact_to_zoho_includes_approved_booking_data(mock_zoho_cls):
    zoho = MagicMock()
    mock_zoho_cls.return_value = zoho
    svc = servicePayment(persistence=MagicMock())
    svc._load_customer = MagicMock(return_value=MagicMock(
        first_name="Rehan", last_name="Sharma", email="rehan@example.com", phone="9999999998",
    ))
    record = MagicMock(
        id="pay1", owner_id="C00001", inventory_id="INV-1",
        purpose="plot_booking", method="razorpay",
    )
    booking = MagicMock(
        id="BKG-1", project_name="Divine Greens", unit_number="A-112",
        amount=2500000, status="booked", kyc_status="verified",
    )

    svc._push_booking_contact_to_zoho(record, booking=booking)

    zoho.push_booking_contact_async.assert_called_once_with(
        customer_id="C00001",
        first_name="Rehan",
        last_name="Sharma",
        email="rehan@example.com",
        phone="9999999998",
        inventory_id="INV-1",
        payment_id="pay1",
        purpose="plot_booking",
        payment_method="razorpay",
        booking_id="BKG-1",
        project_name="Divine Greens",
        unit_number="A-112",
        booking_amount=2500000,
        booking_status="booked",
        kyc_status="verified",
    )


# ---------- handle_webhook ----------

def test_handle_webhook_raises_when_not_configured():
    svc, _ = _service()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)
        try:
            svc.handle_webhook(b"{}", "any-signature")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert str(e) == "payment_webhook_not_configured"


def test_handle_webhook_raises_on_invalid_signature():
    svc, _ = _service()
    body = _webhook_body("payment.captured")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        try:
            svc.handle_webhook(body.encode("utf-8"), "not-the-real-signature")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_webhook_signature"


def test_handle_webhook_raises_on_invalid_body_encoding():
    svc, _ = _service()
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        try:
            svc.handle_webhook(b"\xff\xfe not valid utf-8", "sig")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_webhook_body"


def test_handle_webhook_ignores_unrecognized_event_type():
    svc, persistence = _service()
    body = _webhook_body("order.paid")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "ignored_event:order.paid"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_ignores_malformed_payload():
    svc, persistence = _service()
    body = json.dumps({"event": "payment.captured", "payload": {}})
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "ignored_malformed_payload"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_ignores_unknown_order():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = None
    body = _webhook_body("payment.captured", order_id="order_unknown")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "ignored_unknown_order"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_does_not_downgrade_already_paid_payment():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(id="pay1", status="paid")
    body = _webhook_body("payment.failed")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "ignored_already_settled"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_marks_paid_on_captured_event():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(id="pay1", status="created")
    body = _webhook_body("payment.captured", order_id="order_x", payment_id="pay_abc")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "processed:paid"
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["id"] == "pay1"
    assert kwargs["status"] == "paid"
    assert kwargs["razorpay_payment_id"] == "pay_abc"


def test_handle_webhook_marks_failed_on_failed_event():
    svc, persistence = _service()
    persistence.get_by_razorpay_order_id.return_value = MagicMock(id="pay1", status="created")
    body = _webhook_body("payment.failed")
    with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": _WEBHOOK_SECRET}):
        result = svc.handle_webhook(body.encode("utf-8"), _sign(body))
    assert result == "processed:failed"
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "failed"


# ---------- record_cash_payment ----------

def test_record_cash_payment_allows_customer_caller():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    record = svc.record_cash_payment(1000, owner_id="C00001", owner_role="customer")

    assert record.status == "paid"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "C00001"
    assert kwargs["owner_role"] == "customer"


def test_record_cash_payment_allows_broker_caller():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    record = svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker")

    assert record.status == "paid"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "B00001"
    assert kwargs["owner_role"] == "broker"


def test_record_cash_payment_rejects_zero_or_negative_amount():
    svc, _ = _service()
    for bad in (0, -5):
        try:
            svc.record_cash_payment(bad, owner_id="B00001", owner_role="broker")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_amount"


def test_record_cash_payment_rejects_amount_too_large():
    svc, _ = _service()
    try:
        svc.record_cash_payment(50_000_000, owner_id="B00001", owner_role="broker")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "amount_too_large"


def test_record_cash_payment_does_not_require_razorpay_configuration():
    # Unlike create_order/verify_payment, cash never touches the gateway - must succeed
    # even with no RAZORPAY_KEY_ID/SECRET set at all.
    svc, persistence = _service(configured=False)
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")
    record = svc.record_cash_payment(15000, owner_id="B00001", owner_role="broker")
    assert record.status == "paid"


def test_record_cash_payment_settles_immediately_with_no_gateway_order_id():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(2500.75, owner_id="B00001", owner_role="broker")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "B00001"
    assert kwargs["amount"] == 2500.75
    assert kwargs["status"] == "paid"
    assert kwargs["method"] == "cash"
    assert kwargs["razorpay_order_id"] is None


def test_record_cash_payment_includes_note_when_given():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker", note="Collected at site visit")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {"note": "Collected at site visit"}


def test_record_cash_payment_omits_note_when_not_given():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {}


def test_record_cash_payment_omits_whitespace_only_note():
    svc, persistence = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker", note="   ")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {}

import json
import os
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_payment import servicePayment

_REDIRECT_ENV = {
    "ZOHO_PAYMENTS_SUCCESS_URL": "https://www.divinevisioninfra.com/customer/payments/success",
    "ZOHO_PAYMENTS_FAILURE_URL": "https://www.divinevisioninfra.com/customer/payments/failure",
}


def _webhook_body(event: str, session_id: str = "session_x", payment_id: str = "pay_x") -> str:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"payments_session_id": session_id, "payment_id": payment_id, "status": "succeeded"}},
    })


def _service(gateway=None):
    persistence = MagicMock()
    gw = gateway if gateway is not None else MagicMock()
    svc = servicePayment(persistence, gateway=gw)
    svc._load_customer = MagicMock(return_value=None)
    return svc, persistence, gw


# ---------- create_order ----------

def test_create_order_raises_when_redirect_urls_not_configured():
    svc, _, _gw = _service()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ZOHO_PAYMENTS_SUCCESS_URL", None)
        os.environ.pop("ZOHO_PAYMENTS_FAILURE_URL", None)
        try:
            svc.create_order(100.0, owner_id="C00001", owner_role="customer")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert str(e) == "payment_redirect_urls_not_configured"


def test_create_order_raises_when_gateway_not_configured():
    gw = MagicMock()
    gw.create_payment_session.side_effect = RuntimeError("payment_not_configured")
    svc, _, _ = _service(gateway=gw)
    with patch.dict(os.environ, _REDIRECT_ENV):
        try:
            svc.create_order(100.0, owner_id="C00001", owner_role="customer")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert str(e) == "payment_order_failed:payment_not_configured"


def test_create_order_rejects_zero_or_negative_amount():
    svc, _, _ = _service()
    for bad in (0, -5):
        try:
            svc.create_order(bad, owner_id="C00001", owner_role="customer")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_amount"


def test_create_order_rejects_amount_too_large():
    svc, _, _ = _service()
    try:
        svc.create_order(50_000_000, owner_id="C00001", owner_role="customer")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "amount_too_large"


def test_create_order_wraps_gateway_failure():
    gw = MagicMock()
    gw.create_payment_session.side_effect = RuntimeError("payment_gateway_error:status_400:amount_exceeds_maximum_allowed")
    svc, _, _ = _service(gateway=gw)
    with patch.dict(os.environ, _REDIRECT_ENV):
        try:
            svc.create_order(100.0, owner_id="C00001", owner_role="customer")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert str(e).startswith("payment_order_failed:")
            assert "amount_exceeds_maximum_allowed" in str(e)


def test_create_order_happy_path_sends_decimal_amount_never_paise():
    gw = MagicMock()
    gw.create_payment_session.return_value = {
        "payments_session_id": "session_abc123", "access_key": "key_abc", "checkout_url": "https://payments.zoho.in/hostedcheckout/key_abc",
        "amount": "1500.50", "currency": "INR",
    }
    persistence = MagicMock()
    persistence.create_payment.return_value = MagicMock(
        id="pay1", zoho_payments_session_id="session_abc123", amount=1500.50, currency="INR", status="created",
    )
    svc = servicePayment(persistence, gateway=gw)
    svc._load_customer = MagicMock(return_value=None)

    with patch.dict(os.environ, _REDIRECT_ENV):
        record, session = svc.create_order(1500.50, owner_id="C00001", owner_role="customer")

    assert session["access_key"] == "key_abc"
    gw.create_payment_session.assert_called_once()
    _, call_kwargs = gw.create_payment_session.call_args
    # The critical, highest-risk assertion in this whole migration: Zoho Payments
    # takes a DECIMAL amount, never Razorpay's amount*100 paise convention. Getting
    # this wrong overcharges/undercharges every customer by 100x.
    assert call_kwargs["amount"] == 1500.50
    assert call_kwargs["currency"] == "INR"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["zoho_payments_session_id"] == "session_abc123"
    assert kwargs["status"] == "created"
    assert kwargs["owner_id"] == "C00001"


# ---------- verify_payment ----------

def test_verify_payment_raises_not_found_for_unknown_session():
    svc, persistence, _ = _service()
    persistence.get_by_zoho_session_id.return_value = None
    try:
        svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "sig_x", owner_id="C00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_verify_payment_raises_forbidden_for_other_owner():
    svc, persistence, _ = _service()
    persistence.get_by_zoho_session_id.return_value = MagicMock(owner_id="C99999")
    try:
        svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "sig_x", owner_id="C00001")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_verify_payment_marks_paid_on_valid_signature_and_confirmed_session():
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = True
    gw.retrieve_payment_session.return_value = {"status": "succeeded"}
    persistence = MagicMock()
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="paid")
    svc = servicePayment(persistence, gateway=gw)

    record, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "sig_x", owner_id="C00001")

    assert verified is True
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "paid"
    assert kwargs["zoho_payment_id"] == "pay_x"


def test_verify_payment_marks_failed_on_invalid_signature():
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = False
    persistence = MagicMock()
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="failed")
    svc = servicePayment(persistence, gateway=gw)

    record, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "forged_sig", owner_id="C00001")

    assert verified is False
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "failed"
    gw.retrieve_payment_session.assert_not_called()


def test_verify_payment_marks_failed_when_redirect_says_not_succeeded_even_if_signed():
    """A validly-signed redirect claiming anything other than 'succeeded' must
    never be trusted as paid - the signature only proves Zoho sent it, not that
    the payment actually succeeded."""
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = True
    persistence = MagicMock()
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="failed")
    svc = servicePayment(persistence, gateway=gw)

    record, verified = svc.verify_payment("session_x", "pay_x", "failed", "100.00", "sig_x", owner_id="C00001")

    assert verified is False


def test_verify_payment_overrides_signed_success_when_live_session_disagrees():
    """Defense in depth beyond a bare signature check: even a validly-signed
    'succeeded' redirect is cross-checked against a live retrieve_payment_session
    call - if Zoho's own record says otherwise, that wins."""
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = True
    gw.retrieve_payment_session.return_value = {"status": "failed"}
    persistence = MagicMock()
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="failed")
    svc = servicePayment(persistence, gateway=gw)

    record, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "sig_x", owner_id="C00001")

    assert verified is False


def test_verify_payment_trusts_signature_when_live_recheck_errors():
    """A transient failure calling retrieve_payment_session must not turn a
    real, correctly-signed payment into a false 'failed' - the webhook is the
    durable fallback confirmation either way."""
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = True
    gw.retrieve_payment_session.side_effect = RuntimeError("payment_gateway_request_failed")
    persistence = MagicMock()
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", owner_id="C00001")
    persistence.update_payment_status.return_value = MagicMock(status="paid")
    svc = servicePayment(persistence, gateway=gw)

    record, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "100.00", "sig_x", owner_id="C00001")

    assert verified is True


# ---------- get ----------

def test_get_raises_not_found():
    svc, persistence, _ = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.get("pay1", requester_id="C00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_get_raises_forbidden_for_other_owner():
    svc, persistence, _ = _service()
    persistence.get_by_id.return_value = MagicMock(owner_id="C99999")
    try:
        svc.get("pay1", requester_id="C00001")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_get_returns_record_for_owner():
    svc, persistence, _ = _service()
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
        owner_id="C00001", owner_role="customer", method="zoho", amount=2500000,
    )

    result = svc._apply_booking_to_inventory(record)

    assert result.inventory_status == "pending_kyc_review"
    booking_persistence.create_booking.assert_called_once()
    svc._push_booking_contact_to_zoho.assert_not_called()


def test_apply_booking_to_inventory_still_trusts_legacy_razorpay_payments():
    """A pre-cutover razorpay payment must still be trusted enough to flip the
    plot to 'booked' - it already went through the real (now-retired) gateway's
    verify_payment/webhook confirmation back when it settled."""
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
    record = MagicMock(
        id="pay1", purpose="plot_booking", inventory_id="INV-1",
        owner_id="C00001", owner_role="customer", method="razorpay", amount=2500000,
    )

    result = svc._apply_booking_to_inventory(record)

    assert result.inventory_status == "pending_kyc_review"


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
        purpose="plot_booking", method="zoho",
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
        payment_method="zoho",
        booking_id="BKG-1",
        project_name="Divine Greens",
        unit_number="A-112",
        booking_amount=2500000,
        booking_status="booked",
        kyc_status="verified",
    )


# ---------- handle_webhook ----------

def test_handle_webhook_raises_when_not_configured():
    gw = MagicMock()
    gw.verify_webhook_signature.side_effect = RuntimeError("payment_webhook_not_configured")
    svc, _, _ = _service(gateway=gw)
    try:
        svc.handle_webhook(b"{}", "any-signature")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_webhook_not_configured"


def test_handle_webhook_raises_on_invalid_signature():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = False
    svc, _, _ = _service(gateway=gw)
    body = _webhook_body("payment.success")
    try:
        svc.handle_webhook(body.encode("utf-8"), "not-the-real-signature")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_webhook_signature"


def test_handle_webhook_raises_on_invalid_body_encoding():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, _, _ = _service(gateway=gw)
    try:
        svc.handle_webhook(b"\xff\xfe not valid utf-8", "sig")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_webhook_body"


def test_handle_webhook_ignores_unrecognized_event_type():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    body = _webhook_body("payment.dispute_created")
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "ignored_event:payment.dispute_created"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_ignores_malformed_payload():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    body = json.dumps({"event": "payment.success", "payload": {}})
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "ignored_malformed_payload"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_ignores_unknown_session():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    persistence.get_by_zoho_session_id.return_value = None
    body = _webhook_body("payment.success", session_id="session_unknown")
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "ignored_unknown_order"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_does_not_downgrade_already_paid_payment():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", status="paid")
    body = _webhook_body("payment.failed")
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "ignored_already_settled"
    persistence.update_payment_status.assert_not_called()


def test_handle_webhook_marks_paid_on_success_event():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", status="created")
    body = _webhook_body("payment.success", session_id="session_x", payment_id="pay_abc")
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "processed:paid"
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["id"] == "pay1"
    assert kwargs["status"] == "paid"
    assert kwargs["zoho_payment_id"] == "pay_abc"


def test_handle_webhook_marks_failed_on_failed_event():
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, persistence, _ = _service(gateway=gw)
    persistence.get_by_zoho_session_id.return_value = MagicMock(id="pay1", status="created")
    body = _webhook_body("payment.failed")
    result = svc.handle_webhook(body.encode("utf-8"), "sig")
    assert result == "processed:failed"
    _, kwargs = persistence.update_payment_status.call_args
    assert kwargs["status"] == "failed"


# ---------- record_cash_payment ----------

def test_record_cash_payment_allows_customer_caller():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    record = svc.record_cash_payment(1000, owner_id="C00001", owner_role="customer")

    assert record.status == "paid"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "C00001"
    assert kwargs["owner_role"] == "customer"


def test_record_cash_payment_allows_broker_caller():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    record = svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker")

    assert record.status == "paid"
    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "B00001"
    assert kwargs["owner_role"] == "broker"


def test_record_cash_payment_rejects_zero_or_negative_amount():
    svc, _, _ = _service()
    for bad in (0, -5):
        try:
            svc.record_cash_payment(bad, owner_id="B00001", owner_role="broker")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_amount"


def test_record_cash_payment_rejects_amount_too_large():
    svc, _, _ = _service()
    try:
        svc.record_cash_payment(50_000_000, owner_id="B00001", owner_role="broker")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "amount_too_large"


def test_record_cash_payment_does_not_require_gateway_configuration():
    # Unlike create_order/verify_payment, cash never touches the gateway - must
    # succeed even with a completely unconfigured Zoho Payments gateway.
    gw = MagicMock()
    gw.create_payment_session.side_effect = RuntimeError("payment_not_configured")
    svc, persistence, _ = _service(gateway=gw)
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")
    record = svc.record_cash_payment(15000, owner_id="B00001", owner_role="broker")
    assert record.status == "paid"


def test_record_cash_payment_settles_immediately_with_no_gateway_session_id():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(2500.75, owner_id="B00001", owner_role="broker")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["owner_id"] == "B00001"
    assert kwargs["amount"] == 2500.75
    assert kwargs["status"] == "paid"
    assert kwargs["method"] == "cash"
    assert kwargs["zoho_payments_session_id"] is None


def test_record_cash_payment_includes_note_when_given():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker", note="Collected at site visit")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {"note": "Collected at site visit"}


def test_record_cash_payment_omits_note_when_not_given():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {}


def test_record_cash_payment_omits_whitespace_only_note():
    svc, persistence, _ = _service()
    persistence.create_payment.return_value = MagicMock(id="pay1", status="paid", method="cash")

    svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker", note="   ")

    _, kwargs = persistence.create_payment.call_args
    assert kwargs["notes"] == {}

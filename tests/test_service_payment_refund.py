"""initiate_refund - the KYC-reject/cancel refund trigger. Real money, so every
branch (already-refunded guard, gateway success/failure, manual cash/rtgs_neft/
legacy-razorpay paths, and the edge cases around a payment that was never
actually charged or carries no amount) is covered here without needing a live
Zoho Payments account."""
import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_payment import servicePayment
from DivineService.service_payment_gateway import servicePaymentGateway


def _service(gateway=None):
    persistence = MagicMock()
    svc = servicePayment(persistence, MagicMock(), MagicMock(), gateway=gateway or MagicMock())
    return svc, persistence


def _row(**kw):
    base = {
        "id": "pay1", "status": "paid", "method": "zoho", "amount": 2450000,
        "zoho_payment_id": "zoho_pay_x", "refund_status": "none",
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
    gw = MagicMock()
    gw.create_refund.return_value = {"refund_id": "rfnd_1", "status": "succeeded"}
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(refund_status="failed")
    persistence.update_refund_status.return_value = _row(refund_status="completed")

    svc.initiate_refund("pay1", reason="retry")

    persistence.update_refund_status.assert_called_once()


def test_initiate_refund_zoho_success_sends_decimal_amount_never_paise():
    gw = MagicMock()
    gw.create_refund.return_value = {"refund_id": "rfnd_abc123", "status": "succeeded"}
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(amount=2450000)
    persistence.update_refund_status.return_value = _row(refund_status="completed")

    svc.initiate_refund("pay1", reason="KYC rejected")

    _, call_kwargs = gw.create_refund.call_args
    assert call_kwargs["payment_id"] == "zoho_pay_x"
    # The critical, highest-risk assertion in this whole migration: the gateway
    # must receive a decimal amount, never Razorpay's amount*100 paise convention.
    assert call_kwargs["amount"] == 2450000
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["refund_amount"] == 2450000
    assert kwargs["zoho_refund_id"] == "rfnd_abc123"
    assert kwargs["refund_completed_date"] is not None


def test_initiate_refund_zoho_gateway_failure_marks_pending_not_failed():
    """A gateway error must never silently lose track of an owed refund - it
    settles 'pending' (needs manual retry), never leaves refund_status='none'
    and never raises out to the caller (reject/cancel must still succeed)."""
    gw = MagicMock()
    gw.create_refund.side_effect = RuntimeError("gateway timeout")
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row()
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1", reason="KYC rejected")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "manual retry" in kwargs["refund_note"]


def test_initiate_refund_zoho_without_a_captured_payment_id_completes_as_zero():
    """A plot-booking order that was cancelled before Zoho ever actually
    captured a payment (zoho_payment_id never set) has nothing to refund at
    the gateway - this must complete cleanly, not crash trying to call
    create_refund with no payment id."""
    gw = MagicMock()
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(zoho_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="completed", refund_amount=0)

    svc.initiate_refund("pay1", reason="cancelled before capture")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["refund_amount"] == 0
    gw.create_refund.assert_not_called()


def test_initiate_refund_cash_settles_pending_with_office_collection_note():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="cash", zoho_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1", reason="Docs mismatch")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "collect the cash refund from our office" in kwargs["refund_note"]
    assert "Docs mismatch" in kwargs["refund_note"]


def test_initiate_refund_rtgs_neft_settles_pending_with_manual_transfer_note():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="rtgs_neft", zoho_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "NEFT/RTGS" in kwargs["refund_note"]


def test_initiate_refund_legacy_razorpay_settles_pending_with_manual_note():
    """A pre-cutover razorpay payment has no live gateway to call any more - it
    must go through the exact same manual-bookkeeping path as cash/rtgs_neft,
    with a note explaining why (previous payment partner, gateway retired)."""
    gw = MagicMock()
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(method="razorpay", zoho_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1", reason="Docs mismatch")

    gw.create_refund.assert_not_called()
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "previous payment partner" in kwargs["refund_note"]
    assert "Docs mismatch" in kwargs["refund_note"]


def test_initiate_refund_handles_none_amount_without_crashing():
    """A payment row with no amount (shouldn't happen, but must never 500) -
    the cash/rtgs_neft manual path just records refund_amount=None."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="cash", amount=None, zoho_payment_id=None)
    persistence.update_refund_status.return_value = _row(refund_status="pending", refund_amount=None)

    svc.initiate_refund("pay1")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_amount"] is None


def test_initiate_refund_zoho_corrupt_amount_never_raises_and_settles_pending():
    """Regression guard: a corrupt/non-numeric amount must never escape this
    'never raises' method uncaught - it settles 'pending' like any other
    failed gateway attempt."""
    gw = MagicMock()
    gw.create_refund.side_effect = lambda **kw: (_ for _ in ()).throw(ValueError("bad decimal"))
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(amount="not-a-number")
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    result = svc.initiate_refund("pay1", reason="KYC rejected")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "manual retry" in kwargs["refund_note"]
    assert result is not None


def test_initiate_refund_zoho_retries_once_on_transient_failure_then_succeeds():
    """A single transient gateway error (timeout, blip) must not strand the
    refund at 'pending' - the built-in one-retry should recover it within the
    same call, so most such failures never need an admin's manual retry."""
    gw = MagicMock()
    gw.create_refund.side_effect = [
        RuntimeError("gateway timeout"), {"refund_id": "rfnd_retry_1", "status": "succeeded"},
    ]
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row()
    persistence.update_refund_status.return_value = _row(refund_status="completed")

    svc.initiate_refund("pay1", reason="KYC rejected")

    assert gw.create_refund.call_count == 2
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["zoho_refund_id"] == "rfnd_retry_1"


def test_initiate_refund_zoho_both_attempts_failing_settles_pending():
    gw = MagicMock()
    gw.create_refund.side_effect = RuntimeError("gateway down")
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row()
    persistence.update_refund_status.return_value = _row(refund_status="pending")

    svc.initiate_refund("pay1", reason="KYC rejected")

    assert gw.create_refund.call_count == 2
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "pending"
    assert "manual retry" in kwargs["refund_note"]


def test_initiate_refund_zoho_any_successful_gateway_call_settles_completed():
    """A successful (non-raising) gateway call always settles 'completed' -
    there's no refund webhook wired up in this codebase to later advance a
    finer-grained 'processing' state, so persisting anything less final would
    strand the refund with no path to ever reach 'completed'."""
    gw = MagicMock()
    gw.create_refund.return_value = {"refund_id": "rfnd_async", "status": "initiated"}
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row()
    persistence.update_refund_status.return_value = _row(refund_status="completed")

    svc.initiate_refund("pay1")

    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["refund_completed_date"] is not None


def test_retry_zoho_refund_raises_not_found():
    svc, persistence = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_retry_zoho_refund_raises_not_a_zoho_refund_for_cash():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="cash", refund_status="pending")
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_a_zoho_refund"
    persistence.update_refund_status.assert_not_called()


def test_retry_zoho_refund_raises_razorpay_gateway_retired_for_legacy_payment():
    """A pre-cutover razorpay refund can never be retried against a gateway
    that no longer has credentials - it gets a distinct error directing the
    admin at mark_collected instead of a confusing gateway-auth failure."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(method="razorpay", refund_status="pending")
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "razorpay_gateway_retired"
    persistence.claim_refund_retry.assert_not_called()


def test_retry_zoho_refund_raises_refund_not_retryable_when_not_pending():
    """The claim query itself is the source of truth for eligibility - a
    non-'pending' row fails the atomic claim and is refused, without ever
    touching update_refund_status again or calling the gateway."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="none")
    persistence.claim_refund_retry.return_value = None
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_not_retryable"
    persistence.update_refund_status.assert_not_called()


def test_retry_zoho_refund_raises_refund_not_retryable_when_already_completed():
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="completed")
    persistence.claim_refund_retry.return_value = None
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_not_retryable"


def test_retry_zoho_refund_refuses_to_call_gateway_again_when_a_refund_id_already_exists():
    """Defense in depth against a duplicate gateway refund: the claim query's own
    WHERE clause excludes a row that already has a zoho_refund_id, so this
    never even reaches the gateway call."""
    gw = MagicMock()
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(refund_status="pending", zoho_refund_id="rfnd_existing")
    persistence.claim_refund_retry.return_value = None
    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "refund_not_retryable"
    gw.create_refund.assert_not_called()


def test_retry_zoho_refund_uses_the_atomic_claim_not_the_raw_get_by_id_row():
    """Regression guard for the concurrency fix: retry must act on whatever
    claim_refund_retry's compare-and-swap returns (the source of truth at the
    moment of the claim), not the earlier get_by_id snapshot - two concurrent
    retries must never both pass a plain Python-level check."""
    svc, persistence = _service()
    persistence.get_by_id.return_value = _row(refund_status="pending")
    persistence.claim_refund_retry.return_value = None

    try:
        svc.retry_zoho_refund("pay1")
        assert False, "expected ValueError - the claim losing must be respected"
    except ValueError as e:
        assert str(e) == "refund_not_retryable"
    persistence.claim_refund_retry.assert_called_once_with(id="pay1")


def test_retry_zoho_refund_happy_path_recovers_a_stuck_refund():
    gw = MagicMock()
    gw.create_refund.return_value = {"refund_id": "rfnd_recovered", "status": "succeeded"}
    svc, persistence = _service(gateway=gw)
    persistence.get_by_id.return_value = _row(refund_status="pending")
    persistence.claim_refund_retry.return_value = _row(refund_status="processing")
    persistence.update_refund_status.return_value = _row(refund_status="completed")

    result = svc.retry_zoho_refund("pay1", reason="admin retry")

    gw.create_refund.assert_called_once()
    _, kwargs = persistence.update_refund_status.call_args
    assert kwargs["refund_status"] == "completed"
    assert kwargs["zoho_refund_id"] == "rfnd_recovered"
    assert result.refund_status == "completed"

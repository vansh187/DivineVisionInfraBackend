import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_booking_kyc import serviceBookingKyc


def _service():
    persistence = MagicMock()
    inventory = MagicMock()
    payment_service = MagicMock()
    document_service = MagicMock()
    customer_persistence = MagicMock()
    email_service = MagicMock()
    svc = serviceBookingKyc(
        persistence=persistence, inventory_persistence=inventory, payment_service=payment_service,
        document_service=document_service, customer_persistence=customer_persistence,
        email_service=email_service,
    )
    return svc, persistence, inventory, payment_service, document_service, customer_persistence


def _booking(**kw):
    base = {
        "id": "BKG-2026-000001", "payment_id": "pay1", "inventory_id": "INV-1",
        "customer_id": "C00001", "project_name": "Green Meadows", "unit_number": "A-112",
        "amount": 2450000, "status": "pending_kyc_review", "kyc_status": "pending", "version": 1,
        "admin_note": None, "created_date": None, "last_updated_date": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- approve ----------

def test_approve_raises_not_found():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.approve("BKG-X", admin_id="DV0001", note="ok", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_approve_raises_booking_not_reviewable_for_already_decided():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = _booking(status="booked")
    try:
        svc.approve("BKG-1", admin_id="DV0001", note="ok", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "booking_not_reviewable"


def test_approve_raises_inventory_confirm_failed_when_unit_lookup_misses():
    svc, persistence, inventory, *_ = _service()
    persistence.get_by_id.return_value = _booking()
    inventory.confirm_booking_after_kyc.return_value = None
    try:
        svc.approve("BKG-1", admin_id="DV0001", note="ok", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "inventory_confirm_failed"
    persistence.update_decision.assert_not_called()


def test_approve_version_conflict_rolls_back_with_unbook_not_release(monkeypatch):
    """Regression test: once confirm_booking_after_kyc has already flipped the
    unit to 'booked', the guarded UPDATE behind release_from_kyc_review can never
    match it again (its WHERE clause requires status='pending_kyc_review') - a
    version-conflict rollback must call unbook_unit instead, or the unit is left
    permanently stuck 'booked' with no matching decision record."""
    svc, persistence, inventory, *_ = _service()
    persistence.get_by_id.return_value = _booking()
    inventory.confirm_booking_after_kyc.return_value = SimpleNamespace(id="INV-1", status="booked")
    persistence.update_decision.return_value = None  # lost the optimistic-lock race

    try:
        svc.approve("BKG-1", admin_id="DV0001", note="ok", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "version_conflict"

    inventory.unbook_unit.assert_called_once_with(id="INV-1")
    inventory.release_from_kyc_review.assert_not_called()
    persistence.add_decision.assert_not_called()


def test_approve_happy_path():
    svc, persistence, inventory, payment_service, document_service, customer_persistence = _service()
    updated_booking = _booking(status="booked", kyc_status="verified", version=2)
    # get_by_id is called twice: once by approve() itself (must still see the
    # pre-decision row to pass _require_reviewable), once by the get_detail()
    # it returns at the end (must see the now-updated row) - a real DB read
    # naturally sees this progression; side_effect models it for the mock.
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    inventory.confirm_booking_after_kyc.return_value = SimpleNamespace(id="INV-1", status="booked")
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = SimpleNamespace(
        first_name="Rehan", last_name="Sharma", email="rehan@example.com", phone="9999999998")
    document_service.admin_get_latest.return_value = (None, None, None)
    payment_service.get.return_value = SimpleNamespace(method="razorpay", status="paid",
                                                        razorpay_payment_id="pay_x", utr_number=None)

    detail = svc.approve("BKG-1", admin_id="DV0001", note="All docs verified", expected_version=1)

    inventory.confirm_booking_after_kyc.assert_called_once_with(id="INV-1", payment_id="pay1")
    persistence.update_decision.assert_called_once_with(
        id="BKG-1", expected_version=1, status="booked", kyc_status="verified", admin_note="All docs verified")
    persistence.add_decision.assert_called_once_with(
        "BKG-1", actor="DV0001", action="approved", note="All docs verified")
    assert detail["status"] == "booked"
    assert detail["kyc_status"] == "verified"


# ---------- reject / cancel ----------

def test_reject_releases_plot_and_initiates_refund():
    svc, persistence, inventory, payment_service, document_service, customer_persistence = _service()
    updated_booking = _booking(status="rejected", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = SimpleNamespace(
        first_name="Rehan", last_name="Sharma", email="rehan@example.com", phone="9999999998")
    document_service.admin_get_latest.return_value = (None, None, None)
    payment_service.get.return_value = SimpleNamespace(method="razorpay", status="paid",
                                                        razorpay_payment_id="pay_x", utr_number=None)

    detail = svc.reject("BKG-1", admin_id="DV0001", note="Docs mismatch", expected_version=1)

    inventory.release_from_kyc_review.assert_called_once_with(id="INV-1", payment_id="pay1")
    payment_service.initiate_refund.assert_called_once_with("pay1", reason="Docs mismatch")
    assert detail["status"] == "rejected"
    assert detail["kyc_status"] == "rejected"


def test_reject_refund_failure_does_not_fail_the_reject():
    """The booking decision and plot release already succeeded (the business-
    critical parts) - a refund-kickoff failure must be logged, never turned into
    a 500 that makes the admin think the reject itself failed."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence = _service()
    updated_booking = _booking(status="rejected", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest.return_value = (None, None, None)
    payment_service.initiate_refund.side_effect = RuntimeError("gateway down")

    detail = svc.reject("BKG-1", admin_id="DV0001", note="Docs mismatch", expected_version=1)

    assert detail["status"] == "rejected"
    persistence.add_decision.assert_called_once()


def test_cancel_uses_cancelled_status_and_action():
    svc, persistence, inventory, payment_service, document_service, customer_persistence = _service()
    updated_booking = _booking(status="cancelled", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest.return_value = (None, None, None)

    detail = svc.cancel("BKG-1", admin_id="DV0001", note="Customer asked to cancel", expected_version=1)

    persistence.update_decision.assert_called_once_with(
        id="BKG-1", expected_version=1, status="cancelled", kyc_status="rejected",
        admin_note="Customer asked to cancel")
    persistence.add_decision.assert_called_once_with(
        "BKG-1", actor="DV0001", action="cancelled", note="Customer asked to cancel")
    assert detail["status"] == "cancelled"


def test_reject_raises_booking_not_reviewable_for_already_cancelled():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = _booking(status="cancelled")
    try:
        svc.reject("BKG-1", admin_id="DV0001", note="x", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "booking_not_reviewable"
    persistence.update_decision.assert_not_called()


# ---------- list_mine / get_receipt ----------

def test_list_mine_returns_empty_list_for_no_bookings():
    svc, persistence, *_ = _service()
    persistence.list_by_customer.return_value = []
    assert svc.list_mine("C00001") == []


def test_list_mine_can_download_receipt_only_when_booked():
    svc, persistence, *_ = _service()
    persistence.list_by_customer.return_value = [
        _booking(id="BKG-1", status="pending_kyc_review"),
        _booking(id="BKG-2", status="booked"),
    ]
    result = svc.list_mine("C00001")
    by_id = {r["id"]: r for r in result}
    assert by_id["BKG-1"]["can_download_receipt"] is False
    assert by_id["BKG-2"]["can_download_receipt"] is True


def test_get_receipt_raises_forbidden_for_non_owner():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = _booking(customer_id="C00001")
    try:
        svc.get_receipt("BKG-1", requester_id="C99999")
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_get_receipt_raises_kyc_not_approved_before_booked():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = _booking(customer_id="C00001", status="pending_kyc_review")
    try:
        svc.get_receipt("BKG-1", requester_id="C00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "kyc_not_approved"


def test_get_receipt_delegates_to_payment_service_once_booked():
    svc, persistence, inventory, payment_service, *_ = _service()
    persistence.get_by_id.return_value = _booking(customer_id="C00001", status="booked")
    payment_service.get_receipt.return_value = (b"%PDF-...", "receipt.pdf")

    pdf, filename = svc.get_receipt("BKG-1", requester_id="C00001")

    payment_service.get_receipt.assert_called_once_with("pay1", requester_id="C00001")
    assert pdf == b"%PDF-..."
    assert filename == "receipt.pdf"

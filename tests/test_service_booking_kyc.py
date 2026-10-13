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
    milestone_service = MagicMock()
    milestone_service.enriched_schedule.return_value = {"rows": [], "next_due": None}
    milestone_service.amount_received_rupees.return_value = None
    milestone_persistence = MagicMock()
    svc = serviceBookingKyc(
        persistence=persistence, inventory_persistence=inventory, payment_service=payment_service,
        document_service=document_service, customer_persistence=customer_persistence,
        email_service=email_service, milestone_service=milestone_service,
        milestone_persistence=milestone_persistence,
    )
    return (svc, persistence, inventory, payment_service, document_service, customer_persistence,
            milestone_service, milestone_persistence)


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
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
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
    document_service.admin_get_latest_batch.return_value = {}
    payment_service.get.return_value = SimpleNamespace(method="razorpay", status="paid",
                                                        razorpay_payment_id="pay_x", utr_number=None)

    detail = svc.approve("BKG-1", admin_id="DV0001", note="All docs verified", expected_version=1)

    inventory.confirm_booking_after_kyc.assert_called_once_with(id="INV-1", payment_id="pay1")
    persistence.update_decision.assert_called_once_with(
        id="BKG-1", expected_version=1, status="booked", kyc_status="verified", admin_note="All docs verified")
    persistence.add_decision.assert_called_once_with(
        "BKG-1", actor="DV0001", action="approved", note="All docs verified")
    payment_service.notify_booking_confirmed.assert_called_once_with("pay1", booking=updated_booking)
    assert detail["status"] == "booked"
    assert detail["kyc_status"] == "verified"


def test_list_queue_batches_customer_names():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    rows = [
        _booking(id="BKG-1", customer_id="C00001"),
        _booking(id="BKG-2", customer_id="C00002"),
        _booking(id="BKG-3", customer_id="C00001"),
    ]
    for row in rows:
        row.total_count = 3
    persistence.list_queue.return_value = rows
    customer_persistence.get_by_ids.return_value = [
        SimpleNamespace(id="C00001", first_name="Rehan", last_name="Sharma"),
        SimpleNamespace(id="C00002", first_name="Asha", last_name="Mehta"),
    ]

    result = svc.list_queue(page=1, page_size=100)

    customer_persistence.get_by_ids.assert_called_once_with(["C00001", "C00002"])
    customer_persistence.get_by_id.assert_not_called()
    assert [i["customer_name"] for i in result["items"]] == [
        "Rehan Sharma", "Asha Mehta", "Rehan Sharma",
    ]


# ---------- reject / cancel ----------

def test_reject_releases_plot_and_initiates_refund():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    updated_booking = _booking(status="rejected", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = SimpleNamespace(
        first_name="Rehan", last_name="Sharma", email="rehan@example.com", phone="9999999998")
    document_service.admin_get_latest_batch.return_value = {}
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
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    updated_booking = _booking(status="rejected", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    payment_service.initiate_refund.side_effect = RuntimeError("gateway down")

    detail = svc.reject("BKG-1", admin_id="DV0001", note="Docs mismatch", expected_version=1)

    assert detail["status"] == "rejected"
    persistence.add_decision.assert_called_once()


def test_cancel_uses_cancelled_status_and_action():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    updated_booking = _booking(status="cancelled", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}

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


def test_cancel_an_already_booked_plot_unbooks_instead_of_releasing_from_review():
    """The client's new requirement: cancel must also work once a plot is already
    fully booked (KYC approved), not just while still pending_kyc_review - and it
    must unbook the unit (booked -> available), not call the KYC-review-only
    release path."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    booked = _booking(status="booked", kyc_status="verified", version=2)
    updated_booking = _booking(status="cancelled", kyc_status="verified", version=3)
    persistence.get_by_id.side_effect = [booked, updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}

    detail = svc.cancel("BKG-1", admin_id="DV0001", note="Customer asked to cancel", expected_version=2)

    inventory.unbook_unit_for_payment.assert_called_once_with(id="INV-1", payment_id="pay1")
    inventory.unbook_unit.assert_not_called()
    inventory.release_from_kyc_review.assert_not_called()
    persistence.update_decision.assert_called_once_with(
        id="BKG-1", expected_version=2, status="cancelled", kyc_status="verified",
        admin_note="Customer asked to cancel")
    payment_service.initiate_refund.assert_called_once_with("pay1", reason="Customer asked to cancel")
    assert detail["status"] == "cancelled"


def test_cancel_raises_booking_not_cancellable_for_already_terminal_booking():
    svc, persistence, inventory, *_ = _service()
    persistence.get_by_id.return_value = _booking(status="rejected")
    try:
        svc.cancel("BKG-1", admin_id="DV0001", note="x", expected_version=1)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "booking_not_cancellable"
    persistence.update_decision.assert_not_called()
    inventory.unbook_unit.assert_not_called()
    inventory.release_from_kyc_review.assert_not_called()


def test_cancel_sends_project_specific_cash_office_address():
    """Suraksha Enclave cash-refund cancellations must point the customer to the
    Ganaur site office, not the default OPS Divine Greens office."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    booked = _booking(status="booked", kyc_status="verified", version=1, project_name="Suraksha Enclave")
    updated_booking = _booking(status="cancelled", kyc_status="verified", version=2,
                               project_name="Suraksha Enclave")
    persistence.get_by_id.side_effect = [booked, updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = SimpleNamespace(
        first_name="Rehan", last_name="Sharma", email="rehan@example.com", phone="9999999998")
    document_service.admin_get_latest_batch.return_value = {}
    payment_service.get.return_value = SimpleNamespace(method="cash", status="paid",
                                                        razorpay_payment_id=None, utr_number=None)

    svc.cancel("BKG-1", admin_id="DV0001", note="Customer asked to cancel", expected_version=1)

    email_service = svc._email_service_override
    email_service.send_booking_cancellation_async.assert_called_once()
    kwargs = email_service.send_booking_cancellation_async.call_args.kwargs
    assert "ganaur" in kwargs["refund_instructions"].lower()


def test_cancel_refund_failure_does_not_fail_the_cancel():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    updated_booking = _booking(status="cancelled", kyc_status="rejected", version=2)
    persistence.get_by_id.side_effect = [_booking(), updated_booking]
    persistence.update_decision.return_value = updated_booking
    persistence.list_decisions.return_value = []
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    payment_service.initiate_refund.side_effect = RuntimeError("gateway down")

    detail = svc.cancel("BKG-1", admin_id="DV0001", note="Customer asked to cancel", expected_version=1)

    assert detail["status"] == "cancelled"
    persistence.add_decision.assert_called_once()


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


# ---------- get_detail: payment_schedule ----------

def test_get_detail_includes_payment_schedule_from_milestone_service():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, _ = _service()
    persistence.get_by_id.return_value = _booking(status="booked", payment_id="pay1")
    persistence.list_decisions.return_value = []
    payment_service.get.return_value = None
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    # The booking-application DOCUMENT's own id - NOT the divine_bookings.id - is
    # what serviceMilestones calls "booking_id" (see
    # service_document.py::get_booking_application_document_id).
    document_service.get_booking_application_document_id.return_value = "DOC-APP-1"
    milestone_rows = [SimpleNamespace(id="DOC-APP-1-m2", booking_id="DOC-APP-1")]
    milestone_service.ensure_for_customer.return_value = milestone_rows
    schedule_row = {
        "id": "DOC-APP-1-m2", "label": "Within 45 days", "percent": 15, "due_days": 45,
        "due_date": "2026-01-01", "amount": 300000, "status": "due",
        "pay_enabled_from": "2025-12-27", "paid_on": None, "paid_payment_id": None,
    }
    milestone_service.enriched_schedule.return_value = {"rows": [schedule_row], "next_due": schedule_row}
    milestone_service.amount_received_rupees.return_value = 245000.0

    detail = svc.get_detail("BKG-1")

    document_service.get_booking_application_document_id.assert_called_once_with("pay1")
    milestone_service.ensure_for_customer.assert_called_once_with("C00001")
    # ensure_for_customer's result is reused (rows=) by both calls below instead of
    # each one re-fetching the customer's milestones from the DB itself - was 3
    # round trips to the milestones table per page load, now 1.
    milestone_service.enriched_schedule.assert_called_once_with(
        "C00001", booking_id="DOC-APP-1", rows=milestone_rows)
    milestone_service.amount_received_rupees.assert_called_once_with(
        "C00001", booking_id="DOC-APP-1", rows=milestone_rows)
    assert detail["payment_schedule"] == [schedule_row]
    assert detail["next_due"] == schedule_row
    assert detail["amount_received"] == 245000.0


def test_get_detail_empty_schedule_when_no_booking_application_uploaded_yet():
    """No booking-application document uploaded against this payment yet -> []
    rather than an unscoped lookup that could leak a DIFFERENT plot's schedule
    for a multi-plot customer."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, _ = _service()
    persistence.get_by_id.return_value = _booking(status="booked", payment_id="pay1")
    persistence.list_decisions.return_value = []
    payment_service.get.return_value = None
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    document_service.get_booking_application_document_id.return_value = None

    detail = svc.get_detail("BKG-1")

    milestone_service.enriched_schedule.assert_not_called()
    assert detail["payment_schedule"] == []
    assert detail["next_due"] is None
    assert detail["amount_received"] is None


def test_get_detail_degrades_to_empty_schedule_on_milestone_service_failure():
    """A milestone-service hiccup must never break the whole booking-detail page."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, _ = _service()
    persistence.get_by_id.return_value = _booking(status="booked", payment_id="pay1")
    persistence.list_decisions.return_value = []
    payment_service.get.return_value = None
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    document_service.get_booking_application_document_id.return_value = "DOC-APP-1"
    milestone_service.enriched_schedule.side_effect = RuntimeError("db exploded")

    detail = svc.get_detail("BKG-1")

    assert detail["payment_schedule"] == []
    assert detail["next_due"] is None
    assert detail["amount_received"] is None


def test_get_detail_degrades_to_empty_schedule_when_milestone_service_unavailable():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, *_ = _service()
    svc._milestones = lambda: None  # simulates _milestones()'s own except-branch returning None
    persistence.get_by_id.return_value = _booking(status="booked")
    persistence.list_decisions.return_value = []
    payment_service.get.return_value = None
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}

    detail = svc.get_detail("BKG-1")

    assert detail["payment_schedule"] == []
    assert detail["next_due"] is None
    assert detail["amount_received"] is None


# ---------- record_installment_payment ----------

def _milestone(**kw):
    # "booking_id" here is the booking-APPLICATION DOCUMENT's id (see
    # service_document.py::get_booking_application_document_id) - never
    # divine_bookings.id - "DOC-APP-1" is the matching document id the
    # happy-path tests below mock get_booking_application_document_id to return.
    base = {
        "id": "DOC-APP-1-m2", "booking_id": "DOC-APP-1", "customer_id": "C00001",
        "inventory_id": "INV-1", "milestone_no": 2, "label": "Within 45 days",
        "due_date": "2026-01-01", "amount": 300000, "status": "due",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_record_installment_payment_raises_not_found():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.record_installment_payment("BKG-X", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_record_installment_payment_raises_booking_not_booked():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = _booking(status="pending_kyc_review")
    try:
        svc.record_installment_payment("BKG-1", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "booking_not_booked"


def test_record_installment_payment_raises_milestone_not_found_when_missing():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, milestone_persistence = _service()
    persistence.get_by_id.return_value = _booking(status="booked")
    milestone_persistence.get.return_value = None
    try:
        svc.record_installment_payment("BKG-2026-000001", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "milestone_not_found"


def test_record_installment_payment_raises_milestone_not_found_for_wrong_booking():
    """A milestone id that belongs to a DIFFERENT booking-application document
    must be rejected, even if it exists - defends against an admin
    pasting/guessing another booking's milestone id."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, milestone_persistence = _service()
    persistence.get_by_id.return_value = _booking(id="BKG-2026-000001", status="booked", payment_id="pay1")
    document_service.get_booking_application_document_id.return_value = "DOC-APP-1"
    milestone_persistence.get.return_value = _milestone(booking_id="DOC-APP-OTHER")
    try:
        svc.record_installment_payment("BKG-2026-000001", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "milestone_not_found"


def test_record_installment_payment_raises_milestone_not_found_when_no_booking_application_uploaded():
    """No booking-application document uploaded against this payment yet -> no
    valid "booking_id" to match against, so any milestone_id is rejected."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, milestone_persistence = _service()
    persistence.get_by_id.return_value = _booking(status="booked", payment_id="pay1")
    document_service.get_booking_application_document_id.return_value = None
    milestone_persistence.get.return_value = _milestone()
    try:
        svc.record_installment_payment("BKG-2026-000001", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "milestone_not_found"


def test_record_installment_payment_happy_path_calls_record_cash_payment_and_returns_detail():
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, milestone_persistence = _service()
    booking = _booking(id="BKG-2026-000001", status="booked", customer_id="C00001",
                       inventory_id="INV-1", payment_id="pay1")
    persistence.get_by_id.return_value = booking
    persistence.list_decisions.return_value = []
    payment_service.get.return_value = None
    customer_persistence.get_by_id.return_value = None
    document_service.admin_get_latest_batch.return_value = {}
    document_service.get_booking_application_document_id.return_value = "DOC-APP-1"
    milestone_persistence.get.return_value = _milestone()

    detail = svc.record_installment_payment(
        "BKG-2026-000001", admin_id="DV0001", milestone_id="BKG-2026-000001-m2", amount=300000,
        method="cheque", cheque_number="CHQ123",
    )

    payment_service.record_cash_payment.assert_called_once_with(
        300000, owner_id="C00001", owner_role="customer", note=None,
        purpose="installment", inventory_id="INV-1",
        installment_no=2, due_date="2026-01-01",
        method="cheque", utr_number=None, cheque_number="CHQ123",
        recorded_by_admin_id="DV0001",
    )
    assert detail["id"] == "BKG-2026-000001"


def test_record_installment_payment_propagates_record_cash_payment_errors():
    """A guard-rail ValueError from record_cash_payment (e.g. the milestone is
    already paid) must propagate unchanged, not be swallowed/rewrapped."""
    svc, persistence, inventory, payment_service, document_service, customer_persistence, milestone_service, milestone_persistence = _service()
    persistence.get_by_id.return_value = _booking(status="booked", payment_id="pay1")
    document_service.get_booking_application_document_id.return_value = "DOC-APP-1"
    milestone_persistence.get.return_value = _milestone()
    payment_service.record_cash_payment.side_effect = ValueError("installment_already_paid")

    try:
        svc.record_installment_payment("BKG-2026-000001", admin_id="DV0001", milestone_id="m2", amount=300000)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "installment_already_paid"


# ---------- get_allotment_letter ----------

def test_get_allotment_letter_raises_not_found_for_missing_booking():
    svc, persistence, *_ = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.get_allotment_letter("BKG-X")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_get_allotment_letter_raises_not_uploaded_when_no_application_pdf():
    svc, persistence, inventory, payment_service, document_service, *_ = _service()
    persistence.get_by_id.return_value = _booking()
    document_service.get_booking_application_bytes.return_value = (None, None)
    try:
        svc.get_allotment_letter("BKG-1")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_uploaded"


def test_get_allotment_letter_returns_stored_pdf_bytes():
    svc, persistence, inventory, payment_service, document_service, *_ = _service()
    persistence.get_by_id.return_value = _booking(payment_id="pay1")
    document_service.get_booking_application_bytes.return_value = (b"%PDF-...", "booking_application.pdf")

    pdf, filename = svc.get_allotment_letter("BKG-1")

    document_service.get_booking_application_bytes.assert_called_once_with("pay1")
    assert pdf == b"%PDF-..."
    assert filename == "booking_application.pdf"

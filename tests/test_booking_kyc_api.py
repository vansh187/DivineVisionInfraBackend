"""End-to-end functional coverage for the Booking KYC review workflow, over
real HTTP through TestClient against a real (SQLite) database - complements
the mocked unit tests in test_service_booking_kyc.py and
test_service_payment_refund.py. Exercises: payment settlement creating a
held booking, the admin queue/detail/approve/reject/cancel endpoints, the
customer's own /bookings/mine + receipt download, auth boundaries, and the
optimistic-concurrency 409."""
import os
import uuid
from datetime import date, datetime, timedelta, timezone
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_customer import persistenceCustomer
from DivineService.service_payment import servicePayment
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_ID = None
_UNIT_ID = None
_PAYMENT_RECORD = None
_BOOKING_ID = None


def setup_module(module):
    global _CUSTOMER_ID, _UNIT_ID, _PAYMENT_RECORD, _BOOKING_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer = persistenceCustomer().create_user(
        username="booking_kyc_customer", password_hash="not-used-by-these-tests",
        phone="9000000020", email="booking.kyc@example.com", first_name="Rehan", last_name="Sharma",
    )
    _CUSTOMER_ID = customer.id

    inv = persistenceInventory()
    _UNIT_ID = str(uuid.uuid4())
    inv.upsert_unit(
        id=_UNIT_ID, project_name="Green Meadows", city="Karnal", unit_number="A-112",
        area_sqmt=200, area_sqyd=239, status="available",
    )

    # rtgs_neft is the "trusted" manual method (see servicePayment._booking_flip_trusted) -
    # inserted straight through the service layer rather than a real POST /payments/cash
    # HTTP call, to avoid spending that path's shared rate-limit budget.
    _PAYMENT_RECORD = servicePayment().record_cash_payment(
        amount=2450000, owner_id=_CUSTOMER_ID, owner_role="customer",
        purpose="plot_booking", inventory_id=_UNIT_ID, method="rtgs_neft", utr_number="UTR000111222",
    )
    _BOOKING_ID = _PAYMENT_RECORD.booking_id
    assert _BOOKING_ID, "setup did not create a booking - payment flow is broken"


def _admin_headers():
    payload = {
        "sub": "DV1234", "username": "kyc_admin@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "admintestsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(customer_id: str = None):
    payload = {
        "sub": customer_id or _CUSTOMER_ID, "username": "booking_kyc_customer",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "testsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _broker_headers():
    payload = {
        "sub": "B00099", "username": "some_broker",
        "role": "broker", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, 'testsecret', algorithm='HS256')}"}


# ---------- setup sanity ----------

def test_setup_holds_unit_for_kyc_review_not_booked():
    inv = persistenceInventory().get_by_id(_UNIT_ID)
    assert inv.status == "pending_kyc_review"


# ---------- auth boundaries ----------

def test_admin_queue_requires_auth():
    r = client.get("/admin/bookings")
    assert r.status_code == 401, r.text


def test_admin_queue_rejects_customer_token():
    r = client.get("/admin/bookings", headers=_customer_headers())
    assert r.status_code == 401, r.text


def test_bookings_mine_requires_auth():
    r = client.get("/bookings/mine")
    assert r.status_code == 401, r.text


def test_bookings_mine_rejects_broker_token():
    r = client.get("/bookings/mine", headers=_broker_headers())
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "bookings_customer_only"


def test_approve_requires_admin_auth():
    r = client.post(f"/admin/bookings/{_BOOKING_ID}/approve", json={"note": "x", "version": 1})
    assert r.status_code == 401, r.text


# ---------- admin queue / detail ----------

def test_admin_queue_lists_the_new_booking():
    r = client.get("/admin/bookings", params={"status": "pending_kyc_review"}, headers=_admin_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    ids = [item["id"] for item in data["items"]]
    assert _BOOKING_ID in ids
    item = next(i for i in data["items"] if i["id"] == _BOOKING_ID)
    assert item["kyc_status"] == "pending"
    assert item["version"] == 1
    assert item["customer_name"] == "Rehan Sharma"


def test_admin_detail_includes_document_checklist_and_decision_history():
    r = client.get(f"/admin/bookings/{_BOOKING_ID}", headers=_admin_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "pending_kyc_review"
    doc_types = {d["document_type"] for d in data["documents"]}
    assert doc_types == {"aadhaar_front", "aadhaar_back", "pan_card", "applicant_photo", "cancelled_cheque"}
    assert all(d["uploaded"] is False for d in data["documents"])  # none uploaded in this test
    assert len(data["decision_history"]) == 1
    assert data["decision_history"][0]["action"] == "payment_received"
    assert data["utr_number"] == "UTR000111222"
    assert data["payment_method"] == "rtgs_neft"


def test_admin_detail_not_found():
    r = client.get("/admin/bookings/BKG-DOES-NOT-EXIST", headers=_admin_headers())
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "not_found"


# ---------- customer view before a decision ----------

def test_customer_sees_pending_review_and_cannot_download_receipt_yet():
    r = client.get("/bookings/mine", headers=_customer_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    item = next(i for i in data if i["id"] == _BOOKING_ID)
    assert item["status"] == "pending_kyc_review"
    assert item["can_download_receipt"] is False

    receipt = client.get(f"/bookings/{_BOOKING_ID}/receipt", headers=_customer_headers())
    assert receipt.status_code == 400, receipt.text
    assert receipt.json()["detail"] == "kyc_not_approved"


def test_receipt_download_requires_owner():
    other_customer = persistenceCustomer().create_user(
        username="booking_kyc_other_customer", password_hash="x",
        phone="9000000021", email="other@example.com",
    )
    r = client.get(f"/bookings/{_BOOKING_ID}/receipt", headers=_customer_headers(other_customer.id))
    assert r.status_code == 403, r.text


# ---------- decision validation ----------

def test_approve_rejects_stale_version_with_409():
    r = client.post(
        f"/admin/bookings/{_BOOKING_ID}/approve", json={"note": "ok", "version": 999},
        headers=_admin_headers(),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "version_conflict"

    # Regression: a rejected-version approve attempt must NEVER touch inventory -
    # the plot must still be exactly as it was (held for review), not stranded
    # 'available' by a rollback that can't restore the precise hold state, which
    # would then permanently break even a later CORRECT-version approve.
    inv = persistenceInventory().get_by_id(_UNIT_ID)
    assert inv.status == "pending_kyc_review"
    assert inv.booked_payment_id == _PAYMENT_RECORD.id


def test_approve_requires_a_positive_version_field():
    r = client.post(
        f"/admin/bookings/{_BOOKING_ID}/approve", json={"note": "ok", "version": 0},
        headers=_admin_headers(),
    )
    assert r.status_code == 422, r.text


def test_approve_missing_booking_returns_404():
    r = client.post(
        "/admin/bookings/BKG-DOES-NOT-EXIST/approve", json={"note": "ok", "version": 1},
        headers=_admin_headers(),
    )
    assert r.status_code == 404, r.text


# ---------- happy path: approve -> booked -> receipt unlocked ----------

def test_approve_happy_path_books_the_plot_and_unlocks_the_receipt():
    r = client.post(
        f"/admin/bookings/{_BOOKING_ID}/approve",
        json={"note": "All KYC documents verified", "version": 1},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "booked"
    assert data["kyc_status"] == "verified"
    assert data["version"] == 2
    assert any(d["action"] == "approved" for d in data["decision_history"])

    inv = persistenceInventory().get_by_id(_UNIT_ID)
    assert inv.status == "booked"

    receipt = client.get(f"/bookings/{_BOOKING_ID}/receipt", headers=_customer_headers())
    assert receipt.status_code == 200, receipt.text
    assert receipt.headers["content-type"] == "application/pdf"
    assert len(receipt.content) > 0

    mine = client.get("/bookings/mine", headers=_customer_headers())
    item = next(i for i in mine.json() if i["id"] == _BOOKING_ID)
    assert item["can_download_receipt"] is True


def test_re_approving_an_already_decided_booking_returns_409():
    r = client.post(
        f"/admin/bookings/{_BOOKING_ID}/approve", json={"note": "again", "version": 2},
        headers=_admin_headers(),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "booking_not_reviewable"


# ---------- reject path on a second booking ----------

def test_reject_stale_version_never_touches_inventory():
    unit_stale_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit_stale_id, project_name="Green Meadows", city="Karnal", unit_number="A-STALE",
        area_sqmt=200, area_sqyd=239, status="available",
    )
    payment_stale = servicePayment().record_cash_payment(
        amount=500000, owner_id=_CUSTOMER_ID, owner_role="customer",
        purpose="plot_booking", inventory_id=unit_stale_id, method="rtgs_neft", utr_number="UTR999000111",
    )
    booking_stale_id = payment_stale.booking_id
    assert booking_stale_id

    r = client.post(
        f"/admin/bookings/{booking_stale_id}/reject", json={"note": "x", "version": 999},
        headers=_admin_headers(),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "version_conflict"

    inv = persistenceInventory().get_by_id(unit_stale_id)
    assert inv.status == "pending_kyc_review"

    # A correct-version reject must still work fine afterward - the plot was
    # never touched by the failed stale-version attempt above.
    r2 = client.post(
        f"/admin/bookings/{booking_stale_id}/reject", json={"note": "x", "version": 1},
        headers=_admin_headers(),
    )
    assert r2.status_code == 200, r2.text
    inv_after = persistenceInventory().get_by_id(unit_stale_id)
    assert inv_after.status == "available"


def test_reject_happy_path_releases_the_plot_and_initiates_refund():
    unit2_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit2_id, project_name="Green Meadows", city="Karnal", unit_number="A-113",
        area_sqmt=200, area_sqyd=239, status="available",
    )
    payment2 = servicePayment().record_cash_payment(
        amount=1000000, owner_id=_CUSTOMER_ID, owner_role="customer",
        purpose="plot_booking", inventory_id=unit2_id, method="rtgs_neft", utr_number="UTR333444555",
    )
    booking2_id = payment2.booking_id
    assert booking2_id

    r = client.post(
        f"/admin/bookings/{booking2_id}/reject",
        json={"note": "PAN and Aadhaar names do not match", "version": 1},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "rejected"
    assert data["kyc_status"] == "rejected"
    assert any(d["action"] == "rejected" for d in data["decision_history"])

    unit_after = persistenceInventory().get_by_id(unit2_id)
    assert unit_after.status == "available"
    assert unit_after.booked_payment_id is None

    from Divinepersistence.persistence_payment import persistencePayment
    payment_after = persistencePayment().get_by_id(payment2.id)
    assert payment_after.refund_status == "pending"
    assert "NEFT/RTGS" in (payment_after.refund_note or "")

    receipt = client.get(f"/bookings/{booking2_id}/receipt", headers=_customer_headers())
    assert receipt.status_code == 400, receipt.text
    assert receipt.json()["detail"] == "kyc_not_approved"


# ---------- cancel path on a third booking ----------

def test_cancel_happy_path_releases_the_plot():
    unit3_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit3_id, project_name="Green Meadows", city="Karnal", unit_number="A-114",
        area_sqmt=200, area_sqyd=239, status="available",
    )
    payment3 = servicePayment().record_cash_payment(
        amount=1200000, owner_id=_CUSTOMER_ID, owner_role="customer",
        purpose="plot_booking", inventory_id=unit3_id, method="rtgs_neft", utr_number="UTR666777888",
    )
    booking3_id = payment3.booking_id
    assert booking3_id

    r = client.post(
        f"/admin/bookings/{booking3_id}/cancel",
        json={"note": "Customer requested cancellation", "version": 1},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "cancelled"
    assert any(d["action"] == "cancelled" for d in data["decision_history"])

    unit_after = persistenceInventory().get_by_id(unit3_id)
    assert unit_after.status == "available"


def test_cancel_an_already_booked_plot_unbooks_it_and_initiates_cash_refund():
    """The client's core new requirement: cancel isn't just for a booking still
    under KYC review - an admin must be able to cancel a plot that's already
    fully booked (KYC approved), release it back to available, and kick off a
    refund. Suraksha Enclave here also exercises the Ganaur-office cash-pickup
    address selection."""
    unit4_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit4_id, project_name="Suraksha Enclave", city="Sonipat", unit_number="C-9",
        area_sqmt=180, area_sqyd=215, status="available",
    )
    # owner_role="broker" is required for a cash payment to be trusted enough to
    # flip the unit into KYC review immediately (see servicePayment._booking_flip_trusted -
    # a customer's own self-reported cash entry is deliberately NOT trusted); the
    # booking's customer_id still ends up as owner_id, so this still lands as
    # _CUSTOMER_ID's booking for the rest of the test.
    payment4 = servicePayment().record_cash_payment(
        amount=900000, owner_id=_CUSTOMER_ID, owner_role="broker",
        purpose="plot_booking", inventory_id=unit4_id, method="cash",
    )
    booking4_id = payment4.booking_id
    assert booking4_id

    approve = client.post(
        f"/admin/bookings/{booking4_id}/approve",
        json={"note": "All KYC documents verified", "version": 1},
        headers=_admin_headers(),
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "booked"

    inv_booked = persistenceInventory().get_by_id(unit4_id)
    assert inv_booked.status == "booked"

    cancel = client.post(
        f"/admin/bookings/{booking4_id}/cancel",
        json={"note": "Customer requested cancellation post-booking", "version": 2},
        headers=_admin_headers(),
    )
    assert cancel.status_code == 200, cancel.text
    data = cancel.json()
    assert data["status"] == "cancelled"
    assert data["kyc_status"] == "verified"
    assert any(d["action"] == "cancelled" for d in data["decision_history"])

    inv_after = persistenceInventory().get_by_id(unit4_id)
    assert inv_after.status == "available"
    assert inv_after.booked_payment_id is None

    from Divinepersistence.persistence_payment import persistencePayment
    payment_after = persistencePayment().get_by_id(payment4.id)
    assert payment_after.refund_status == "pending"
    assert "office" in (payment_after.refund_note or "").lower()

    # A second cancel on an already-cancelled booking is rejected, not silently re-applied.
    recancel = client.post(
        f"/admin/bookings/{booking4_id}/cancel",
        json={"note": "again", "version": 3},
        headers=_admin_headers(),
    )
    assert recancel.status_code == 409, recancel.text
    assert recancel.json()["detail"] == "booking_not_cancellable"

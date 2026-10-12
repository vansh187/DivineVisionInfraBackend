"""End-to-end functional coverage for POST /admin/payments/{payment_id}/refund/retry
over real HTTP through TestClient against a real (SQLite) database - complements
the mocked unit tests in test_service_payment_refund.py. Exercises: the
admin-only auth boundary, and that a Zoho refund genuinely stuck at
refund_status='pending' (both of initiate_refund's own gateway attempts having
already failed) can be recovered through this endpoint without any direct DB
access."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_payment import persistencePayment
from DivineService.service_payment_gateway import servicePaymentGateway
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_ID = None


def setup_module(module):
    global _CUSTOMER_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer = persistenceCustomer().create_user(
        username="admin_payments_retry_customer", password_hash="not-used-by-these-tests",
        phone="9000000030", email="admin.payments.retry@example.com",
        first_name="Kavya", last_name="Rao",
    )
    _CUSTOMER_ID = customer.id


def _admin_headers():
    payload = {
        "sub": "DV9001", "username": "payments_admin@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "admintestsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _customer_headers():
    payload = {
        "sub": _CUSTOMER_ID, "username": "admin_payments_retry_customer",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "testsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _make_stuck_zoho_refund(amount=500000):
    """A 'paid' Zoho payment whose refund already failed at the gateway once
    (both of initiate_refund's own attempts) and is now stranded at
    refund_status='pending' - the exact state the retry endpoint exists for."""
    payments = persistencePayment()
    record = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer", amount=amount,
        currency="INR", status="created", zoho_payments_session_id=f"session_{uuid.uuid4().hex[:12]}",
        method="zoho",
    )
    payments.update_payment_status(
        id=record.id, status="paid", zoho_payment_id=f"zoho_pay_{uuid.uuid4().hex[:12]}",
        zoho_signature=None,
    )
    payments.update_refund_status(
        id=record.id, refund_status="pending", refund_amount=amount,
        refund_initiated_date=datetime.now(timezone.utc),
        refund_note="Automatic refund failed (gateway timeout) - needs manual retry.",
    )
    return payments.get_by_id(record.id)


def _make_stuck_legacy_razorpay_refund(amount=400000):
    """A pre-cutover razorpay payment stuck at refund_status='pending' - unlike
    a live 'zoho' refund, this can never be retried through the gateway again
    (credentials/package retired at cutover)."""
    payments = persistencePayment()
    record = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer", amount=amount,
        currency="INR", status="created", method="razorpay",
    )
    payments.seed_legacy_razorpay_fields(
        record.id, razorpay_order_id=f"order_{uuid.uuid4().hex[:12]}",
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
    )
    payments.update_payment_status(id=record.id, status="paid", zoho_payment_id=None, zoho_signature=None)
    payments.update_refund_status(
        id=record.id, refund_status="pending", refund_amount=amount,
        refund_initiated_date=datetime.now(timezone.utc), refund_note="gateway timeout",
    )
    return payments.get_by_id(record.id)


def test_retry_refund_rejects_non_admin_token():
    stuck = _make_stuck_zoho_refund()
    r = client.post(f"/admin/payments/{stuck.id}/refund/retry", headers=_customer_headers())
    assert r.status_code == 401, r.text


def test_retry_refund_returns_404_for_unknown_payment():
    r = client.post("/admin/payments/does-not-exist/refund/retry", headers=_admin_headers())
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "not_found"


def test_retry_refund_happy_path_recovers_a_stuck_refund():
    stuck = _make_stuck_zoho_refund(amount=750000)
    with patch.object(servicePaymentGateway, "create_refund") as mock_create_refund:
        mock_create_refund.return_value = {"refund_id": "rfnd_e2e_1", "status": "succeeded"}
        r = client.post(f"/admin/payments/{stuck.id}/refund/retry", headers=_admin_headers())

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["refund_status"] == "completed"
    assert data["zoho_refund_id"] == "rfnd_e2e_1"
    assert data["refund_amount"] == 750000

    persisted = persistencePayment().get_by_id(stuck.id)
    assert persisted.refund_status == "completed"


def test_retry_refund_on_a_non_pending_payment_returns_409():
    stuck = _make_stuck_zoho_refund()
    with patch.object(servicePaymentGateway, "create_refund") as mock_create_refund:
        mock_create_refund.return_value = {"refund_id": "rfnd_e2e_2", "status": "succeeded"}
        first = client.post(f"/admin/payments/{stuck.id}/refund/retry", headers=_admin_headers())
    assert first.status_code == 200, first.text

    # Already completed now - a second retry must be refused, not silently
    # re-trigger another gateway refund.
    second = client.post(f"/admin/payments/{stuck.id}/refund/retry", headers=_admin_headers())
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "refund_not_retryable"


def test_claim_refund_retry_is_atomic_against_a_concurrent_claim():
    """Direct persistence-level proof of the concurrency fix: once one caller's
    claim_refund_retry lands, a second claim on the same row (simulating a
    double-click or two admins racing) must find nothing to claim - never both
    succeed and never both be allowed to call the Zoho gateway."""
    stuck = _make_stuck_zoho_refund()
    payments = persistencePayment()

    first = payments.claim_refund_retry(id=stuck.id)
    assert first is not None
    assert first.refund_status == "processing"

    second = payments.claim_refund_retry(id=stuck.id)
    assert second is None


def test_retry_refund_on_a_cash_payment_returns_409():
    payments = persistencePayment()
    record = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer", amount=200000,
        currency="INR", status="paid", method="cash",
    )
    payments.update_refund_status(
        id=record.id, refund_status="pending", refund_amount=200000,
        refund_initiated_date=datetime.now(timezone.utc), refund_note="collect from office",
    )
    r = client.post(f"/admin/payments/{record.id}/refund/retry", headers=_admin_headers())
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "not_a_zoho_refund"


def test_retry_refund_on_a_legacy_razorpay_payment_returns_409_gateway_retired():
    stuck = _make_stuck_legacy_razorpay_refund()
    r = client.post(f"/admin/payments/{stuck.id}/refund/retry", headers=_admin_headers())
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "razorpay_gateway_retired"

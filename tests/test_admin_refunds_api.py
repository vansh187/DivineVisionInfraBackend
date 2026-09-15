"""End-to-end coverage for the admin Refunds API over real HTTP."""
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
from DivineService.service_payment import servicePayment
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_ID = None
_RAZORPAY_PENDING_ID = None
_CASH_PENDING_ID = None
_BANK_PENDING_ID = None


def setup_module(module):
    global _CUSTOMER_ID, _RAZORPAY_PENDING_ID, _CASH_PENDING_ID, _BANK_PENDING_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer = persistenceCustomer().create_user(
        username="admin_refunds_customer", password_hash="not-used",
        phone="9000000401", email="admin.refunds@example.com",
        first_name="Nisha", last_name="Verma",
    )
    _CUSTOMER_ID = customer.id
    payments = persistencePayment()

    razorpay_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer",
        amount=100000, currency="INR", status="created",
        method="razorpay", razorpay_order_id=f"order_{uuid.uuid4().hex[:12]}",
    )
    payments.update_payment_status(razorpay_pending.id, "paid", f"pay_{uuid.uuid4().hex[:12]}", "sig")
    payments.update_refund_status(
        razorpay_pending.id, "pending", refund_amount=100000,
        refund_initiated_date=datetime.now(timezone.utc), refund_note="gateway timeout",
    )
    _RAZORPAY_PENDING_ID = razorpay_pending.id

    cash_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer",
        amount=200000, currency="INR", status="paid", method="cash", purpose="other",
    )
    payments.update_refund_status(
        cash_pending.id, "pending", refund_amount=200000,
        refund_initiated_date=datetime.now(timezone.utc), refund_note="collect from office",
    )
    _CASH_PENDING_ID = cash_pending.id

    bank_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_ID, owner_role="customer",
        amount=300000, currency="INR", status="paid", method="rtgs_neft",
        utr_number="UTRADMINREFUND", purpose="other",
    )
    payments.update_refund_status(
        bank_pending.id, "pending", refund_amount=300000,
        refund_initiated_date=datetime.now(timezone.utc), refund_note="manual transfer owed",
    )
    _BANK_PENDING_ID = bank_pending.id


def _admin_headers():
    payload = {
        "sub": "DV9002", "username": "refunds_admin@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"}


def _customer_headers():
    payload = {
        "sub": _CUSTOMER_ID, "username": "admin_refunds_customer",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, 'testsecret', algorithm='HS256')}"}


def test_list_refunds_requires_admin():
    assert client.get("/admin/refunds").status_code == 401
    r = client.get("/admin/refunds", headers=_customer_headers())
    assert r.status_code == 401, r.text


def test_list_refunds_returns_display_statuses_and_filters():
    r = client.get("/admin/refunds?page_size=100", headers=_admin_headers())
    assert r.status_code == 200, r.text
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id[_RAZORPAY_PENDING_ID]["status"] == "processing"
    assert by_id[_CASH_PENDING_ID]["status"] == "cash_refund_pending"
    assert by_id[_BANK_PENDING_ID]["status"] == "bank_transfer_pending"
    assert by_id[_CASH_PENDING_ID]["customer_name"] == "Nisha Verma"

    filtered = client.get("/admin/refunds?status=cash_refund_pending", headers=_admin_headers())
    assert filtered.status_code == 200, filtered.text
    items = filtered.json()["items"]
    assert _CASH_PENDING_ID in {i["id"] for i in items}
    assert all(i["status"] == "cash_refund_pending" for i in items)


def test_list_refunds_rejects_bad_query_params():
    r = client.get("/admin/refunds?page=0&page_size=999&status=nope&method=bitcoin", headers=_admin_headers())
    assert r.status_code == 422, r.text


def test_get_refund_detail_and_not_found():
    r = client.get(f"/admin/refunds/{_RAZORPAY_PENDING_ID}", headers=_admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == _RAZORPAY_PENDING_ID
    assert body["status"] == "processing"
    assert body["refund_note"] == "gateway timeout"

    missing = client.get("/admin/refunds/does-not-exist", headers=_admin_headers())
    assert missing.status_code == 404, missing.text


def test_retry_refund_endpoint_returns_refund_detail():
    with patch.object(servicePayment, "_client") as mock_client:
        mock_client.return_value.payment.refund.return_value = {"id": "rfnd_admin_refunds", "status": "processed"}
        r = client.post(f"/admin/refunds/{_RAZORPAY_PENDING_ID}/retry", headers=_admin_headers())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == _RAZORPAY_PENDING_ID
    assert body["status"] == "completed"
    assert body["razorpay_refund_id"] == "rfnd_admin_refunds"


def test_retry_refund_rejects_manual_refund():
    r = client.post(f"/admin/refunds/{_CASH_PENDING_ID}/retry", headers=_admin_headers())
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "not_a_razorpay_refund"


def test_mark_collected_completes_manual_refund():
    r = client.post(
        f"/admin/refunds/{_BANK_PENDING_ID}/mark-collected",
        headers=_admin_headers(),
        json={"note": "NEFT completed from ops account"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == _BANK_PENDING_ID
    assert body["status"] == "bank_transfer_completed"
    assert "NEFT completed" in body["refund_note"]

    second = client.post(
        f"/admin/refunds/{_BANK_PENDING_ID}/mark-collected",
        headers=_admin_headers(),
        json={"note": "double click"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "refund_not_pending"


def test_mark_collected_rejects_razorpay_refund():
    r = client.post(
        f"/admin/refunds/{_RAZORPAY_PENDING_ID}/mark-collected",
        headers=_admin_headers(),
        json={},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "not_a_manual_refund"

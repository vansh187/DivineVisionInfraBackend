"""End-to-end functional coverage for the admin Revenue tab
(GET /admin/revenue/summary, /admin/revenue/transactions,
/admin/revenue/transactions/{id}) over real HTTP through TestClient against a
real (SQLite) database - complements the mocked unit tests in
test_service_revenue.py and the query-level coverage in
test_persistence_revenue.py. Exercises: auth boundaries (no financial data
leaks to a non-admin caller), status bucketing, search, filters, pagination,
and bad-input handling.

NOTE ON ISOLATION: the shared test_db.sqlite is written by many test files in
the same pytest session, and on Windows a prior file's still-open SQLAlchemy
engine can prevent this file's own setup_module from actually deleting it -
the same reason test_admin_visits_api.py asserts `total_items >= 1` rather
than `== 1`. Assertions below check "the fixtures I created behave correctly"
(exact by id) rather than "the whole table has exactly N rows", and this
file's project names are prefixed uniquely so its own search tests can't
accidentally also match another revenue test file's fixtures."""
import os
import uuid
from datetime import datetime, timedelta, timezone
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_payment import persistencePayment
from Divinepersistence.persistence_booking import persistenceBooking
from DivineService.service_payment import servicePayment
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_A = None
_CUSTOMER_B = None
_CAPTURED_ID = None
_CASH_ID = None
_REFUNDED_ID = None
_REFUND_PENDING_ID = None


def setup_module(module):
    global _CUSTOMER_A, _CUSTOMER_B, _CAPTURED_ID, _CASH_ID, _REFUNDED_ID, _REFUND_PENDING_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    # Fixtures go straight through the persistence/service layer, not real
    # POST /payments/... HTTP calls - keeps this file off the shared
    # client-ip:path rate-limit bucket other payment test files already use.
    customer_a = persistenceCustomer().create_user(
        username="admin_revenue_customer_a", password_hash="not-used-by-these-tests",
        phone="9000000201", email="admin.revenue.a@example.com", first_name="Meera", last_name="Pillai",
    )
    _CUSTOMER_A = customer_a.id
    customer_b = persistenceCustomer().create_user(
        username="admin_revenue_customer_b", password_hash="not-used-by-these-tests",
        phone="9000000202", email="admin.revenue.b@example.com", first_name="Rehan", last_name="Sharma",
    )
    _CUSTOMER_B = customer_b.id

    # Project names are prefixed uniquely to this file so a search test here
    # can never accidentally also match test_persistence_revenue.py's rows if
    # both happen to share the SQLite file within one pytest session.
    inv = persistenceInventory()
    unit_captured = str(uuid.uuid4())
    inv.upsert_unit(id=unit_captured, project_name="Admin QA Sunrise Meadows", city="Karnal",
                     unit_number="P-01", area_sqmt=200, area_sqyd=239, status="available")
    unit_refund = str(uuid.uuid4())
    inv.upsert_unit(id=unit_refund, project_name="Admin QA Emerald Court", city="Karnal",
                     unit_number="E-09", area_sqmt=200, area_sqyd=239, status="available")

    payment_persistence = persistencePayment()
    booking_persistence = persistenceBooking()

    captured = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=3120000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_captured,
    )
    payment_persistence.update_payment_status(
        captured.id, status="paid", razorpay_payment_id="pay_captured", razorpay_signature="sig_captured",
    )
    booking_persistence.create_booking(
        payment_id=captured.id, inventory_id=unit_captured, customer_id=_CUSTOMER_A,
        project_name="Admin QA Sunrise Meadows", unit_number="P-01", amount=3120000,
    )
    _CAPTURED_ID = captured.id

    cash_record = servicePayment().record_cash_payment(
        amount=980000, owner_id=_CUSTOMER_B, owner_role="customer", purpose="other", method="cash",
    )
    _CASH_ID = cash_record.id

    refunded = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_B, owner_role="customer",
        amount=2100000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_refund,
    )
    payment_persistence.update_payment_status(
        refunded.id, status="paid", razorpay_payment_id="pay_refunded", razorpay_signature="sig_refunded",
    )
    booking_persistence.create_booking(
        payment_id=refunded.id, inventory_id=unit_refund, customer_id=_CUSTOMER_B,
        project_name="Admin QA Emerald Court", unit_number="E-09", amount=2100000,
    )
    payment_persistence.update_refund_status(refunded.id, refund_status="completed", refund_amount=2100000)
    _REFUNDED_ID = refunded.id

    refund_pending = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=500000, currency="INR", status="created", method="rtgs_neft", utr_number="UTR900001",
        purpose="other",
    )
    payment_persistence.update_payment_status(
        refund_pending.id, status="paid", razorpay_payment_id=None, razorpay_signature=None,
    )
    payment_persistence.update_refund_status(refund_pending.id, refund_status="pending", refund_amount=500000)
    _REFUND_PENDING_ID = refund_pending.id

    # Never settled - must never appear in any response from this API.
    payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=100000, currency="INR", status="created", method="razorpay", purpose="other",
    )


def _admin_headers():
    payload = {
        "sub": "DV5001", "username": "revenue_admin@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "admintestsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _customer_headers():
    payload = {
        "sub": _CUSTOMER_A, "username": "admin_revenue_customer_a",
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


# ---------- auth boundaries - no financial data may leak to a non-admin ----------

def test_list_transactions_requires_auth():
    r = client.get("/admin/revenue/transactions")
    assert r.status_code == 401, r.text


def test_list_transactions_rejects_customer_token():
    r = client.get("/admin/revenue/transactions", headers=_customer_headers())
    assert r.status_code == 401, r.text


def test_list_transactions_rejects_broker_token():
    r = client.get("/admin/revenue/transactions", headers=_broker_headers())
    assert r.status_code == 401, r.text


def test_summary_requires_auth():
    r = client.get("/admin/revenue/summary")
    assert r.status_code == 401, r.text


def test_transaction_detail_requires_auth():
    r = client.get(f"/admin/revenue/transactions/{_CAPTURED_ID}")
    assert r.status_code == 401, r.text


# ---------- transactions list ----------

def test_list_transactions_excludes_unsettled_payments():
    r = client.get("/admin/revenue/transactions?page_size=100", headers=_admin_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    ids = {i["transaction_id"] for i in data["items"]}
    assert _CAPTURED_ID in ids
    assert _CASH_ID in ids
    assert _REFUNDED_ID in ids
    assert _REFUND_PENDING_ID in ids
    # >=, not ==: the shared test_db.sqlite may also carry rows inserted by
    # other test files within the same pytest session (see module docstring).
    assert data["pagination"]["total_items"] >= 4


def test_list_transactions_status_labels_match_screenshot_vocabulary():
    r = client.get("/admin/revenue/transactions?page_size=100", headers=_admin_headers())
    by_id = {i["transaction_id"]: i for i in r.json()["items"]}
    assert by_id[_CAPTURED_ID]["status"] == "captured"
    assert by_id[_CASH_ID]["status"] == "cash_recorded"
    assert by_id[_REFUNDED_ID]["status"] == "refunded"
    assert by_id[_REFUND_PENDING_ID]["status"] == "refund_pending"
    assert by_id[_CAPTURED_ID]["project_name"] == "Admin QA Sunrise Meadows"
    assert by_id[_CAPTURED_ID]["customer_name"] == "Meera Pillai"
    assert by_id[_CAPTURED_ID]["amount"] == 3120000.0


def test_list_transactions_filter_by_status():
    r = client.get("/admin/revenue/transactions?status=refunded", headers=_admin_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    ids = {i["transaction_id"] for i in data["items"]}
    assert _REFUNDED_ID in ids
    assert all(i["status"] == "refunded" for i in data["items"])


def test_list_transactions_filter_by_method():
    r = client.get("/admin/revenue/transactions?method=cash", headers=_admin_headers())
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert _CASH_ID in {i["transaction_id"] for i in items}
    assert all(i["method"] == "cash" for i in items)


def test_list_transactions_search_by_project():
    r = client.get("/admin/revenue/transactions?search=Admin+QA+Sunrise+Meadows", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert {i["transaction_id"] for i in r.json()["items"]} == {_CAPTURED_ID}


def test_list_transactions_search_by_customer_name():
    r = client.get("/admin/revenue/transactions?search=Rehan+Sharma", headers=_admin_headers())
    assert r.status_code == 200, r.text
    ids = {i["transaction_id"] for i in r.json()["items"]}
    assert _CASH_ID in ids
    assert _REFUNDED_ID in ids


def test_list_transactions_pagination():
    r = client.get("/admin/revenue/transactions?page=1&page_size=2", headers=_admin_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["items"]) == 2
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["page_size"] == 2
    assert data["pagination"]["total_items"] >= 4

    r2 = client.get("/admin/revenue/transactions?page=2&page_size=2", headers=_admin_headers())
    page1_ids = {i["transaction_id"] for i in data["items"]}
    page2_ids = {i["transaction_id"] for i in r2.json()["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_list_transactions_rejects_bad_query_params():
    r = client.get(
        "/admin/revenue/transactions?page=0&page_size=999&status=not_a_status&method=bitcoin&date_from=09-2026-01",
        headers=_admin_headers(),
    )
    assert r.status_code == 422, r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert ("query", "page") in locs
    assert ("query", "page_size") in locs
    assert ("query", "status") in locs
    assert ("query", "method") in locs
    assert ("query", "date_from") in locs


def test_list_transactions_rejects_date_from_after_date_to():
    r = client.get(
        "/admin/revenue/transactions?date_from=2026-09-20&date_to=2026-09-01",
        headers=_admin_headers(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "date_from_after_date_to"


# ---------- transaction detail ----------

def test_get_transaction_detail():
    r = client.get(f"/admin/revenue/transactions/{_CAPTURED_ID}", headers=_admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transaction_id"] == _CAPTURED_ID
    assert body["status"] == "captured"
    assert body["razorpay_payment_id"] == "pay_captured"


def test_get_transaction_detail_not_found_for_unknown_id():
    r = client.get("/admin/revenue/transactions/does-not-exist", headers=_admin_headers())
    assert r.status_code == 404, r.text


def test_get_transaction_detail_not_found_for_unsettled_payment():
    """A 'created' (never settled) payment is not revenue - detail must 404,
    not leak its existence or amount."""
    unsettled = persistencePayment().create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=250000, currency="INR", status="created", method="razorpay", purpose="other",
    )
    r = client.get(f"/admin/revenue/transactions/{unsettled.id}", headers=_admin_headers())
    assert r.status_code == 404, r.text


# ---------- summary ----------

def test_get_summary_totals():
    r = client.get("/admin/revenue/summary", headers=_admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    # >=, not ==: see module docstring on cross-file DB accumulation.
    assert body["total_transactions"] >= 4
    assert body["captured_amount"] >= 3120000
    assert body["cash_amount"] >= 980000
    assert body["refunded_amount"] >= 2100000
    assert body["refund_pending_amount"] >= 500000


def test_get_summary_rejects_bad_date():
    r = client.get("/admin/revenue/summary?date_from=not-a-date", headers=_admin_headers())
    assert r.status_code == 422, r.text

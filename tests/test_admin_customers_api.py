import os
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app
from tests.admin_signup_helpers import mint_admin_access_token, signup_and_verify_admin

client = TestClient(app)

_ADMIN_TOKEN = None

# NOTE ON CALL BUDGET: the app's RateLimitMiddleware buckets by client+PATH ONLY (not
# method), so every GET and POST to /admin/customers in this whole file shares one
# 10-calls/60s bucket. Tests below are deliberately consolidated (several assertions
# per request, via query/body payloads that violate multiple constraints at once) to
# stay well under that shared limit - see tests/test_installment_payments.py for the
# same constraint documented against a different endpoint.


def setup_module(module):
    global _ADMIN_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    admin_email = "customers_admin@divinevisioninfra.com"
    signup = signup_and_verify_admin(client, {
        "full_name": "Customers Admin", "employee_id": "DV7777",
        "email": admin_email, "password": "strongpassword",
    })
    _ADMIN_TOKEN = mint_admin_access_token(signup.json()["id"], admin_email)


def _auth_headers():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_list_customers_requires_auth():
    r = client.get("/admin/customers")
    assert r.status_code == 401, r.text


def test_list_customers_empty_result_shape():
    # A search term guaranteed to match nothing - this test's own module-level
    # setup_module resets test_db.sqlite, but on Windows a still-open SQLite
    # connection from an earlier test file can make that os.remove() silently
    # no-op (caught by its own try/except), leaving other files' rows visible
    # here. Searching for a value nothing could ever match keeps this
    # assertion valid regardless of what ran before it.
    r = client.get("/admin/customers?search=no-such-customer-xyz-should-never-match", headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["items"] == []
    assert data["pagination"] == {"page": 1, "page_size": 20, "total_items": 0, "total_pages": 0}


def test_create_customer_creates_a_real_active_customer_account():
    payload = {"full_name": "Arjun Mehta", "email": "arjun.mehta@example.com", "phone": "9876543210"}
    r = client.post("/admin/customers", json=payload, headers=_auth_headers())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["source"] == "WEBSITE"
    assert data["status"] == "ACTIVE"
    assert data["full_name"] == "Arjun Mehta"
    assert data["email"] == "arjun.mehta@example.com"
    assert data["id"]

    # Confirm it's a real divine_customer_users row (checked directly, not via
    # another rate-limited HTTP endpoint) - not a placeholder/lead record.
    from Divinepersistence import persistenceCustomer
    stored = persistenceCustomer().get_by_email("arjun.mehta@example.com")
    assert stored is not None
    assert stored.id == data["id"]
    assert stored.username == "arjun.mehta@example.com"
    assert stored.password_hash and stored.password_hash != ""


def test_create_customer_requires_auth():
    payload = {"full_name": "No Auth", "email": "no_auth@example.com", "phone": "123"}
    r = client.post("/admin/customers", json=payload)
    assert r.status_code == 401, r.text


def test_create_customer_rejects_duplicate_email():
    payload = {"full_name": "Dup Person", "email": "arjun.mehta@example.com", "phone": "111"}
    r = client.post("/admin/customers", json=payload, headers=_auth_headers())
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "email_already_exists"


def test_create_customer_rejects_multiple_invalid_fields_at_once():
    # Blank full_name, invalid email, blank phone - all three violated in one request,
    # since Pydantic reports every field error in a single 422 response.
    payload = {"full_name": "  ", "email": "not-an-email", "phone": "   "}
    r = client.post("/admin/customers", json=payload, headers=_auth_headers())
    assert r.status_code == 422, r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert ("body", "full_name") in locs
    assert ("body", "email") in locs
    assert ("body", "phone") in locs


def test_list_customers_search_and_filters_together():
    r = client.get(
        "/admin/customers?search=arjun&status=ACTIVE&source=WEBSITE", headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pagination"]["total_items"] >= 1
    assert any(item["email"] == "arjun.mehta@example.com" for item in data["items"])
    for item in data["items"]:
        assert item["status"] == "ACTIVE"
        assert item["source"] == "WEBSITE"


def test_list_customers_rejects_multiple_bad_query_params_at_once():
    # page too low, page_size too high, and three bad enum/literal values - all five
    # violated in one request; FastAPI/Pydantic reports every one in the same 422.
    r = client.get(
        "/admin/customers?page=0&page_size=999&status=NOT_A_STATUS&source=NOT_A_SOURCE&sort=not_a_field",
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert ("query", "page") in locs
    assert ("query", "page_size") in locs
    assert ("query", "status") in locs
    assert ("query", "source") in locs
    assert ("query", "sort") in locs


def test_list_customers_rejects_broker_token():
    broker_signup = client.post("/broker/signup", json={
        "username": "cust_admin_test_broker", "password": "strongpassword",
        "phone": "9990001111", "project": "suraksha-enclave",
    })
    assert broker_signup.status_code == 200, broker_signup.text
    broker_login = client.post("/broker/login", json={"username": "cust_admin_test_broker", "password": "strongpassword"})
    assert broker_login.status_code == 200, broker_login.text
    broker_token = broker_login.json()["access_token"]

    r = client.get("/admin/customers", headers={"Authorization": f"Bearer {broker_token}"})
    assert r.status_code == 401, r.text

import os
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
import Divinepersistence.persistence_chatbot  # noqa: F401 - registers ChatbotLeadModel on Base.metadata
from DivineAPI.main import app

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

    client.post("/admin/signup", json={
        "full_name": "Customers Admin", "employee_id": "DV7777",
        "email": "customers_admin@example.com", "password": "strongpassword",
    })
    login = client.post("/admin/login", json={"email": "customers_admin@example.com", "password": "strongpassword"})
    assert login.status_code == 200, login.text
    _ADMIN_TOKEN = login.json()["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_list_customers_requires_auth():
    r = client.get("/admin/customers")
    assert r.status_code == 401, r.text


def test_list_customers_empty_result_shape():
    r = client.get("/admin/customers", headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["items"] == []
    assert data["pagination"] == {"page": 1, "page_size": 20, "total_items": 0, "total_pages": 0}


def test_create_customer_defaults_to_website_lead():
    payload = {"full_name": "Arjun Mehta", "email": "arjun.mehta@example.com", "phone": "9876543210"}
    r = client.post("/admin/customers", json=payload, headers=_auth_headers())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["source"] == "WEBSITE"
    assert data["status"] == "LEAD"
    assert data["full_name"] == "Arjun Mehta"
    assert data["email"] == "arjun.mehta@example.com"
    assert data["id"]


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
        "/admin/customers?search=arjun&status=LEAD&source=WEBSITE", headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pagination"]["total_items"] >= 1
    assert any(item["email"] == "arjun.mehta@example.com" for item in data["items"])
    for item in data["items"]:
        assert item["status"] == "LEAD"
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

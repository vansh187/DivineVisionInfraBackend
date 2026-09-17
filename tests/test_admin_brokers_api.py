import os
from datetime import datetime, timedelta, timezone
import jwt
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
# method), shared across the WHOLE test session (not just this file) - /broker/signup
# and /customer/signup in particular are already near their 10-calls/60s budget from
# every other test file that needs a broker/customer identity, so this file adds at
# most ONE more call to either and mints JWTs by hand (matching the pattern in
# test_broker_commissions.py) instead of a real /customer/signup+login round trip
# wherever a non-admin token is only needed to prove it's rejected.


def _customer_token():
    payload = {
        "sub": "C99999", "username": "not-a-real-customer",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, "testsecret", algorithm="HS256")


def setup_module(module):
    global _ADMIN_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    admin_email = "brokers_admin@divinevisioninfra.com"
    signup = signup_and_verify_admin(client, {
        "full_name": "Brokers Admin", "employee_id": "DV8888",
        "email": admin_email, "password": "strongpassword",
    })
    _ADMIN_TOKEN = mint_admin_access_token(signup.json()["id"], admin_email)

    client.post("/broker/signup", json={
        "username": "admin_list_broker_1", "password": "strongpassword",
        "phone": "9998887771", "project": "suraksha-enclave",
        "first_name": "Ravi", "last_name": "Kumar", "email": "ravi.kumar@example.com",
    })


def _auth_headers():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_list_brokers_requires_auth():
    r = client.get("/admin/brokers")
    assert r.status_code == 401, r.text


def test_list_brokers_returns_registered_brokers():
    r = client.get("/admin/brokers", headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pagination"]["total_items"] >= 1
    assert any(item["email"] == "ravi.kumar@example.com" for item in data["items"])
    for item in data["items"]:
        if item["project"] is not None:
            assert item["project"] in ("suraksha-enclave", "ops-divine-greens")


def test_list_brokers_search_and_project_filter_together():
    r = client.get(
        "/admin/brokers?search=ravi&project=suraksha-enclave", headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert any(item["email"] == "ravi.kumar@example.com" for item in data["items"])
    for item in data["items"]:
        assert item["project"] == "suraksha-enclave"


def test_list_brokers_rejects_multiple_bad_query_params_at_once():
    r = client.get(
        "/admin/brokers?page=0&page_size=999&project=not-a-real-project&sort=not_a_field",
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert ("query", "page") in locs
    assert ("query", "page_size") in locs
    assert ("query", "project") in locs
    assert ("query", "sort") in locs


def test_list_brokers_rejects_customer_token():
    r = client.get("/admin/brokers", headers={"Authorization": f"Bearer {_customer_token()}"})
    assert r.status_code == 401, r.text

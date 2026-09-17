import os
from datetime import date, datetime, timedelta, timezone
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_visit import persistenceVisit
from Divinepersistence.persistence_broker import persistenceBroker
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app
from tests.admin_signup_helpers import mint_admin_access_token, signup_and_verify_admin

client = TestClient(app)

_ADMIN_TOKEN = None


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

    admin_email = "visits_admin@divinevisioninfra.com"
    signup = signup_and_verify_admin(client, {
        "full_name": "Visits Admin", "employee_id": "DV8899",
        "email": admin_email, "password": "strongpassword",
    })
    _ADMIN_TOKEN = mint_admin_access_token(signup.json()["id"], admin_email)

    # Broker and visit are inserted straight through the persistence layer
    # rather than POST /broker/signup + POST /visits: both endpoints share a
    # rate-limit bucket (client+path) with test_visits.py / test_admin_brokers_api.py,
    # which are already tuned to stay at/near their own 10-calls/60s budget -
    # extra calls here (all files run in the same pytest session) would push
    # them over and turn those tests' 200s into 429s.
    broker = persistenceBroker().create_user(
        username="admin_list_visit_broker", password_hash="not-used-by-these-tests",
        phone="9998887772", project="suraksha-enclave",
        first_name="Nisha", last_name="Verma", email="nisha.verma@example.com",
    )
    broker_id = broker.id
    persistenceVisit().create_visit(
        id="admin-visits-test-visit-1",
        broker_id=broker_id,
        customer_name="Admin Visits Fixture Customer",
        customer_contact="9999999998",
        visit_date=date(2026, 9, 12),
        visit_time="10:00",
        notes="Green Meadows walkthrough",
        status="scheduled",
    )


def _auth_headers():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_list_visits_requires_auth():
    r = client.get("/admin/visits")
    assert r.status_code == 401, r.text


def test_list_visits_rejects_customer_token():
    r = client.get("/admin/visits", headers={"Authorization": f"Bearer {_customer_token()}"})
    assert r.status_code == 401, r.text


def test_list_visits_returns_scheduled_visit():
    r = client.get("/admin/visits", headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pagination"]["total_items"] >= 1
    item = next(i for i in data["items"] if i["customer_name"] == "Admin Visits Fixture Customer")
    assert item["origin_type"] == "CHANNEL_PARTNER"
    assert item["source"] == "Nisha Verma"
    assert item["status"] == "scheduled"
    assert item["visit_date"] == "2026-09-12"
    assert item["visit_time"] == "10:00"


def test_list_visits_search_and_status_filter_together():
    r = client.get(
        "/admin/visits?search=Admin+Visits+Fixture&status=scheduled&origin_type=CHANNEL_PARTNER",
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert any(i["customer_name"] == "Admin Visits Fixture Customer" for i in data["items"])
    for item in data["items"]:
        assert item["status"] == "scheduled"
        assert item["origin_type"] == "CHANNEL_PARTNER"


def test_list_visits_rejects_bad_query_params():
    r = client.get(
        "/admin/visits?page=0&page_size=999&status=not_a_status&sort=not_a_field",
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert ("query", "page") in locs
    assert ("query", "page_size") in locs
    assert ("query", "status") in locs
    assert ("query", "sort") in locs


def test_get_visit_detail():
    listed = client.get("/admin/visits", headers=_auth_headers()).json()["items"]
    visit_id = next(i for i in listed if i["customer_name"] == "Admin Visits Fixture Customer")["id"]

    r = client.get(f"/admin/visits/{visit_id}", headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["id"] == visit_id


def test_get_visit_detail_not_found():
    r = client.get("/admin/visits/does-not-exist", headers=_auth_headers())
    assert r.status_code == 404, r.text


def test_get_visit_detail_requires_auth():
    r = client.get("/admin/visits/does-not-exist")
    assert r.status_code == 401, r.text

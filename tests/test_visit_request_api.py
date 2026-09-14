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

client = TestClient(app)


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()


def _auth_headers():
    # Minted by hand rather than a real POST /admin/signup + /admin/login round
    # trip: that shares a rate-limit bucket (client+path) with test_admin_brokers_api.py
    # / test_admin_customers_api.py / test_admin_visits_api.py, all of which are
    # already at/near their own 10-calls/60s budget in the same pytest session -
    # see the identical note in test_admin_visits_api.py.
    payload = {
        "sub": "DV7777", "username": "request_callback_admin@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "admintestsecret", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _sample_payload(**overrides):
    payload = {
        "customer_name": "Rehan Sharma",
        "customer_contact": "9999999998",
        "customer_email": "rehan@example.com",
        "project": "suraksha-enclave",
        "preferred_window": "weekend",
    }
    payload.update(overrides)
    return payload


def test_request_callback_happy_path_no_auth_required():
    r = client.post("/visits/request", json=_sample_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["broker_id"] is None
    assert data["project"] == "suraksha-enclave"
    assert data["date"] is None
    assert data["time"] is None
    assert data["status"] == "requested"
    assert data["source"] == "website"
    assert data["customer_name"] == "Rehan Sharma"


def test_request_callback_rejects_unknown_project():
    r = client.post("/visits/request", json=_sample_payload(project="not-a-real-project"))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_project"


def test_request_callback_rejects_blank_contact():
    r = client.post("/visits/request", json=_sample_payload(customer_contact="   "))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_contact"


def test_request_callback_rejects_missing_required_field():
    payload = _sample_payload()
    del payload["customer_name"]
    r = client.post("/visits/request", json=payload)
    assert r.status_code == 422, r.text


def test_request_callback_rejects_bad_preferred_window():
    r = client.post("/visits/request", json=_sample_payload(preferred_window="this_weekend"))
    assert r.status_code == 422, r.text


def test_request_callback_email_is_optional():
    payload = _sample_payload(customer_contact="9999999997")
    del payload["customer_email"]
    r = client.post("/visits/request", json=payload)
    assert r.status_code == 201, r.text


def test_requested_visit_is_visible_in_admin_list_without_crashing():
    r = client.post("/visits/request", json=_sample_payload(customer_contact="9999999996"))
    assert r.status_code == 201, r.text

    listed = client.get(
        "/admin/visits", params={"origin_type": "CUSTOMER", "status": "requested"}, headers=_auth_headers(),
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert any(i["customer_contact"] == "9999999996" for i in data["items"])
    for item in data["items"]:
        assert item["status"] == "requested"
        assert item["preferred_window"] in ("today", "tomorrow", "weekend")

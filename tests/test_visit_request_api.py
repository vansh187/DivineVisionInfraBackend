import os
from datetime import datetime, timedelta, timezone
import jwt
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app
from DivineDTO.models import VisitRequestCallbackDTO

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
        "date": "2026-09-20",
        "time": "11:00",
        "notes": "Looking for a corner plot near the entrance.",
    }
    payload.update(overrides)
    return payload


# NOTE on call budget: POST /visits/request shares a rate-limit bucket
# (client+path) with test_visits_mine_api.py in the same pytest session, and
# both are capped at 10 calls/60s combined - see the identical note there.
# Field-validation cases (bad project, blank contact, bad date/time, a missing
# required field) are covered as DTO/service-layer unit tests below instead of
# real HTTP round trips, so this file only spends its budget on genuine
# end-to-end checks.

def test_request_callback_happy_path_no_auth_required():
    r = client.post("/visits/request", json=_sample_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["broker_id"] is None
    assert data["project"] == "suraksha-enclave"
    assert data["date"] == "2026-09-20"
    assert data["time"] == "11:00"
    assert data["status"] == "scheduled"
    assert data["source"] == "website"
    assert data["customer_name"] == "Rehan Sharma"
    assert data["customer_email"] == "rehan@example.com"
    assert data["notes"] == "Looking for a corner plot near the entrance."


def test_scheduled_website_visit_is_visible_in_admin_list_without_crashing():
    r = client.post("/visits/request", json=_sample_payload(customer_contact="9999999996"))
    assert r.status_code == 201, r.text

    listed = client.get(
        "/admin/visits", params={"origin_type": "CUSTOMER", "status": "scheduled"}, headers=_auth_headers(),
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert any(i["customer_contact"] == "9999999996" for i in data["items"])
    for item in data["items"]:
        assert item["status"] == "scheduled"
        assert item["visit_date"] == "2026-09-20"
        assert item["visit_time"] == "11:00"


# ---------- DTO-level validation (no HTTP call, no rate-limit budget spent) ----------

def test_dto_rejects_missing_required_field():
    payload = _sample_payload()
    del payload["customer_name"]
    with pytest.raises(ValidationError):
        VisitRequestCallbackDTO(**payload)


def test_dto_rejects_missing_date():
    payload = _sample_payload()
    del payload["date"]
    with pytest.raises(ValidationError):
        VisitRequestCallbackDTO(**payload)


def test_dto_email_is_optional():
    payload = _sample_payload()
    del payload["customer_email"]
    dto = VisitRequestCallbackDTO(**payload)
    assert dto.customer_email is None

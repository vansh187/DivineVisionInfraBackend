import os
import uuid
from datetime import date, datetime, timedelta, timezone
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_visit import persistenceVisit
from Divinepersistence.persistence_broker import persistenceBroker
from Divinepersistence.persistence_customer import persistenceCustomer
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_WITH_VISITS_ID = None
_CUSTOMER_NO_EMAIL_ID = None
_CUSTOMER_ID_ONLY_ID = None
_CUSTOMER_FOR_LIVE_REQUEST_ID = None


def setup_module(module):
    global _CUSTOMER_WITH_VISITS_ID, _CUSTOMER_NO_EMAIL_ID, _CUSTOMER_ID_ONLY_ID, _CUSTOMER_FOR_LIVE_REQUEST_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    # Everything below is inserted straight through the persistence layer
    # rather than the real signup/login/POST endpoints: /customer/signup,
    # /broker/signup and /visits/request each share a rate-limit bucket
    # (client+path) with other test files already at/near their own
    # 10-calls/60s budget in the same pytest session - see the identical note
    # in test_admin_visits_api.py / test_visit_request_api.py. Only GET
    # /visits/mine itself (its own, otherwise-unused bucket) goes through the
    # real HTTP client.
    customer = persistenceCustomer().create_user(
        username="visits_mine_customer", password_hash="not-used-by-these-tests",
        phone="9000000010", email="mine.customer@example.com",
        first_name="Rehan", last_name="Sharma",
    )
    _CUSTOMER_WITH_VISITS_ID = customer.id

    no_email_customer = persistenceCustomer().create_user(
        username="visits_mine_no_email_customer", password_hash="not-used-by-these-tests",
        phone="9000000012", email=None, first_name="No", last_name="Email",
    )
    _CUSTOMER_NO_EMAIL_ID = no_email_customer.id

    id_only_customer = persistenceCustomer().create_user(
        username="visits_mine_id_only_customer", password_hash="not-used-by-these-tests",
        phone="9000000013", email=None, first_name="Id", last_name="Only",
    )
    _CUSTOMER_ID_ONLY_ID = id_only_customer.id

    live_request_customer = persistenceCustomer().create_user(
        username="visits_mine_live_request_customer", password_hash="not-used-by-these-tests",
        phone="9000000014", email="live.request@example.com", first_name="Live", last_name="Request",
    )
    _CUSTOMER_FOR_LIVE_REQUEST_ID = live_request_customer.id

    broker = persistenceBroker().create_user(
        username="visits_mine_broker", password_hash="not-used-by-these-tests",
        phone="9000000011", project="ops-divine-greens", first_name="Broker", last_name="One",
    )

    # Self-requested (website) visit for the customer - the website flow now
    # collects an exact date/time and creates it 'scheduled' directly, same as
    # the broker flow (see serviceVisit.request_callback).
    persistenceVisit().create_visit_request(
        id=str(uuid.uuid4()),
        customer_name="Rehan Sharma", customer_contact="9999999998",
        customer_email="mine.customer@example.com",
        visit_date=date(2026, 9, 19), visit_time="15:00", notes="corner plot",
        project_name="suraksha-enclave", status="scheduled",
    )

    # Broker-scheduled visit for the same customer (matched by email).
    persistenceVisit().create_visit(
        id=str(uuid.uuid4()), broker_id=broker.id,
        customer_name="Rehan Sharma", customer_contact="9999999998",
        customer_email="mine.customer@example.com",
        visit_date=date(2026, 9, 20), visit_time="11:00", notes="corner plot near entrance",
        status="scheduled", project_name="ops-divine-greens",
    )

    # A visit for someone else entirely - must never show up in the customer's list.
    persistenceVisit().create_visit_request(
        id=str(uuid.uuid4()),
        customer_name="Someone Else", customer_contact="8888888888",
        customer_email="someone.else@example.com",
        visit_date=date(2026, 9, 21), visit_time="10:00", notes=None,
        project_name="suraksha-enclave", status="scheduled",
    )

    # Tagged with customer_id directly and no customer_email at all - must
    # still be found by list_mine even though this account has no email on
    # file (the customer_id match doesn't depend on email).
    persistenceVisit().create_visit_request(
        id=str(uuid.uuid4()),
        customer_name="Id Only", customer_contact="7777777777",
        customer_email=None, customer_id=_CUSTOMER_ID_ONLY_ID,
        visit_date=date(2026, 9, 22), visit_time="09:00", notes=None,
        project_name="ops-divine-greens", status="scheduled",
    )


def _customer_token(customer_id: str):
    payload = {
        "sub": customer_id, "username": "visits_mine_customer",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, "testsecret", algorithm="HS256")


def _broker_token():
    payload = {
        "sub": "B00099", "username": "visits_mine_broker",
        "role": "broker", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, "testsecret", algorithm="HS256")


def test_list_mine_requires_auth():
    r = client.get("/visits/mine")
    assert r.status_code == 401, r.text


def test_list_mine_rejects_broker_token():
    r = client.get("/visits/mine", headers={"Authorization": f"Bearer {_broker_token()}"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "visits_customer_only"


def test_list_mine_returns_both_website_and_broker_visits():
    r = client.get(
        "/visits/mine", headers={"Authorization": f"Bearer {_customer_token(_CUSTOMER_WITH_VISITS_ID)}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 2

    website = next(v for v in data if v["source"] == "website")
    assert website["broker_id"] is None
    assert website["date"] == "2026-09-19"
    assert website["time"] == "15:00"
    assert website["status"] == "scheduled"
    assert website["project"] == "suraksha-enclave"

    broker_scheduled = next(v for v in data if v["source"] == "broker")
    assert broker_scheduled["broker_id"] is not None
    assert broker_scheduled["date"] == "2026-09-20"
    assert broker_scheduled["time"] == "11:00"
    assert broker_scheduled["status"] == "scheduled"
    assert broker_scheduled["project"] == "ops-divine-greens"

    names = {v["customer_name"] for v in data}
    assert names == {"Rehan Sharma"}


def test_list_mine_excludes_other_customers_visits():
    r = client.get(
        "/visits/mine", headers={"Authorization": f"Bearer {_customer_token(_CUSTOMER_WITH_VISITS_ID)}"},
    )
    names = [v["customer_name"] for v in r.json()]
    assert "Someone Else" not in names


def test_list_mine_returns_empty_list_when_no_email_on_file_and_no_tagged_visit():
    r = client.get(
        "/visits/mine", headers={"Authorization": f"Bearer {_customer_token(_CUSTOMER_NO_EMAIL_ID)}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_mine_returns_empty_list_for_unknown_customer_id():
    r = client.get(
        "/visits/mine", headers={"Authorization": f"Bearer {_customer_token('C99999')}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_mine_matches_by_customer_id_even_without_an_email_on_file():
    r = client.get(
        "/visits/mine", headers={"Authorization": f"Bearer {_customer_token(_CUSTOMER_ID_ONLY_ID)}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 1
    assert data[0]["customer_name"] == "Id Only"
    assert data[0]["project"] == "ops-divine-greens"


def test_authenticated_request_callback_is_tagged_with_customer_id_and_found_by_mine():
    token = _customer_token(_CUSTOMER_FOR_LIVE_REQUEST_ID)
    r = client.post(
        "/visits/request",
        json={
            "customer_name": "Live Request",
            "customer_contact": "6666666666",
            "project": "suraksha-enclave",
            "date": "2026-09-23",
            "time": "14:00",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["broker_id"] is None
    assert r.json()["status"] == "scheduled"

    mine = client.get("/visits/mine", headers={"Authorization": f"Bearer {token}"})
    assert mine.status_code == 200, mine.text
    data = mine.json()
    assert len(data) == 1
    assert data[0]["customer_name"] == "Live Request"


def test_anonymous_request_callback_still_succeeds_without_a_token():
    r = client.post(
        "/visits/request",
        json={
            "customer_name": "Anonymous Caller",
            "customer_contact": "5555555555",
            "project": "suraksha-enclave",
            "date": "2026-09-24",
            "time": "16:00",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["broker_id"] is None
    assert r.json()["status"] == "scheduled"

import os
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app

client = TestClient(app)

_BROKER_TOKEN = None
_OTHER_BROKER_TOKEN = None
_CUSTOMER_TOKEN = None


def setup_module(module):
    global _BROKER_TOKEN, _OTHER_BROKER_TOKEN, _CUSTOMER_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    client.post("/broker/signup", json={"username": "visitbroker_shared", "password": "strongpassword"})
    lr = client.post("/broker/login", json={"username": "visitbroker_shared", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _BROKER_TOKEN = lr.json()["access_token"]

    client.post("/broker/signup", json={"username": "visitbroker_other", "password": "strongpassword"})
    lr2 = client.post("/broker/login", json={"username": "visitbroker_other", "password": "strongpassword"})
    assert lr2.status_code == 200, lr2.text
    _OTHER_BROKER_TOKEN = lr2.json()["access_token"]

    client.post("/customer/signup", json={"username": "visitcust_shared", "password": "strongpassword"})
    lr3 = client.post("/customer/login", json={"username": "visitcust_shared", "password": "strongpassword"})
    assert lr3.status_code == 200, lr3.text
    _CUSTOMER_TOKEN = lr3.json()["access_token"]


def _auth_headers(token=None):
    return {"Authorization": f"Bearer {token or _BROKER_TOKEN}"}


def _sample_payload(**overrides):
    payload = {
        "customer_name": "Jane Doe",
        "customer_contact": "9999999999",
        "date": "2026-09-15",
        "time": "14:30",
        "notes": "Interested in OPS Divine Greens, 250 sq yd",
    }
    payload.update(overrides)
    return payload


# NOTE on call budget: the rate limiter's bucket key is client+path only (not method),
# so GET /visits and POST /visits share one bucket - this file is written to stay at or
# under 10 total combined GET+POST calls to exactly "/visits" across the whole file by
# reusing visits created in earlier tests instead of creating a fresh one per test.
# DELETE /visits/{id} uses a per-id path, so it doesn't share that bucket.

def test_schedule_visit_requires_auth():
    r = client.post("/visits", json=_sample_payload())  # /visits call 1
    assert r.status_code == 401


def test_schedule_visit_rejects_customer_caller():
    r = client.post("/visits", json=_sample_payload(), headers=_auth_headers(_CUSTOMER_TOKEN))  # /visits call 2
    assert r.status_code == 403
    assert r.json()["detail"] == "visits_broker_only"


def test_schedule_visit_rejects_invalid_date():
    r = client.post("/visits", json=_sample_payload(date="15-09-2026"), headers=_auth_headers())  # /visits call 3
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_date"


_shared_visit_id = None


def test_schedule_visit_happy_path():
    global _shared_visit_id
    r = client.post("/visits", json=_sample_payload(), headers=_auth_headers())  # /visits call 4
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["customer_name"] == "Jane Doe"
    assert data["customer_contact"] == "9999999999"
    assert data["date"] == "2026-09-15"
    assert data["time"] == "14:30"
    assert data["status"] == "scheduled"
    assert data["broker_id"]
    _shared_visit_id = data["id"]


def test_list_visits_requires_auth():
    r = client.get("/visits")  # /visits call 5
    assert r.status_code == 401


def test_list_visits_rejects_customer_caller():
    r = client.get("/visits", headers=_auth_headers(_CUSTOMER_TOKEN))  # /visits call 6
    assert r.status_code == 403


def test_list_visits_scoped_to_own_broker():
    other_res = client.post(  # /visits call 7
        "/visits", json=_sample_payload(customer_name="Other Broker Customer"), headers=_auth_headers(_OTHER_BROKER_TOKEN)
    )
    assert other_res.status_code == 200, other_res.text

    r = client.get("/visits", headers=_auth_headers())  # /visits call 8
    assert r.status_code == 200, r.text
    names = [v["customer_name"] for v in r.json()]
    assert "Jane Doe" in names  # the shared visit from test_schedule_visit_happy_path
    assert "Other Broker Customer" not in names


def test_cancel_visit_not_found():
    r = client.delete("/visits/does-not-exist", headers=_auth_headers())
    assert r.status_code == 404


def test_cancel_visit_rejects_other_brokers_visit():
    r = client.delete(f"/visits/{_shared_visit_id}", headers=_auth_headers(_OTHER_BROKER_TOKEN))
    assert r.status_code == 403


def test_cancel_visit_happy_path_removes_it_from_list():
    cancel_res = client.delete(f"/visits/{_shared_visit_id}", headers=_auth_headers())
    assert cancel_res.status_code == 200, cancel_res.text
    assert cancel_res.json()["status"] == "cancelled"

    list_res = client.get("/visits", headers=_auth_headers())  # /visits call 9
    ids = [v["id"] for v in list_res.json()]
    assert _shared_visit_id not in ids


# ---------- PATCH /visits/{id} : mark completed (per-id path, own rate-limit bucket) ----------

_complete_visit_id = None


def test_patch_visit_requires_auth():
    r = client.patch("/visits/whatever", json={"status": "completed", "notes": ""})
    assert r.status_code == 401


def test_patch_visit_rejects_customer_caller():
    r = client.patch("/visits/whatever", json={"status": "completed", "notes": ""},
                     headers=_auth_headers(_CUSTOMER_TOKEN))
    assert r.status_code == 403
    assert r.json()["detail"] == "visits_broker_only"


def test_patch_visit_not_found():
    r = client.patch("/visits/does-not-exist", json={"status": "completed", "notes": "x"},
                     headers=_auth_headers())
    assert r.status_code == 404


def test_patch_visit_rejects_malformed_body():
    r = client.patch("/visits/does-not-exist", json={"status": "cancelled"}, headers=_auth_headers())
    assert r.status_code == 422


def _broker_id_from_token(token):
    import jwt
    return jwt.decode(token, os.environ["JWT_SECRET_KEY"], algorithms=["HS256"])["sub"]


def test_patch_visit_rejects_other_brokers_visit():
    # Insert a scheduled visit straight through persistence so this doesn't spend a
    # /visits POST from the shared rate-limit budget.
    global _complete_visit_id
    import uuid
    from datetime import date as _date
    from Divinepersistence import persistenceVisit
    _complete_visit_id = str(uuid.uuid4())
    persistenceVisit().create_visit(
        id=_complete_visit_id, broker_id=_broker_id_from_token(_BROKER_TOKEN),
        customer_name="Ravi Kumar", customer_contact="9998887776",
        visit_date=_date(2026, 10, 1), visit_time="09:00", notes="pre-meeting",
        status="scheduled",
    )

    r = client.patch(f"/visits/{_complete_visit_id}", json={"status": "completed", "notes": "no"},
                     headers=_auth_headers(_OTHER_BROKER_TOKEN))
    assert r.status_code == 403
    assert r.json()["detail"] == "visit_owner_only"


def test_patch_visit_completes_and_overwrites_notes():
    r = client.patch(f"/visits/{_complete_visit_id}",
                     json={"status": "completed", "notes": "Client to confirm by Friday"},
                     headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == _complete_visit_id
    assert data["status"] == "completed"
    assert data["notes"] == "Client to confirm by Friday"


def test_patch_visit_already_completed_returns_409():
    r = client.patch(f"/visits/{_complete_visit_id}", json={"status": "completed", "notes": ""},
                     headers=_auth_headers())
    assert r.status_code == 409
    assert r.json()["detail"] == "visit_not_scheduled"


def test_completed_visit_moves_to_history():
    hist = client.get("/visits/history", headers=_auth_headers())
    assert hist.status_code == 200
    entry = next((v for v in hist.json() if v["id"] == _complete_visit_id), None)
    assert entry is not None and entry["status"] == "completed"


def test_visit_history_requires_auth():
    r = client.get("/visits/history")
    assert r.status_code == 401


def test_visit_history_rejects_customer_caller():
    r = client.get("/visits/history", headers=_auth_headers(_CUSTOMER_TOKEN))
    assert r.status_code == 403


def test_visit_history_shows_past_visit_before_cancelled_visit():
    # _shared_visit_id was already cancelled above. Add one past-dated visit
    # (never cancelled) so both history buckets are populated.
    past_res = client.post(  # /visits call 10
        "/visits",
        json=_sample_payload(customer_name="Past Visit Customer", date="2020-01-01", time="09:00"),
        headers=_auth_headers(),
    )
    assert past_res.status_code == 200, past_res.text
    past_id = past_res.json()["id"]

    r = client.get("/visits/history", headers=_auth_headers())
    assert r.status_code == 200, r.text
    entries = [v for v in r.json() if v["id"] in (past_id, _shared_visit_id)]
    assert [e["id"] for e in entries] == [past_id, _shared_visit_id]
    assert entries[0]["status"] == "completed"
    assert entries[1]["status"] == "cancelled"


def test_visit_history_scoped_to_own_broker():
    r = client.get("/visits/history", headers=_auth_headers(_OTHER_BROKER_TOKEN))
    assert r.status_code == 200, r.text
    names = [v["customer_name"] for v in r.json()]
    assert "Past Visit Customer" not in names
    assert "Jane Doe" not in names
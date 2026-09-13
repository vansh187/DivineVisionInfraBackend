import os

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from fastapi.testclient import TestClient

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_TOKEN = None
_BROKER_TOKEN = None


def setup_module(module):
    global _CUSTOMER_TOKEN, _BROKER_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    client.post("/customer/signup", json={"username": "profile_cust", "password": "strongpassword", "phone": "9876500014"})
    lr = client.post("/customer/login", json={"username": "profile_cust", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _CUSTOMER_TOKEN = lr.json()["access_token"]

    client.post("/broker/signup", json={"username": "profile_broker", "password": "strongpassword", "phone": "9876500015", "project": "suraksha-enclave"})
    br = client.post("/broker/login", json={"username": "profile_broker", "password": "strongpassword"})
    assert br.status_code == 200, br.text
    _BROKER_TOKEN = br.json()["access_token"]


def test_profile_requires_a_token():
    r = client.get("/customer/profile")
    assert r.status_code == 401


def test_profile_rejects_broker_token_with_customer_only():
    r = client.get("/customer/profile", headers={"Authorization": f"Bearer {_BROKER_TOKEN}"})
    assert r.status_code == 403
    assert r.json()["detail"] == "customer_only"


def test_profile_returns_partial_payload_for_customer_without_booking():
    r = client.get("/customer/profile", headers={"Authorization": f"Bearer {_CUSTOMER_TOKEN}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_id"].startswith("C")
    assert body["booking"]["has_booking"] is False


def test_profile_rejects_garbage_token():
    r = client.get("/customer/profile", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"

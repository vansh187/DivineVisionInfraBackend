import os
import json
from fastapi.testclient import TestClient

# Ensure test env before importing app/persistence
os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app


client = TestClient(app)


def setup_module(module):
    # ensure fresh sqlite file for tests
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()


def test_customer_signup_and_login():
    # ensure tables exist in the same engine/session
    PersistenceDB().create_tables()

    payload = {
        "username": "cust1",
        "password": "strongpassword",
        "email": "cust1@example.com",
        "phone": "+1234567890",
        "first_name": "Cust",
        "last_name": "One"
    }
    r = client.post("/customer/signup", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"].startswith("C") and len(data["id"]) == 6

    lr = client.post("/customer/login", json={"username": payload["username"], "password": payload["password"]})
    assert lr.status_code == 200, lr.text
    token = lr.json().get("access_token")
    assert token and isinstance(token, str)


def test_broker_signup_and_login():
    # ensure tables exist in the same engine/session
    PersistenceDB().create_tables()

    payload = {
        "username": "broker1",
        "password": "anotherstrongpass",
        "email": "broker1@example.com",
        "phone": "+1987654321",
        "first_name": "Broker",
        "last_name": "One"
    }
    r = client.post("/broker/signup", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"].startswith("B") and len(data["id"]) == 6

    lr = client.post("/broker/login", json={"username": payload["username"], "password": payload["password"]})
    assert lr.status_code == 200, lr.text
    token = lr.json().get("access_token")
    assert token and isinstance(token, str)

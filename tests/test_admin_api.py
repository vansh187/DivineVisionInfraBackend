import os
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

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


def test_admin_signup_login_and_refresh_happy_path():
    payload = {
        "full_name": "Arjun Mehta",
        "employee_id": "dv1024",
        "email": "arjun@example.com",
        "password": "strongpassword",
    }
    signup = client.post("/admin/signup", json=payload)
    assert signup.status_code == 200, signup.text
    data = signup.json()
    assert data["id"].startswith("A") and len(data["id"]) == 6
    assert data["employee_id"] == "DV1024"  # normalized to uppercase
    assert data["full_name"] == "Arjun Mehta"

    login = client.post("/admin/login", json={"email": payload["email"], "password": payload["password"]})
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 30 * 60
    assert tokens["access_token"] and tokens["refresh_token"]

    refreshed = client.post("/admin/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    refreshed_data = refreshed.json()
    assert refreshed_data["access_token"]
    assert refreshed_data["expires_in"] == 30 * 60


def test_admin_signup_rejects_employee_id_not_starting_with_dv():
    payload = {
        "full_name": "Someone Else",
        "employee_id": "XX1024",
        "email": "someone_else@example.com",
        "password": "strongpassword",
    }
    r = client.post("/admin/signup", json=payload)
    assert r.status_code == 422, r.text


def test_admin_login_rejects_wrong_password():
    r = client.post("/admin/login", json={"email": "arjun@example.com", "password": "wrong-password"})
    assert r.status_code == 401, r.text


def test_admin_refresh_rejects_an_access_token():
    login = client.post("/admin/login", json={"email": "arjun@example.com", "password": "strongpassword"})
    access_token = login.json()["access_token"]

    r = client.post("/admin/refresh", json={"refresh_token": access_token})
    assert r.status_code == 401, r.text


def test_admin_protected_route_rejects_a_refresh_token():
    from DivineService.auth import get_current_admin

    login = client.post("/admin/login", json={"email": "arjun@example.com", "password": "strongpassword"})
    refresh_token = login.json()["refresh_token"]

    with pytest.raises(HTTPException) as exc_info:
        get_current_admin(authorization=f"Bearer {refresh_token}")
    assert exc_info.value.status_code == 401

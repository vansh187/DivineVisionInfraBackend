import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app
from tests.admin_signup_helpers import CapturingOtpEmail, signup_and_verify_admin

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
        "email": "arjun@divinevisioninfra.com",
        "password": "strongpassword",
    }
    signup = signup_and_verify_admin(client, payload)
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


def test_admin_signup_rejects_non_company_domain():
    payload = {
        "full_name": "Outsider",
        "employee_id": "DV9999",
        "email": "outsider@gmail.com",
        "password": "strongpassword",
    }
    r = client.post("/admin/signup", json=payload)
    assert r.status_code == 422, r.text


def test_admin_login_rejects_unverified_account():
    import DivineAPI.admin_api as admin_api

    payload = {
        "full_name": "Pending Person",
        "employee_id": "DV2048",
        "email": "pending@divinevisioninfra.com",
        "password": "strongpassword",
    }
    fake_email = CapturingOtpEmail()
    original_email = admin_api._admin_service._email
    admin_api._admin_service._email = fake_email
    try:
        signup = client.post("/admin/signup", json=payload)
    finally:
        admin_api._admin_service._email = original_email
    assert signup.status_code == 200, signup.text

    login = client.post("/admin/login", json={"email": payload["email"], "password": payload["password"]})
    assert login.status_code == 403, login.text
    assert login.json()["detail"] == "email_not_verified"


def test_admin_profile_autopopulates_current_admin():
    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    token = login.json()["access_token"]

    r = client.get("/admin/profile", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["full_name"] == "Arjun Mehta"
    assert data["employee_id"] == "DV1024"
    assert data["email"] == "arjun@divinevisioninfra.com"
    assert data["initials"] == "AM"
    assert data["phone"] is None
    assert data["avatar_url"] is None


def test_admin_profile_requires_admin_access_token():
    assert client.get("/admin/profile").status_code == 401

    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    refresh_token = login.json()["refresh_token"]
    r = client.get("/admin/profile", headers={"Authorization": f"Bearer {refresh_token}"})
    assert r.status_code == 401, r.text


def test_admin_profile_photo_upload_stores_public_url():
    import DivineAPI.admin_api as admin_api

    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    token = login.json()["access_token"]

    admin_api._admin_service._supabase_url = "https://fake.supabase.co"
    admin_api._admin_service._service_key = "service-role"
    admin_api._admin_service._profile_photo_bucket = "admin-profile-photos"

    tiny_png = b"\x89PNG\r\n\x1a\n" + b"png-data"
    with patch("DivineService.service_admin.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=201)
        r = client.post(
            "/admin/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("avatar.png", tiny_png, "image/png")},
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["avatar_url"].startswith(
        "https://fake.supabase.co/storage/v1/object/public/admin-profile-photos/"
    )
    assert data["avatar_url"].endswith(".png")
    mock_post.assert_called_once()

    profile = client.get("/admin/profile", headers={"Authorization": f"Bearer {token}"})
    assert profile.status_code == 200, profile.text
    assert profile.json()["avatar_url"] == data["avatar_url"]


def test_admin_profile_photo_upload_rejects_invalid_image():
    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    token = login.json()["access_token"]

    r = client.post(
        "/admin/profile/photo",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("avatar.txt", b"not an image", "text/plain")},
    )

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "unsupported_file_type"


def test_admin_signup_rejects_employee_id_not_starting_with_dv():
    payload = {
        "full_name": "Someone Else",
        "employee_id": "XX1024",
        "email": "someone_else@divinevisioninfra.com",
        "password": "strongpassword",
    }
    r = client.post("/admin/signup", json=payload)
    assert r.status_code == 422, r.text


def test_admin_login_rejects_wrong_password():
    r = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "wrong-password"})
    assert r.status_code == 401, r.text


def test_admin_refresh_rejects_an_access_token():
    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    access_token = login.json()["access_token"]

    r = client.post("/admin/refresh", json={"refresh_token": access_token})
    assert r.status_code == 401, r.text


def test_admin_protected_route_rejects_a_refresh_token():
    from DivineService.auth import get_current_admin

    login = client.post("/admin/login", json={"email": "arjun@divinevisioninfra.com", "password": "strongpassword"})
    refresh_token = login.json()["refresh_token"]

    with pytest.raises(HTTPException) as exc_info:
        get_current_admin(authorization=f"Bearer {refresh_token}")
    assert exc_info.value.status_code == 401

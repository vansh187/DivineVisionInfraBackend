import os
import jwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient

# Ensure test env before importing app/persistence
os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app


client = TestClient(app)

FAKE_SIGNED_URL = "https://fake.supabase.co/storage/v1/object/sign/documents/fake.pdf?token=abc"


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()


def _signup_and_login_customer(username="dockust1"):
    client.post("/customer/signup", json={
        "username": username, "password": "strongpassword", "email": f"{username}@example.com",
    })
    lr = client.post("/customer/login", json={"username": username, "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"]


def test_generate_document_requires_auth():
    r = client.post("/documents/generate", json={"document_type": "kyc", "form_data": {"name": "A"}})
    assert r.status_code == 401


def test_generate_document_rejects_garbage_token():
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert r.status_code == 401


def test_generate_document_rejects_expired_token():
    expired_payload = {
        "sub": "C00001",
        "username": "someone",
        "role": "customer",
        "exp": (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp(),
    }
    expired_token = jwt.encode(expired_payload, "testsecret", algorithm="HS256")
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r.status_code == 401


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value="C00001/kyc_123.pdf")
def test_generate_document_happy_path(mock_upload, mock_sign):
    token = _signup_and_login_customer("dockust_happy")
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"full_name": "Jane Doe", "pan": "ABCDE1234F"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_type"] == "kyc"
    assert data["owner_role"] == "customer"
    assert data["status"] == "generated"
    assert data["signed_url"] == FAKE_SIGNED_URL
    assert data["signed_url_expires_in"] == 3600
    mock_upload.assert_called_once()
    mock_sign.assert_called_once()


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value="C00002/kyc_123.pdf")
def test_get_document_forbidden_for_other_owner(mock_upload, mock_sign):
    owner_token = _signup_and_login_customer("dockust_owner")
    other_token = _signup_and_login_customer("dockust_other")

    create_resp = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"full_name": "Owner"}},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert create_resp.status_code == 200, create_resp.text
    document_id = create_resp.json()["id"]

    r = client.get(f"/documents/{document_id}", headers={"Authorization": f"Bearer {other_token}"})
    assert r.status_code == 403


def test_get_document_not_found():
    token = _signup_and_login_customer("dockust_notfound")
    r = client.get("/documents/does-not-exist", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404

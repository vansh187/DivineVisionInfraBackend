import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

import pytest
from DivineDTO.models import AdminLoginDTO
from fastapi.testclient import TestClient

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app
import DivineAPI.admin_api as admin_api
from tests.admin_signup_helpers import mint_admin_access_token, signup_and_verify_admin

client = TestClient(app)

PASSWORD = "strongpassword"
SUPER = {"full_name": "Himanshu Super", "employee_id": "DV0001",
         "email": "himanshu@divinevisioninfra.com", "password": PASSWORD}


def _login(email, password=PASSWORD):
    return client.post("/admin/login", json={"email": email, "password": password})


def _auth(email):
    # Minted rather than logged in: the app's rate limiter buckets by client+path,
    # and /admin/login's budget is shared with every other admin test file.
    user = admin_api._admin_service._persistence.get_by_email(email)
    return {"Authorization": f"Bearer {mint_admin_access_token(user.id, user.email)}"}


def _service_login(email):
    return admin_api._admin_service.login(AdminLoginDTO(email=email, password=PASSWORD))


@pytest.fixture(scope="module", autouse=True)
def _db():
    PersistenceDB().create_tables()
    # The super admin signs up through the real flow (approve=False) - it must be
    # auto-approved purely by matching the configured email + employee id.
    if not admin_api._admin_service._persistence.get_by_email(SUPER["email"]):
        signup_and_verify_admin(client, SUPER, approve=False)
    yield


def _pending_admin(employee_id, email):
    """Signed up + email verified, awaiting approval. Seeded through the service
    layer to stay off the rate-limited /admin/signup and /admin/verify-signup."""
    svc = admin_api._admin_service
    persistence = svc._persistence
    existing = persistence.get_by_email(email)
    if existing:
        return existing.id
    user = persistence.create_user("New Joiner", employee_id, email, svc._hash_password(PASSWORD))
    persistence.mark_email_verified(email)
    return user.id


def test_super_admin_is_auto_approved_and_flagged_in_profile():
    assert _service_login(SUPER["email"])["access_token"]
    headers = _auth(SUPER["email"])
    profile = client.get("/admin/profile", headers=headers).json()
    assert profile["is_super_admin"] is True


def test_verified_admin_cannot_login_until_super_admin_approves():
    admin_id = _pending_admin("DV5001", "joiner1@divinevisioninfra.com")

    r = _login("joiner1@divinevisioninfra.com")
    assert r.status_code == 403 and r.json()["detail"] == "approval_pending"

    headers = _auth(SUPER["email"])
    pending = client.get("/admin/approvals", headers=headers)
    assert pending.status_code == 200, pending.text
    assert admin_id in [item["id"] for item in pending.json()["items"]]

    approved = client.post(f"/admin/approvals/{admin_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_status"] == "approved"
    assert approved.json()["approval_decided_by"].startswith("admin:")

    assert _service_login("joiner1@divinevisioninfra.com")["access_token"]
    again = client.post(f"/admin/approvals/{admin_id}/approve", headers=headers)
    assert again.status_code == 409


def test_rejected_admin_cannot_login():
    admin_id = _pending_admin("DV5002", "joiner2@divinevisioninfra.com")
    headers = _auth(SUPER["email"])
    r = client.post(f"/admin/approvals/{admin_id}/reject", headers=headers, json={"note": "Not an employee"})
    assert r.status_code == 200, r.text
    assert r.json()["approval_note"] == "Not an employee"

    with pytest.raises(ValueError, match="approval_rejected"):
        _service_login("joiner2@divinevisioninfra.com")


def test_regular_admin_cannot_use_approval_endpoints():
    admin_id = _pending_admin("DV5003", "joiner3@divinevisioninfra.com")
    admin_api._admin_service._persistence.set_approval_status(admin_id, "approved", decided_by="test")
    headers = _auth("joiner3@divinevisioninfra.com")

    assert client.get("/admin/approvals", headers=headers).status_code == 403
    assert client.post(f"/admin/approvals/{admin_id}/approve", headers=headers).status_code == 403


def test_matching_email_with_wrong_employee_id_is_not_super_admin():
    svc = admin_api._admin_service
    assert svc.is_super_admin(MagicMock(email="EA@divinevisioninfra.com", employee_id="dv0011"))
    assert not svc.is_super_admin(MagicMock(email="ea@divinevisioninfra.com", employee_id="DV9999"))
    assert not svc.is_super_admin(None)


def test_unknown_target_returns_404():
    headers = _auth(SUPER["email"])
    assert client.post("/admin/approvals/A99999/approve", headers=headers).status_code == 404


def test_unexpected_persistence_error_becomes_500_not_a_crash():
    headers = _auth(SUPER["email"])
    persistence = admin_api._admin_service._persistence
    original = persistence.list_by_approval_status
    persistence.list_by_approval_status = MagicMock(side_effect=Exception("db down"))
    try:
        r = client.get("/admin/approvals", headers=headers)
    finally:
        persistence.list_by_approval_status = original
    assert r.status_code == 500 and r.json()["detail"] == "internal_error"

import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import jwt
import pytest
from DivineService.service_admin import serviceAdmin
from DivineDTO.models import AdminCreateDTO, AdminLoginDTO


def _service():
    persistence = MagicMock()
    return serviceAdmin(persistence, secret_key="testsecret"), persistence


def _dto(**overrides):
    values = dict(full_name="Arjun Mehta", employee_id="DV1024", email="arjun@example.com", password="strongpassword")
    values.update(overrides)
    return AdminCreateDTO(**values)


def test_constructor_raises_without_secret():
    persistence = MagicMock()
    old = os.environ.pop("ADMIN_JWT_SECRET_KEY", None)
    try:
        with pytest.raises(RuntimeError):
            serviceAdmin(persistence, secret_key=None)
    finally:
        if old is not None:
            os.environ["ADMIN_JWT_SECRET_KEY"] = old


def test_constructor_defaults_persistence_when_none_given():
    svc = serviceAdmin(secret_key="testsecret")
    assert svc._persistence is not None


def test_signup_rejects_duplicate_employee_id():
    svc, persistence = _service()
    persistence.get_by_employee_id.return_value = MagicMock()
    with pytest.raises(ValueError) as exc_info:
        svc.signup(_dto())
    assert str(exc_info.value) == "employee_id_taken"
    persistence.create_user.assert_not_called()


def test_signup_rejects_duplicate_email():
    svc, persistence = _service()
    persistence.get_by_employee_id.return_value = None
    persistence.get_by_email.return_value = MagicMock()
    with pytest.raises(ValueError) as exc_info:
        svc.signup(_dto())
    assert str(exc_info.value) == "email_taken"
    persistence.create_user.assert_not_called()


def test_signup_hashes_password_before_persisting():
    svc, persistence = _service()
    persistence.get_by_employee_id.return_value = None
    persistence.get_by_email.return_value = None
    persistence.create_user.return_value = MagicMock(id="A00001")
    svc.signup(_dto(), created_by="1.2.3.4")

    args = persistence.create_user.call_args.args
    hashed_password = args[3]
    assert hashed_password != "strongpassword"
    assert hashed_password.startswith("$2b$")


def test_login_rejects_unknown_email():
    svc, persistence = _service()
    persistence.get_by_email.return_value = None
    with pytest.raises(ValueError):
        svc.login(AdminLoginDTO(email="ghost@example.com", password="whatever"))


def test_login_rejects_wrong_password():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_email.return_value = MagicMock(id="A00001", email="admin1@example.com", password_hash=real_hash)
    with pytest.raises(ValueError):
        svc.login(AdminLoginDTO(email="admin1@example.com", password="wrong-password"))


def test_login_returns_access_and_refresh_tokens():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_email.return_value = MagicMock(id="A00001", email="admin1@example.com", password_hash=real_hash)
    tokens = svc.login(AdminLoginDTO(email="admin1@example.com", password="correct-password"))

    assert set(tokens.keys()) == {"access_token", "refresh_token", "expires_in"}
    assert tokens["expires_in"] == 30 * 60

    access_payload = jwt.decode(tokens["access_token"], "testsecret", algorithms=["HS256"])
    assert access_payload["sub"] == "A00001"
    assert access_payload["role"] == "admin"
    assert access_payload["type"] == "access"

    refresh_payload = jwt.decode(tokens["refresh_token"], "testsecret", algorithms=["HS256"])
    assert refresh_payload["sub"] == "A00001"
    assert refresh_payload["role"] == "admin"
    assert refresh_payload["type"] == "refresh"


def test_refresh_rejects_an_access_token():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_email.return_value = MagicMock(id="A00001", email="admin1@example.com", password_hash=real_hash)
    tokens = svc.login(AdminLoginDTO(email="admin1@example.com", password="correct-password"))

    with pytest.raises(ValueError):
        svc.refresh(tokens["access_token"])


def test_refresh_rejects_a_token_for_a_deleted_admin():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_email.return_value = MagicMock(id="A00001", email="admin1@example.com", password_hash=real_hash)
    tokens = svc.login(AdminLoginDTO(email="admin1@example.com", password="correct-password"))

    persistence.get_by_id.return_value = None
    with pytest.raises(ValueError):
        svc.refresh(tokens["refresh_token"])


def test_refresh_issues_a_new_access_token():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_email.return_value = MagicMock(id="A00001", email="admin1@example.com", password_hash=real_hash)
    tokens = svc.login(AdminLoginDTO(email="admin1@example.com", password="correct-password"))

    persistence.get_by_id.return_value = MagicMock(id="A00001", email="admin1@example.com")
    result = svc.refresh(tokens["refresh_token"])

    assert set(result.keys()) == {"access_token", "expires_in"}
    payload = jwt.decode(result["access_token"], "testsecret", algorithms=["HS256"])
    assert payload["sub"] == "A00001"
    assert payload["type"] == "access"


def test_refresh_rejects_garbage_token():
    svc, persistence = _service()
    with pytest.raises(ValueError):
        svc.refresh("not.a.valid.jwt")

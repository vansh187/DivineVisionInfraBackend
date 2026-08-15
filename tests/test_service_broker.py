import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import jwt
import pytest
from DivineService.service_broker import serviceBroker
from DivineDTO.models import UserCreateDTO, UserLoginDTO


def _service():
    persistence = MagicMock()
    return serviceBroker(persistence, secret_key="testsecret"), persistence


def test_constructor_raises_without_secret():
    persistence = MagicMock()
    old = os.environ.pop("JWT_SECRET_KEY", None)
    try:
        with pytest.raises(RuntimeError):
            serviceBroker(persistence, secret_key=None)
    finally:
        if old is not None:
            os.environ["JWT_SECRET_KEY"] = old


def test_constructor_defaults_persistence_when_none_given():
    svc = serviceBroker(secret_key="testsecret")
    assert svc._persistence is not None


def test_signup_rejects_duplicate_username():
    svc, persistence = _service()
    persistence.get_by_username.return_value = MagicMock()
    dto = UserCreateDTO(username="taken", password="strongpassword")
    with pytest.raises(ValueError):
        svc.signup(dto)
    persistence.create_user.assert_not_called()


def test_login_rejects_unknown_username():
    svc, persistence = _service()
    persistence.get_by_username.return_value = None
    with pytest.raises(ValueError):
        svc.login(UserLoginDTO(username="ghost", password="whatever"))


def test_login_rejects_wrong_password():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_username.return_value = MagicMock(id="B00001", username="broker1", password_hash=real_hash)
    with pytest.raises(ValueError):
        svc.login(UserLoginDTO(username="broker1", password="wrong-password"))


def test_login_returns_valid_jwt_with_broker_role():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_username.return_value = MagicMock(id="B00001", username="broker1", password_hash=real_hash)
    token = svc.login(UserLoginDTO(username="broker1", password="correct-password"))

    payload = jwt.decode(token, "testsecret", algorithms=["HS256"])
    assert payload["sub"] == "B00001"
    assert payload["username"] == "broker1"
    assert payload["role"] == "broker"  # not "customer" - this is the one line that differs from serviceCustomer
    assert "exp" in payload

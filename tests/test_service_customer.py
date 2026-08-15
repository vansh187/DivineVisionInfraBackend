import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import jwt
import pytest
from DivineService.service_customer import serviceCustomer
from DivineDTO.models import UserCreateDTO, UserLoginDTO


def _service():
    persistence = MagicMock()
    return serviceCustomer(persistence, secret_key="testsecret"), persistence


def test_constructor_raises_without_secret():
    persistence = MagicMock()
    old = os.environ.pop("JWT_SECRET_KEY", None)
    try:
        with pytest.raises(RuntimeError):
            serviceCustomer(persistence, secret_key=None)
    finally:
        if old is not None:
            os.environ["JWT_SECRET_KEY"] = old


def test_constructor_defaults_persistence_when_none_given():
    # No persistence passed in - must self-construct rather than crash or store None.
    svc = serviceCustomer(secret_key="testsecret")
    assert svc._persistence is not None


def test_signup_rejects_duplicate_username():
    svc, persistence = _service()
    persistence.get_by_username.return_value = MagicMock()  # already exists
    dto = UserCreateDTO(username="taken", password="strongpassword")
    with pytest.raises(ValueError):
        svc.signup(dto)
    persistence.create_user.assert_not_called()


def test_signup_hashes_password_before_persisting():
    svc, persistence = _service()
    persistence.get_by_username.return_value = None
    persistence.create_user.return_value = MagicMock(id="C00001")
    dto = UserCreateDTO(username="new_user", password="strongpassword", email="a@b.com")
    svc.signup(dto, created_by="1.2.3.4")

    _, kwargs = persistence.create_user.call_args
    args = persistence.create_user.call_args.args
    hashed_password = args[1] if len(args) > 1 else kwargs.get("password_hash")
    assert hashed_password != "strongpassword"  # never stored in plaintext
    assert hashed_password.startswith("$2b$")  # bcrypt hash prefix


def test_login_rejects_unknown_username():
    svc, persistence = _service()
    persistence.get_by_username.return_value = None
    with pytest.raises(ValueError):
        svc.login(UserLoginDTO(username="ghost", password="whatever"))


def test_login_rejects_wrong_password():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_username.return_value = MagicMock(id="C00001", username="cust1", password_hash=real_hash)
    with pytest.raises(ValueError):
        svc.login(UserLoginDTO(username="cust1", password="wrong-password"))


def test_login_returns_valid_jwt_with_customer_role():
    svc, persistence = _service()
    real_hash = svc._hash_password("correct-password")
    persistence.get_by_username.return_value = MagicMock(id="C00001", username="cust1", password_hash=real_hash)
    token = svc.login(UserLoginDTO(username="cust1", password="correct-password"))

    payload = jwt.decode(token, "testsecret", algorithms=["HS256"])
    assert payload["sub"] == "C00001"
    assert payload["username"] == "cust1"
    assert payload["role"] == "customer"
    assert "exp" in payload

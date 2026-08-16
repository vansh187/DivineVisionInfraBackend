import os
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import jwt
import pytest
from fastapi import HTTPException
from DivineService.auth import get_current_admin_or_broker, get_current_user, _get_secret


def _token(payload_overrides=None, secret="testsecret", exp_delta=timedelta(hours=1)):
    payload = {
        "sub": "C00001",
        "username": "cust1",
        "role": "customer",
        "exp": (datetime.now(timezone.utc) + exp_delta).timestamp(),
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_valid_token_returns_expected_dict():
    result = get_current_user(authorization=f"Bearer {_token()}")
    assert result == {"sub": "C00001", "username": "cust1", "role": "customer"}


def test_valid_token_broker_role():
    result = get_current_user(authorization=f"Bearer {_token({'role': 'broker', 'sub': 'B00001'})}")
    assert result["role"] == "broker"


def test_valid_token_admin_role_for_admin_or_broker_dependency():
    result = get_current_admin_or_broker(authorization=f"Bearer {_token({'role': 'admin', 'sub': 'admin_1'})}")
    assert result["role"] == "admin"


def test_admin_role_is_not_valid_for_regular_user_dependency():
    token = _token({"role": "admin", "sub": "admin_1"})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_missing_header_raises_401_missing_token():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing_token"


def test_header_without_bearer_prefix_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=_token())  # raw token, no "Bearer " prefix
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing_token"


def test_garbage_token_raises_401_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization="Bearer not.a.valid.jwt")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_wrong_signature_raises_401_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {_token(secret='someone-elses-secret')}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_expired_token_raises_401_token_expired():
    expired = _token(exp_delta=timedelta(hours=-1))
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {expired}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "token_expired"


def test_missing_sub_claim_raises_401_invalid_token():
    token = jwt.encode(
        {"role": "customer", "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()},
        "testsecret", algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_invalid_role_claim_raises_401_invalid_token():
    # Not "customer" or "broker" - e.g. a forged/corrupted token from an older schema.
    token = _token({"role": "auditor"})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_missing_role_claim_raises_401_invalid_token():
    token = jwt.encode(
        {"sub": "C00001", "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()},
        "testsecret", algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_bearer_prefix_is_case_insensitive():
    result = get_current_user(authorization=f"bearer {_token()}")
    assert result["sub"] == "C00001"


def test_get_secret_raises_runtime_error_when_unset():
    old = os.environ.pop("JWT_SECRET_KEY", None)
    try:
        with pytest.raises(RuntimeError):
            _get_secret()
    finally:
        if old is not None:
            os.environ["JWT_SECRET_KEY"] = old


def test_missing_secret_surfaces_as_500_server_misconfigured():
    old = os.environ.pop("JWT_SECRET_KEY", None)
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(authorization=f"Bearer {_token()}")
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == "server_misconfigured"
    finally:
        if old is not None:
            os.environ["JWT_SECRET_KEY"] = old

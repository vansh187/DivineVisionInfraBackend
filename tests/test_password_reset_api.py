import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from passlib.context import CryptContext
from fastapi.testclient import TestClient

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_broker import persistenceBroker
from Divinepersistence.persistence_password_reset import persistencePasswordReset
from DivineDTO.models import UserLoginDTO
from DivineService.service_customer import serviceCustomer
from DivineService.service_broker import serviceBroker
from DivineAPI.main import app
from DivineAPI import customer_api, broker_api

client = TestClient(app)
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Both singletons build their own serviceEmail() at import time, which would try a
# real Resend call. Swap in a mock that reports "sent" so /forgot-password's happy
# path doesn't depend on network access or real credentials, matching how
# test_payments.py patches the payment singleton's Razorpay keys directly instead
# of hitting the real gateway.
customer_api._password_reset_service._email = MagicMock(enabled=True, send_otp_email=MagicMock(return_value=True))
broker_api._password_reset_service._email = MagicMock(enabled=True, send_otp_email=MagicMock(return_value=True))

# Seeded directly through the persistence layer rather than POST /customer|broker/signup:
# this module and every other test file share one FastAPI `app` instance (and therefore
# one RateLimitMiddleware storage) for the whole pytest session, and /customer/signup,
# /customer/login etc. are already at that middleware's 10-calls/60s ceiling from the rest
# of the suite - any extra HTTP call to those specific paths intermittently 429s a
# *different* test file's setup depending on run order. Bypassing the HTTP layer for
# account setup / password verification below avoids spending any of that shared budget;
# /{role}/forgot-password and /{role}/reset-password are new paths this file has
# exclusive, comfortably under-budget use of.
_customer_persistence = persistenceCustomer()
_broker_persistence = persistenceBroker()


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    _customer_persistence.create_user(
        "pwresetcust", _pwd.hash("originalpass1"), email="pwreset.cust@example.com", first_name="Riya",
    )
    _broker_persistence.create_user(
        "pwresetbroker", _pwd.hash("originalpass1"), email="pwreset.broker@example.com",
    )


def _sent_otp():
    """Pulls the OTP the mocked email service was just asked to send."""
    return customer_api._password_reset_service._email.send_otp_email.call_args[0][1]


def _sent_otp_broker():
    return broker_api._password_reset_service._email.send_otp_email.call_args[0][1]


def _can_login(service_cls, persistence, username: str, password: str) -> bool:
    """Verifies a password directly through the real service login logic (hashing,
    lookup, comparison) without spending any of the shared /{role}/login HTTP budget."""
    svc = service_cls(persistence, secret_key="testsecret")
    try:
        svc.login(UserLoginDTO(username=username, password=password))
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# POST /customer/forgot-password                                              #
# --------------------------------------------------------------------------- #
def test_forgot_password_rejects_malformed_email():
    r = client.post("/customer/forgot-password", json={"email": "not-an-email"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_forgot_password_returns_200_for_unregistered_email():
    r = client.post("/customer/forgot-password", json={"email": "definitely.nobody@example.com"})
    assert r.status_code == 200
    assert r.json() == {"message": "If that email is registered, an OTP has been sent."}
    customer_api._password_reset_service._email.send_otp_email.assert_not_called()


def test_forgot_password_returns_200_for_registered_email_and_sends_otp():
    r = client.post("/customer/forgot-password", json={"email": "pwreset.cust@example.com"})
    assert r.status_code == 200
    assert r.json() == {"message": "If that email is registered, an OTP has been sent."}
    customer_api._password_reset_service._email.send_otp_email.assert_called_once()


def test_forgot_password_second_request_inside_cooldown_is_429():
    r = client.post("/customer/forgot-password", json={"email": "pwreset.cust@example.com"})
    assert r.status_code == 429
    assert r.json()["detail"] == "too_many_requests"


# --------------------------------------------------------------------------- #
# POST /customer/reset-password                                               #
# --------------------------------------------------------------------------- #
def test_reset_password_requires_a_prior_otp_request():
    r = client.post("/customer/reset-password", json={
        "email": "never.requested@example.com", "otp": "123456", "new_password": "NewPass!234",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "otp_not_requested"


def test_reset_password_rejects_short_new_password():
    otp = _sent_otp()
    r = client.post("/customer/reset-password", json={
        "email": "pwreset.cust@example.com", "otp": otp, "new_password": "short",
    })
    assert r.status_code == 422


def test_reset_password_rejects_wrong_otp():
    r = client.post("/customer/reset-password", json={
        "email": "pwreset.cust@example.com", "otp": "000000", "new_password": "NewPass!234",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_otp"


def test_reset_password_succeeds_with_correct_otp_and_new_password_logs_in():
    otp = _sent_otp()
    r = client.post("/customer/reset-password", json={
        "email": "pwreset.cust@example.com", "otp": otp, "new_password": "NewPass!234",
    })
    assert r.status_code == 200
    assert r.json() == {"message": "Password has been reset."}

    assert _can_login(serviceCustomer, _customer_persistence, "pwresetcust", "originalpass1") is False
    assert _can_login(serviceCustomer, _customer_persistence, "pwresetcust", "NewPass!234") is True


def test_reset_password_otp_is_single_use():
    """The OTP that just succeeded above must not still work for a second reset."""
    r = client.post("/customer/reset-password", json={
        "email": "pwreset.cust@example.com", "otp": "000000", "new_password": "AnotherPass!234",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "otp_not_requested"


# --------------------------------------------------------------------------- #
# role scoping: broker path is independent of customer path                   #
# --------------------------------------------------------------------------- #
def test_broker_forgot_password_round_trip_is_independent_of_customer():
    r = client.post("/broker/forgot-password", json={"email": "pwreset.broker@example.com"})
    assert r.status_code == 200
    broker_api._password_reset_service._email.send_otp_email.assert_called_once()

    otp = _sent_otp_broker()
    reset = client.post("/broker/reset-password", json={
        "email": "pwreset.broker@example.com", "otp": otp, "new_password": "BrokerNewPass1",
    })
    assert reset.status_code == 200

    assert _can_login(serviceBroker, _broker_persistence, "pwresetbroker", "BrokerNewPass1") is True


# --------------------------------------------------------------------------- #
# lockout after repeated wrong attempts                                       #
# --------------------------------------------------------------------------- #
# The exact attempt-counting / lockout-threshold logic is already covered end to
# end at the service layer (test_service_password_reset.py). This test forces the
# locked state directly at the persistence layer (exactly what 5 real wrong guesses
# would have left behind) and only spends one HTTP call confirming the API maps it
# to 429 too_many_attempts.
def test_reset_password_returns_429_once_locked_out():
    _customer_persistence.create_user(
        "pwresetlockout", _pwd.hash("originalpass1"), email="pwreset.lockout@example.com",
    )
    r = client.post("/customer/forgot-password", json={"email": "pwreset.lockout@example.com"})
    assert r.status_code == 200

    persistencePasswordReset().lock_until(
        "customer", "pwreset.lockout@example.com", datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    locked = client.post("/customer/reset-password", json={
        "email": "pwreset.lockout@example.com", "otp": "111111", "new_password": "NewPass!234",
    })
    assert locked.status_code == 429
    assert locked.json()["detail"] == "too_many_attempts"

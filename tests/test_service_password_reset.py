import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import pytest

from DivineService.service_password_reset import servicePasswordReset, PasswordResetError


def _svc():
    users = MagicMock()
    otp = MagicMock()
    email = MagicMock()
    email.enabled = True
    email.send_otp_email.return_value = True
    svc = servicePasswordReset(role="customer", user_persistence=users, otp_persistence=otp, email=email)
    return svc, users, otp, email


def _otp_row(**kw):
    now = datetime.now(timezone.utc)
    base = {
        "otp_hash": None, "attempts": 0, "locked_until": None,
        "expires_at": now + timedelta(minutes=10), "last_sent_date": now,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# forgot_password                                                             #
# --------------------------------------------------------------------------- #
def test_forgot_password_unknown_email_is_a_silent_noop():
    """No account-enumeration leak: an unregistered email must behave exactly like
    a successful send from the caller's point of view - no exception, no OTP row,
    no email attempted."""
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = None

    svc.forgot_password("nobody@example.com")

    otp.get_by_role_email.assert_not_called()
    otp.upsert_otp.assert_not_called()
    email.send_otp_email.assert_not_called()


def test_forgot_password_sends_otp_for_registered_email():
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com", first_name="Riya")
    otp.get_by_role_email.return_value = None

    svc.forgot_password("a@b.com")

    otp.upsert_otp.assert_called_once()
    email.send_otp_email.assert_called_once()


def test_forgot_password_rejects_resend_inside_cooldown():
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com", first_name=None)
    otp.get_by_role_email.return_value = _otp_row(last_sent_date=datetime.now(timezone.utc))

    with pytest.raises(PasswordResetError) as exc:
        svc.forgot_password("a@b.com")
    assert exc.value.code == "too_many_requests"
    assert exc.value.status_code == 429
    otp.upsert_otp.assert_not_called()


def test_forgot_password_allows_resend_after_cooldown_elapsed():
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com", first_name=None)
    stale = datetime.now(timezone.utc) - timedelta(seconds=31)
    otp.get_by_role_email.return_value = _otp_row(last_sent_date=stale)

    svc.forgot_password("a@b.com")

    otp.upsert_otp.assert_called_once()


def test_forgot_password_surfaces_email_send_failure():
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com", first_name=None)
    otp.get_by_role_email.return_value = None
    email.send_otp_email.return_value = False

    with pytest.raises(PasswordResetError) as exc:
        svc.forgot_password("a@b.com")
    assert exc.value.code == "email_send_failed"
    assert exc.value.status_code == 500


def test_forgot_password_wraps_unexpected_user_lookup_error():
    svc, users, otp, email = _svc()
    users.get_by_email.side_effect = RuntimeError("db down")

    with pytest.raises(PasswordResetError) as exc:
        svc.forgot_password("a@b.com")
    assert exc.value.code == "internal_error"
    assert exc.value.status_code == 500


def test_forgot_password_never_raises_a_bare_exception():
    """The one hard requirement: whatever goes wrong internally, only
    PasswordResetError may escape - never a raw exception type."""
    svc, users, otp, email = _svc()
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com", first_name=None)
    otp.get_by_role_email.side_effect = RuntimeError("boom")

    with pytest.raises(PasswordResetError):
        svc.forgot_password("a@b.com")


# --------------------------------------------------------------------------- #
# reset_password                                                              #
# --------------------------------------------------------------------------- #
def test_reset_password_requires_a_prior_request():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = None

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "123456", "NewPass!234")
    assert exc.value.code == "otp_not_requested"
    assert exc.value.status_code == 400


def test_reset_password_rejects_expired_otp():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(
        otp_hash=svc._hash_otp("482913"), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "482913", "NewPass!234")
    assert exc.value.code == "otp_expired"
    assert exc.value.status_code == 400


def test_reset_password_locked_out_returns_429_without_checking_the_code():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "000000", "NewPass!234")
    assert exc.value.code == "too_many_attempts"
    assert exc.value.status_code == 429
    users.get_by_email.assert_not_called()


def test_reset_password_wrong_otp_increments_attempts():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(otp_hash=svc._hash_otp("482913"))
    otp.increment_attempts.return_value = SimpleNamespace(attempts=1)

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "000000", "NewPass!234")
    assert exc.value.code == "invalid_otp"
    assert exc.value.status_code == 400
    otp.increment_attempts.assert_called_once_with("customer", "a@b.com")
    otp.lock_until.assert_not_called()


def test_reset_password_locks_out_after_max_attempts():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(otp_hash=svc._hash_otp("482913"))
    otp.increment_attempts.return_value = SimpleNamespace(attempts=5)

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "000000", "NewPass!234")
    assert exc.value.code == "invalid_otp"          # this attempt still reports the wrong-code reason
    otp.lock_until.assert_called_once()             # but the account is now locked for the next one


def test_reset_password_succeeds_updates_password_and_clears_otp():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(otp_hash=svc._hash_otp("482913"))
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com")

    svc.reset_password("a@b.com", "482913", "NewPass!234")

    users.update_password.assert_called_once()
    assert users.update_password.call_args[0][0] == "C00001"
    otp.delete.assert_called_once_with("customer", "a@b.com")


def test_reset_password_missing_user_after_valid_otp_is_treated_as_not_requested():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(otp_hash=svc._hash_otp("482913"))
    users.get_by_email.return_value = None

    with pytest.raises(PasswordResetError) as exc:
        svc.reset_password("a@b.com", "482913", "NewPass!234")
    assert exc.value.code == "otp_not_requested"
    users.update_password.assert_not_called()


def test_reset_password_survives_otp_cleanup_failure_after_success():
    """The password change already happened - a failure deleting the now-stale OTP
    row must not turn a successful reset into an error."""
    svc, users, otp, email = _svc()
    otp.get_by_role_email.return_value = _otp_row(otp_hash=svc._hash_otp("482913"))
    users.get_by_email.return_value = SimpleNamespace(id="C00001", email="a@b.com")
    otp.delete.side_effect = RuntimeError("db down")

    svc.reset_password("a@b.com", "482913", "NewPass!234")  # must not raise

    users.update_password.assert_called_once()


def test_reset_password_never_raises_a_bare_exception():
    svc, users, otp, email = _svc()
    otp.get_by_role_email.side_effect = RuntimeError("boom")

    with pytest.raises(PasswordResetError):
        svc.reset_password("a@b.com", "482913", "NewPass!234")

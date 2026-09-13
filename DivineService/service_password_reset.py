import logging
import secrets
from datetime import datetime, timedelta, timezone

from passlib.context import CryptContext

from Divinepersistence import persistencePasswordReset
from DivineService.service_email import serviceEmail

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
RESEND_COOLDOWN_SECONDS = 30
MAX_OTP_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class PasswordResetError(Exception):
    """A known, expected outcome (bad OTP, cooldown, lockout, mail failure) - never
    a bug. Carries the HTTP status the API layer should return alongside `code` as
    the response `detail`, so the route handler never has to guess a mapping."""

    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _as_aware(value):
    """Rows here are read via raw text() SQL (not the ORM), so a DateTime column
    can come back as either a python datetime OR a plain string - sqlite in
    particular never round-trips a python type through raw SQL the way the ORM
    does. Either way, a naive value is treated as UTC, matching how every write in
    this module already stores it, so comparisons never raise 'can't compare
    offset-naive and offset-aware datetimes' (and a malformed/unparseable value
    safely becomes None rather than raising)."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("password_reset.timestamp_normalize_failed error=%s", e)
        return None


class servicePasswordReset:
    """Email-OTP password reset, scoped per role (customer/broker) so a code sent
    for one role's account can never be replayed against the other. Every public
    method either succeeds or raises PasswordResetError - no other exception is
    allowed to escape, so the API layer's try/except is exhaustive by construction."""

    def __init__(self, role: str, user_persistence, otp_persistence: persistencePasswordReset = None,
                 email: serviceEmail = None):
        self._role = role
        self._user_persistence = user_persistence
        self._otp_persistence = otp_persistence or persistencePasswordReset()
        try:
            self._email = email or serviceEmail()
        except Exception as e:
            logger.warning("password_reset.email_service_init_failed role=%s error=%s", role, e)
            self._email = None

    # ------------------------------------------------------------------ helpers
    def _generate_otp(self) -> str:
        return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"

    def _hash_otp(self, otp: str) -> str:
        return pwd_context.hash(otp)

    def _verify_otp(self, otp: str, otp_hash: str) -> bool:
        try:
            return pwd_context.verify(otp or "", otp_hash or "")
        except Exception as e:
            logger.warning("password_reset.otp_verify_failed role=%s error=%s", self._role, e)
            return False

    def _get_user_by_email(self, email: str):
        try:
            return self._user_persistence.get_by_email(email)
        except Exception as e:
            logger.warning("password_reset.user_lookup_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

    def _get_otp_row(self, email: str):
        try:
            return self._otp_persistence.get_by_role_email(self._role, email)
        except Exception as e:
            logger.warning("password_reset.otp_lookup_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

    # ------------------------------------------------------------------ public
    def forgot_password(self, email: str) -> None:
        """Generates and emails a fresh OTP. Returns normally (no exception) both
        when the OTP was actually sent AND when the email simply isn't registered -
        callers must respond identically either way so this endpoint can't be used
        to enumerate accounts. Only a resend inside the cooldown or a genuine
        send/DB failure raises."""
        email = (email or "").strip().lower()
        if not email:
            return

        user = self._get_user_by_email(email)
        if not user:
            logger.info("password_reset.forgot_password_unknown_email role=%s", self._role)
            return

        existing = self._get_otp_row(email)
        now = datetime.now(timezone.utc)
        if existing is not None:
            last_sent = _as_aware(getattr(existing, "last_sent_date", None))
            if last_sent and (now - last_sent).total_seconds() < RESEND_COOLDOWN_SECONDS:
                raise PasswordResetError("too_many_requests", 429)

        try:
            otp = self._generate_otp()
            otp_hash = self._hash_otp(otp)
        except Exception as e:
            logger.warning("password_reset.otp_generate_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

        expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
        try:
            self._otp_persistence.upsert_otp(self._role, email, otp_hash, expires_at, now)
        except Exception as e:
            logger.warning("password_reset.otp_persist_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

        sent = False
        try:
            if self._email and getattr(self._email, "enabled", False):
                sent = bool(self._email.send_otp_email(
                    getattr(user, "email", None) or email, otp,
                    first_name=getattr(user, "first_name", None),
                    expires_minutes=OTP_EXPIRY_MINUTES,
                ))
        except Exception as e:
            logger.warning("password_reset.email_send_exception role=%s error=%s", self._role, e)
            sent = False
        if not sent:
            raise PasswordResetError("email_send_failed", 500)

    def reset_password(self, email: str, otp: str, new_password: str) -> None:
        """Verifies the OTP and, only on a match, sets the new password. Raises
        PasswordResetError for every rejected path (never requested / wrong code /
        expired / locked out / internal failure) - there is no other way out of
        this method besides a clean return on success."""
        email = (email or "").strip().lower()
        row = self._get_otp_row(email)
        if row is None:
            raise PasswordResetError("otp_not_requested", 400)

        now = datetime.now(timezone.utc)
        locked_until = _as_aware(getattr(row, "locked_until", None))
        if locked_until and now < locked_until:
            raise PasswordResetError("too_many_attempts", 429)

        expires_at = _as_aware(getattr(row, "expires_at", None))
        if not expires_at or now > expires_at:
            raise PasswordResetError("otp_expired", 400)

        if not self._verify_otp(otp, getattr(row, "otp_hash", None)):
            self._register_failed_attempt(email)
            raise PasswordResetError("invalid_otp", 400)

        user = self._get_user_by_email(email)
        if not user:
            # Account vanished between the OTP request and this call - nothing safe
            # left to update. Same code as "never requested": from the caller's
            # point of view there is no live reset in progress for this email.
            raise PasswordResetError("otp_not_requested", 400)

        try:
            hashed = pwd_context.hash(new_password)
        except Exception as e:
            logger.warning("password_reset.password_hash_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

        try:
            self._user_persistence.update_password(user.id, hashed)
        except Exception as e:
            logger.warning("password_reset.password_update_failed role=%s error=%s", self._role, e)
            raise PasswordResetError("internal_error", 500) from e

        try:
            self._otp_persistence.delete(self._role, email)
        except Exception as e:
            # The password change already succeeded - a leftover OTP row is a stale
            # artifact (the next forgot_password() upserts over it), not a
            # correctness problem, so this must not turn a successful reset into
            # an error response.
            logger.warning("password_reset.otp_cleanup_failed role=%s error=%s", self._role, e)

    def _register_failed_attempt(self, email: str) -> None:
        try:
            updated = self._otp_persistence.increment_attempts(self._role, email)
        except Exception as e:
            logger.warning("password_reset.attempt_increment_failed role=%s error=%s", self._role, e)
            return
        attempts = getattr(updated, "attempts", None) if updated else None
        if isinstance(attempts, int) and attempts >= MAX_OTP_ATTEMPTS:
            try:
                locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                self._otp_persistence.lock_until(self._role, email, locked_until)
            except Exception as e:
                logger.warning("password_reset.lockout_failed role=%s error=%s", self._role, e)

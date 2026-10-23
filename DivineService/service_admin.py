import os
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
import jwt
import requests
from dotenv import load_dotenv
from Divinepersistence import persistenceAdmin, persistencePasswordReset
from DivineDTO.models import AdminCreateDTO, AdminLoginDTO
from DivineService.service_email import serviceEmail

load_dotenv()

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 30
DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS = 7
DEFAULT_PROFILE_PHOTO_BUCKET = "admin-profile-photos"
MAX_PROFILE_PHOTO_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png"}

# Reuses the existing password-reset OTP table (role, email) under its own role
# value, so signup verification never shares state with an actual password
# reset for the same email.
SIGNUP_OTP_ROLE = "admin_signup"
SIGNUP_OTP_LENGTH = 6
SIGNUP_OTP_EXPIRY_MINUTES = 10
SIGNUP_OTP_RESEND_COOLDOWN_SECONDS = 30
SIGNUP_OTP_MAX_ATTEMPTS = 5
SIGNUP_OTP_LOCKOUT_MINUTES = 15

# After the email OTP is confirmed, a new admin stays 'pending' until a super
# admin approves it. Super admins are identified by email AND employee id (both
# must match the stored row), configurable as "email:EMPLOYEE_ID,..." via
# ADMIN_SUPER_ADMINS. A super admin is auto-approved on verifying their email,
# since there is nobody above them to approve it.
DEFAULT_SUPER_ADMINS = "himanshu@divinevisioninfra.com:DV0001,ea@divinevisioninfra.com:DV0011"
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_STATUSES = (APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED)


def _parse_super_admins(raw: str) -> dict:
    """"a@x.com:DV0001, b@x.com:DV0011" -> {"a@x.com": "DV0001", "b@x.com": "DV0011"}.
    Entries without an employee id are ignored - email alone is not enough."""
    result = {}
    try:
        for entry in (raw or "").split(","):
            email, sep, employee_id = entry.strip().partition(":")
            email, employee_id = email.strip().lower(), employee_id.strip().upper()
            if sep and email and employee_id:
                result[email] = employee_id
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("admin_approval.super_admin_config_invalid error=%s", e)
    return result


def _as_aware(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("admin_signup.timestamp_normalize_failed error=%s", e)
        return None


def _approval_item(row) -> dict:
    return {
        "id": row.id,
        "full_name": row.full_name,
        "employee_id": row.employee_id,
        "email": row.email,
        "approval_status": getattr(row, "approval_status", None),
        "approval_decided_by": getattr(row, "approval_decided_by", None),
        "approval_decided_date": getattr(row, "approval_decided_date", None),
        "approval_note": getattr(row, "approval_note", None),
        "created_date": getattr(row, "created_date", None),
    }


class serviceAdmin:
    def __init__(self, persistence: persistenceAdmin = None, secret_key: str = None,
                 otp_persistence: persistencePasswordReset = None, email: serviceEmail = None,
                 super_admins: str = None):
        self._persistence = persistence or persistenceAdmin()
        self._super_admins = _parse_super_admins(
            super_admins if super_admins is not None else os.getenv("ADMIN_SUPER_ADMINS", DEFAULT_SUPER_ADMINS)
        )
        self._otp_persistence = otp_persistence or persistencePasswordReset()
        try:
            self._email = email or serviceEmail()
        except Exception as e:
            logger.warning("admin_signup.email_service_init_failed error=%s", e)
            self._email = None
        self._supabase_url = os.getenv("SUPABASE_URL")
        self._service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        self._profile_photo_bucket = os.getenv("SUPABASE_ADMIN_PROFILE_PHOTO_BUCKET", DEFAULT_PROFILE_PHOTO_BUCKET)
        # Deliberately its own secret, not the customer/broker JWT_SECRET_KEY - keeps a
        # leaked admin secret from forging customer/broker tokens, and vice versa.
        self._secret = secret_key or os.getenv("ADMIN_JWT_SECRET_KEY")
        if not self._secret:
            raise RuntimeError("ADMIN_JWT_SECRET_KEY environment variable must be set")
        try:
            self._access_expire_minutes = int(os.getenv("ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES", DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES))
        except (TypeError, ValueError):
            self._access_expire_minutes = DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES
        try:
            self._refresh_expire_days = int(os.getenv("ADMIN_REFRESH_TOKEN_EXPIRE_DAYS", DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS))
        except (TypeError, ValueError):
            self._refresh_expire_days = DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS

    def _hash_password(self, raw: str) -> str:
        return pwd_context.hash(raw)

    def _verify_password(self, plain: str, hashed: str) -> bool:
        try:
            return pwd_context.verify(plain, hashed or "")
        except Exception as e:
            logger.warning("admin_password_verify_failed error=%s", e)
            return False

    def signup(self, dto: AdminCreateDTO, created_by: str = None):
        """Creates an admin account and emails a signup OTP to the divinevisioninfra.com
        address given (DTO validation already rejects any other domain). The account
        cannot log in until verify_signup() confirms the OTP. Raises
        ValueError("employee_id_taken") / ValueError("email_taken") on a duplicate, or
        ValueError("otp_email_failed") if the verification email could not be sent -
        the API layer maps all three to an error response."""
        if self._persistence.get_by_employee_id(dto.employee_id):
            raise ValueError("employee_id_taken")
        if self._persistence.get_by_email(dto.email):
            raise ValueError("email_taken")
        hashed = self._hash_password(dto.password)
        user = self._persistence.create_user(
            dto.full_name, dto.employee_id, dto.email, hashed, created_by=created_by,
        )
        self._send_signup_otp(user.email, user.full_name)
        return user

    def resend_signup_otp(self, email: str) -> None:
        """Re-sends the signup OTP for an existing, not-yet-verified account. Raises
        ValueError("not_found") if there is no such pending account, or
        ValueError("already_verified") if it was already confirmed."""
        email = (email or "").strip().lower()
        user = self._persistence.get_by_email(email)
        if not user:
            raise ValueError("not_found")
        if getattr(user, "email_verified", False):
            raise ValueError("already_verified")
        self._send_signup_otp(user.email, user.full_name)

    def verify_signup(self, email: str, otp: str) -> str:
        """Verifies the signup OTP. A super admin is approved straight away and
        "approved" is returned; anyone else is left pending, the super admins are
        emailed, and "pending_approval" is returned. Raises ValueError with one of:
        otp_not_requested / too_many_attempts / otp_expired / invalid_otp /
        not_found - the API layer maps these to 400/429 responses."""
        email = (email or "").strip().lower()
        row = self._otp_persistence.get_by_role_email(SIGNUP_OTP_ROLE, email)
        if row is None:
            raise ValueError("otp_not_requested")

        now = datetime.now(timezone.utc)
        locked_until = _as_aware(getattr(row, "locked_until", None))
        if locked_until and now < locked_until:
            raise ValueError("too_many_attempts")

        expires_at = _as_aware(getattr(row, "expires_at", None))
        if not expires_at or now > expires_at:
            raise ValueError("otp_expired")

        if not self._verify_otp(otp, getattr(row, "otp_hash", None)):
            self._register_signup_otp_failure(email)
            raise ValueError("invalid_otp")

        user = self._persistence.get_by_email(email)
        if not user:
            raise ValueError("not_found")

        self._persistence.mark_email_verified(email)
        try:
            self._otp_persistence.delete(SIGNUP_OTP_ROLE, email)
        except Exception as e:
            logger.warning("admin_signup.otp_cleanup_failed error=%s", e)

        try:
            if self.is_super_admin(user):
                # login() treats a configured super admin as approved regardless, so
                # a failed write here is logged rather than failing the verification.
                try:
                    self._persistence.set_approval_status(user.id, APPROVAL_APPROVED, decided_by="system:super_admin")
                except Exception:
                    logger.exception("admin_approval.super_admin_auto_approve_failed admin_id=%s", user.id)
                return APPROVAL_APPROVED
            if getattr(user, "approval_status", None) == APPROVAL_APPROVED:
                return APPROVAL_APPROVED
            self._notify_super_admins(user)
            return "pending_approval"
        except Exception:
            logger.exception("admin_approval.post_verify_failed email=%s", email)
            return "pending_approval"

    def login(self, dto: AdminLoginDTO) -> dict:
        """Returns {"access_token", "refresh_token", "expires_in"} on success, else
        raises ValueError with one of: invalid_credentials / email_not_verified /
        approval_pending / approval_rejected, or RuntimeError("login_failed") on an
        unexpected error."""
        try:
            email = (dto.email or "").strip().lower()
            user = self._persistence.get_by_email(email)
            if not user:
                raise ValueError("invalid_credentials")
            if not self._verify_password(dto.password, user.password_hash):
                raise ValueError("invalid_credentials")
            if not getattr(user, "email_verified", False):
                raise ValueError("email_not_verified")
            self._require_approved(user)
            return self._issue_tokens(user.id, user.email)
        except ValueError:
            raise
        except Exception:
            logger.exception("admin_login_unexpected_error")
            raise RuntimeError("login_failed")

    # ------------------------------------------------------- super admin approval

    def is_super_admin(self, user) -> bool:
        try:
            if not user:
                return False
            email = (getattr(user, "email", None) or "").strip().lower()
            employee_id = (getattr(user, "employee_id", None) or "").strip().upper()
            return bool(email) and self._super_admins.get(email) == employee_id
        except Exception:
            # Fail closed: an unreadable row is never treated as a super admin.
            logger.exception("admin_approval.super_admin_check_failed")
            return False

    def get_active_admin(self, admin_id: str):
        """The admin row when that admin may act right now under the same rule
        login enforces (_require_approved: super admin, or approved), else None.
        For write endpoints that must not trust a still-unexpired token minted
        before a rejection. Lookup errors propagate - callers fail closed."""
        user = self._persistence.get_by_id((admin_id or "").strip())
        if not user:
            return None
        try:
            self._require_approved(user)
        except ValueError:
            return None
        return user

    def _require_approved(self, user) -> None:
        if self.is_super_admin(user):
            return
        status = getattr(user, "approval_status", None)
        if status == APPROVAL_APPROVED:
            return
        if status == APPROVAL_REJECTED:
            raise ValueError("approval_rejected")
        raise ValueError("approval_pending")

    def _require_super_admin(self, admin_id: str):
        user = self._persistence.get_by_id((admin_id or "").strip())
        if not self.is_super_admin(user):
            raise PermissionError("super_admin_required")
        return user

    def list_approval_requests(self, admin_id: str, status: str = APPROVAL_PENDING) -> list:
        """Email-verified admin accounts in the given approval status, newest first.
        Super admins only - raises PermissionError("super_admin_required"),
        ValueError("invalid_status"), or RuntimeError("approval_list_failed")."""
        try:
            self._require_super_admin(admin_id)
            if status not in APPROVAL_STATUSES:
                raise ValueError("invalid_status")
            return [
                _approval_item(row)
                for row in self._persistence.list_by_approval_status(status)
                if not self.is_super_admin(row)
            ]
        except (PermissionError, ValueError):
            raise
        except Exception:
            logger.exception("admin_approval.list_failed admin_id=%s status=%s", admin_id, status)
            raise RuntimeError("approval_list_failed")

    def decide_approval(self, admin_id: str, target_id: str, decision: str, note: str = None) -> dict:
        """Approves or rejects a pending admin account. Raises
        PermissionError("super_admin_required"), ValueError with one of:
        invalid_decision / not_found / email_not_verified / already_decided, or
        RuntimeError("approval_decision_failed") on an unexpected error."""
        try:
            approver = self._require_super_admin(admin_id)
            if decision not in (APPROVAL_APPROVED, APPROVAL_REJECTED):
                raise ValueError("invalid_decision")
            target = self._persistence.get_by_id((target_id or "").strip())
            if not target or self.is_super_admin(target):
                raise ValueError("not_found")
            if not getattr(target, "email_verified", False):
                raise ValueError("email_not_verified")
            clean_note = (note or "").strip() or None
            updated = self._persistence.set_approval_status(
                target.id, decision, decided_by=f"admin:{approver.id}", note=clean_note,
            )
            if not updated:
                raise ValueError("already_decided")
            result = _approval_item(updated)
        except (PermissionError, ValueError):
            raise
        except Exception:
            logger.exception("admin_approval.decide_failed target=%s decision=%s", target_id, decision)
            raise RuntimeError("approval_decision_failed")

        # The decision is committed from here on; the email is best effort.
        try:
            if self._email and getattr(self._email, "enabled", False):
                self._email.send_admin_approval_decision_async(
                    updated.email, decision=decision,
                    first_name=(updated.full_name or "").split(" ")[0] or None, note=clean_note,
                )
            logger.info("admin_approval.decided target=%s decision=%s by=%s", target.id, decision, approver.id)
        except Exception as e:
            logger.warning("admin_approval.decision_email_failed admin_id=%s error=%s", target.id, e)
        return result

    def _notify_super_admins(self, applicant) -> None:
        """Best effort: a failed notification must not fail the OTP verification -
        the request still shows up in GET /admin/approvals."""
        try:
            if not (self._email and getattr(self._email, "enabled", False)):
                return
        except Exception:
            logger.exception("admin_approval.notify_setup_failed")
            return
        for email in self._super_admins:
            try:
                approver = self._persistence.get_by_email(email)
                approver_name = (getattr(approver, "full_name", None) or "").split(" ")[0] or None
                self._email.send_admin_approval_request_async(
                    email, approver_name=approver_name, applicant_name=applicant.full_name,
                    applicant_email=applicant.email, applicant_employee_id=applicant.employee_id,
                )
            except Exception as e:
                logger.warning("admin_approval.notify_failed to=%s error=%s", email, e)

    # ------------------------------------------------------------ signup OTP

    def _generate_otp(self) -> str:
        return f"{secrets.randbelow(10 ** SIGNUP_OTP_LENGTH):0{SIGNUP_OTP_LENGTH}d}"

    def _hash_otp(self, otp: str) -> str:
        return pwd_context.hash(otp)

    def _verify_otp(self, otp: str, otp_hash: str) -> bool:
        try:
            return pwd_context.verify(otp or "", otp_hash or "")
        except Exception as e:
            logger.warning("admin_signup.otp_verify_failed error=%s", e)
            return False

    def _send_signup_otp(self, email: str, full_name: str = None) -> None:
        now = datetime.now(timezone.utc)
        existing = self._otp_persistence.get_by_role_email(SIGNUP_OTP_ROLE, email)
        if existing is not None:
            last_sent = _as_aware(getattr(existing, "last_sent_date", None))
            if last_sent and (now - last_sent).total_seconds() < SIGNUP_OTP_RESEND_COOLDOWN_SECONDS:
                raise ValueError("too_many_requests")

        otp = self._generate_otp()
        otp_hash = self._hash_otp(otp)
        expires_at = now + timedelta(minutes=SIGNUP_OTP_EXPIRY_MINUTES)
        self._otp_persistence.upsert_otp(SIGNUP_OTP_ROLE, email, otp_hash, expires_at, now)

        sent = False
        try:
            if self._email and getattr(self._email, "enabled", False):
                first_name = (full_name or "").split(" ")[0] if full_name else None
                sent = bool(self._email.send_otp_email(
                    email, otp, first_name=first_name, expires_minutes=SIGNUP_OTP_EXPIRY_MINUTES,
                ))
        except Exception as e:
            logger.warning("admin_signup.otp_send_exception error=%s", e)
            sent = False
        if not sent:
            raise ValueError("otp_email_failed")

    def _register_signup_otp_failure(self, email: str) -> None:
        try:
            updated = self._otp_persistence.increment_attempts(SIGNUP_OTP_ROLE, email)
        except Exception as e:
            logger.warning("admin_signup.attempt_increment_failed error=%s", e)
            return
        attempts = getattr(updated, "attempts", None) if updated else None
        if isinstance(attempts, int) and attempts >= SIGNUP_OTP_MAX_ATTEMPTS:
            try:
                locked_until = datetime.now(timezone.utc) + timedelta(minutes=SIGNUP_OTP_LOCKOUT_MINUTES)
                self._otp_persistence.lock_until(SIGNUP_OTP_ROLE, email, locked_until)
            except Exception as e:
                logger.warning("admin_signup.lockout_failed error=%s", e)

    def profile(self, admin_id: str) -> dict:
        """Returns the current admin's profile fields for the admin panel.

        Raises ValueError("not_found") if the token points at an admin account
        that no longer exists.
        """
        try:
            clean_id = (admin_id or "").strip()
            if not clean_id:
                raise ValueError("not_found")
            user = self._persistence.get_profile_by_id(clean_id)
            if not user:
                raise ValueError("not_found")

            full_name = (getattr(user, "full_name", None) or "").strip()
            email = (getattr(user, "email", None) or "").strip()
            source = full_name or email
            parts = [p for p in source.replace(".", " ").replace("_", " ").split() if p]
            if len(parts) >= 2:
                initials = f"{parts[0][0]}{parts[-1][0]}".upper()
            elif parts:
                initials = parts[0][0].upper()
            else:
                initials = "A"

            return {
                "id": user.id,
                "full_name": full_name,
                "employee_id": user.employee_id,
                "email": email,
                "phone": None,
                "avatar_url": getattr(user, "profile_photo_url", None),
                "initials": initials,
                "is_super_admin": self.is_super_admin(user),
                "approval_status": getattr(user, "approval_status", None),
                "created_by": getattr(user, "created_by", None),
                "created_date": getattr(user, "created_date", None),
                "last_updated_by": getattr(user, "last_updated_by", None),
                "last_updated_date": getattr(user, "last_updated_date", None),
            }
        except ValueError:
            raise
        except Exception:
            logger.exception("admin_profile_lookup_failed admin_id=%s", admin_id)
            raise RuntimeError("profile_lookup_failed")

    def upload_profile_photo(self, admin_id: str, file_bytes: bytes, content_type: str) -> dict:
        """Uploads the current admin's profile photo to Supabase Storage and
        stores the public object URL on divine_admin_users.profile_photo_url."""
        object_path = None
        committed = False
        try:
            clean_id = (admin_id or "").strip()
            if not clean_id:
                raise ValueError("not_found")
            existing = self._persistence.get_by_id(clean_id)
            if not existing:
                raise ValueError("not_found")
            clean_type = (content_type or "").split(";")[0].strip().lower()
            ext = PROFILE_PHOTO_CONTENT_TYPES.get(clean_type)
            if not ext:
                raise ValueError("unsupported_file_type")
            if not file_bytes:
                raise ValueError("empty_file")
            if len(file_bytes) > MAX_PROFILE_PHOTO_BYTES:
                raise ValueError("file_too_large")
            if clean_type == "image/png" and not file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("unsupported_file_type")
            if clean_type == "image/jpeg" and not file_bytes.startswith(b"\xff\xd8"):
                raise ValueError("unsupported_file_type")

            object_path = f"{clean_id}/profile_{uuid.uuid4().hex}.{ext}"
            self._upload_to_storage(object_path, file_bytes, clean_type)
            photo_url = self._public_storage_url(object_path)
            updated = self._persistence.update_profile_photo(
                id=clean_id,
                profile_photo_url=photo_url,
                profile_photo_path=object_path,
                profile_photo_bucket=self._profile_photo_bucket,
                updated_by=clean_id,
            )
            if not updated:
                self._delete_from_storage(object_path)
                raise ValueError("not_found")
            committed = True

            old_path = getattr(existing, "profile_photo_path", None)
            old_bucket = getattr(existing, "profile_photo_bucket", None)
            if old_path and old_path != object_path:
                self._delete_from_storage(old_path, bucket=old_bucket)
            return self.profile(clean_id)
        except ValueError:
            raise
        except RuntimeError:
            if object_path and not committed:
                self._delete_from_storage(object_path)
            raise
        except Exception:
            if object_path and not committed:
                self._delete_from_storage(object_path)
            logger.exception("admin_profile_photo_upload_failed admin_id=%s", admin_id)
            raise RuntimeError("profile_photo_upload_failed")

    def _upload_to_storage(self, object_path: str, file_bytes: bytes, content_type: str) -> None:
        try:
            if not self._supabase_url or not self._service_key:
                raise RuntimeError("storage_not_configured")
            upload_url = f"{self._supabase_url}/storage/v1/object/{self._profile_photo_bucket}/{object_path}"
            resp = requests.post(
                upload_url,
                headers={
                    "apikey": self._service_key,
                    "Authorization": f"Bearer {self._service_key}",
                    "Content-Type": content_type,
                    "x-upsert": "false",
                },
                data=file_bytes,
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "admin_profile_photo_storage_upload_rejected status=%s bucket=%s path=%s body=%s",
                    resp.status_code, self._profile_photo_bucket, object_path, (resp.text or "")[:500],
                )
                raise RuntimeError(f"storage_upload_failed:{resp.status_code}")
        except RuntimeError:
            raise
        except requests.exceptions.RequestException:
            raise RuntimeError("storage_unreachable")
        except Exception:
            logger.exception("admin_profile_photo_storage_upload_failed path=%s", object_path)
            raise RuntimeError("storage_upload_failed")

    def _delete_from_storage(self, object_path: str, bucket: str = None) -> None:
        try:
            if not self._supabase_url or not self._service_key or not object_path:
                return
            delete_url = f"{self._supabase_url}/storage/v1/object/{bucket or self._profile_photo_bucket}/{object_path}"
            requests.delete(
                delete_url,
                headers={"apikey": self._service_key, "Authorization": f"Bearer {self._service_key}"},
                timeout=30,
            )
        except Exception:
            logger.warning("admin_profile_photo_storage_cleanup_failed path=%s", object_path, exc_info=True)

    def _public_storage_url(self, object_path: str) -> str:
        try:
            if not self._supabase_url:
                raise RuntimeError("storage_not_configured")
            return f"{self._supabase_url}/storage/v1/object/public/{self._profile_photo_bucket}/{object_path}"
        except RuntimeError:
            raise
        except Exception:
            logger.exception("admin_profile_photo_public_url_failed path=%s", object_path)
            raise RuntimeError("storage_url_failed")


    def refresh(self, refresh_token: str) -> dict:
        """Verifies a refresh token (type=refresh, role=admin) and mints a fresh
        access token. Raises ValueError("invalid_refresh_token") on any failure -
        expired/malformed/wrong-type token, or an admin id that no longer exists."""
        try:
            payload = jwt.decode(refresh_token, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError as e:
            logger.info("admin_refresh_decode_failed error=%s", e)
            raise ValueError("invalid_refresh_token")

        if payload.get("type") != "refresh" or payload.get("role") != "admin":
            raise ValueError("invalid_refresh_token")

        admin_id = payload.get("sub")
        if not admin_id:
            raise ValueError("invalid_refresh_token")

        try:
            user = self._persistence.get_by_id(admin_id)
            if not user:
                raise ValueError("invalid_refresh_token")
            self._require_approved(user)
            access_token, expires_in = self._access_token(user.id, user.email)
            return {"access_token": access_token, "expires_in": expires_in}
        except ValueError:
            raise ValueError("invalid_refresh_token")
        except Exception:
            logger.exception("admin_refresh_unexpected_error admin_id=%s", admin_id)
            raise RuntimeError("refresh_failed")

    def _issue_tokens(self, admin_id: str, email: str) -> dict:
        access_token, expires_in = self._access_token(admin_id, email)
        refresh_token = self._refresh_token(admin_id)
        return {"access_token": access_token, "refresh_token": refresh_token, "expires_in": expires_in}

    def _access_token(self, admin_id: str, email: str):
        expires_in = self._access_expire_minutes * 60
        payload = {
            "sub": admin_id,
            "email": email,
            "role": "admin",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=self._access_expire_minutes),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256"), expires_in

    def _refresh_token(self, admin_id: str) -> str:
        payload = {
            "sub": admin_id,
            "role": "admin",
            "type": "refresh",
            "exp": datetime.now(timezone.utc) + timedelta(days=self._refresh_expire_days),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

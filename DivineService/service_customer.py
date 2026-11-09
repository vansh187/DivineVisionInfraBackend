import os
import re
import logging
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
import jwt
from dotenv import load_dotenv
from Divinepersistence import persistenceCustomer
from DivineDTO.models import UserCreateDTO, UserLoginDTO
from DivineService.service_zoho import serviceZoho
from DivineService.service_email import serviceEmail, dispatch_welcome_email, CUSTOMER

load_dotenv()

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class serviceCustomer:
    def __init__(self, persistence: persistenceCustomer = None, secret_key: str = None, zoho: serviceZoho = None, email: serviceEmail = None):
        self._persistence = persistence or persistenceCustomer()
        self._secret = secret_key or os.getenv("JWT_SECRET_KEY")
        if not self._secret:
            raise RuntimeError("JWT_SECRET_KEY environment variable must be set")
        try:
            self._zoho = zoho or serviceZoho()
        except Exception as e:
            logger.warning("zoho_service_init_failed: %s", e)
            self._zoho = None
        try:
            self._email = email or serviceEmail()
        except Exception as e:
            logger.warning("email_service_init_failed: %s", e)
            self._email = None

    def _hash_password(self, raw: str) -> str:
        return pwd_context.hash(raw)

    def _verify_password(self, plain: str, hashed: str) -> bool:
        return pwd_context.verify(plain, hashed)

    def signup(self, dto: UserCreateDTO, created_by: str = None):
        existing = self._persistence.get_by_username(dto.username)
        if existing:
            raise ValueError("username_taken")
        hashed = self._hash_password(dto.password)
        user = self._persistence.create_user(
            dto.username,
            hashed,
            created_by,
            email=dto.email,
            phone=dto.phone,
            first_name=getattr(dto, 'first_name', None),
            last_name=getattr(dto, 'last_name', None),
        )
        try:
            if self._zoho:
                self._zoho.push_customer_signup_async(
                    customer_id=user.id, username=dto.username,
                    first_name=getattr(dto, 'first_name', None), last_name=getattr(dto, 'last_name', None),
                    email=dto.email, phone=dto.phone,
                )
        except Exception as e:
            # Best-effort CRM sync - must never fail or roll back a successful signup.
            logger.warning("zoho_customer_sync_failed customer_id=%s error=%s", user.id, e)
        # Best-effort welcome email - dispatched on a daemon thread so it never
        # fails, slows, or rolls back a successful signup.
        dispatch_welcome_email(
            self._email, CUSTOMER, dto.email,
            first_name=getattr(dto, 'first_name', None), username=dto.username,
        )
        return user

    def login(self, dto: UserLoginDTO) -> str:
        return self.login_with_status(dto)[0]

    def login_with_status(self, dto: UserLoginDTO):
        """(token, must_change_password). Raises ValueError('invalid_credentials')
        or ValueError('temp_password_expired') - an emailed temporary password
        (offline-buyer invite) past its expiry can no longer be used; the
        customer sets a new one through Forgot password."""
        username = (dto.username or "").strip().replace("\\@", "@")
        if EMAIL_RE.fullmatch(username):
            user = self._authenticate_email(username, dto.password)
        else:
            user = self._persistence.get_by_username(username)
            if not user or not self._verify_password(dto.password, user.password_hash):
                raise ValueError("invalid_credentials")
        return self._token_and_status(user)

    def login_by_email(self, email: str, password: str) -> str:
        return self._token_and_status(self._authenticate_email(email, password))[0]

    def _authenticate_email(self, email: str, password: str):
        if not email or not password:
            raise ValueError("invalid_credentials")
        user = self._persistence.get_by_email((email or "").strip())
        if not user or not self._verify_password(password, user.password_hash):
            raise ValueError("invalid_credentials")
        return user

    def _token_and_status(self, user):
        must_change = getattr(user, "must_change_password", False) is True or \
            str(getattr(user, "must_change_password", "")).lower() in ("1", "true")
        if must_change:
            expires = getattr(user, "temp_password_expires_at", None)
            if isinstance(expires, str):
                try:
                    expires = datetime.fromisoformat(expires)
                except ValueError:
                    expires = None
            if isinstance(expires, datetime):
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    raise ValueError("temp_password_expired")
        return self._token_for_user(user, must_change_password=must_change), must_change

    def change_password(self, customer_id: str, current_password: str, new_password: str) -> str:
        """Logged-in password change (also how an offline buyer replaces the
        emailed temporary password). Clears must_change_password and returns a
        fresh token without the password-change restriction. Raises
        ValueError('invalid_credentials' | 'password_too_short' | 'password_unchanged')."""
        user = self._persistence.get_by_id(customer_id)
        if not user or not self._verify_password(current_password or "", user.password_hash):
            raise ValueError("invalid_credentials")
        if len(new_password or "") < 8:
            raise ValueError("password_too_short")
        if self._verify_password(new_password, user.password_hash):
            raise ValueError("password_unchanged")
        updated = self._persistence.update_password(user.id, self._hash_password(new_password))
        return self._token_for_user(updated or user, must_change_password=False)

    def _token_for_user(self, user, must_change_password: bool = False) -> str:
        payload = {
            "sub": user.id,
            "username": user.username,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
            "role": "customer",
        }
        if must_change_password:
            # DivineService.auth.get_current_user refuses this token everywhere
            # except change-password / profile until the password is changed.
            payload["pwd_change"] = True
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token

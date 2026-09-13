import os
import logging
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
import jwt
from dotenv import load_dotenv
from Divinepersistence import persistenceAdmin
from DivineDTO.models import AdminCreateDTO, AdminLoginDTO

load_dotenv()

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 30
DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS = 7


class serviceAdmin:
    def __init__(self, persistence: persistenceAdmin = None, secret_key: str = None):
        self._persistence = persistence or persistenceAdmin()
        self._secret = secret_key or os.getenv("JWT_SECRET_KEY")
        if not self._secret:
            raise RuntimeError("JWT_SECRET_KEY environment variable must be set")
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
        """Creates an admin account, active immediately (no approval gate). Raises
        ValueError("employee_id_taken") / ValueError("email_taken") on a duplicate -
        the API layer maps both to 409."""
        if self._persistence.get_by_employee_id(dto.employee_id):
            raise ValueError("employee_id_taken")
        if self._persistence.get_by_email(dto.email):
            raise ValueError("email_taken")
        hashed = self._hash_password(dto.password)
        user = self._persistence.create_user(
            dto.full_name, dto.employee_id, dto.email, hashed, created_by=created_by,
        )
        return user

    def login(self, dto: AdminLoginDTO) -> dict:
        """Returns {"access_token", "refresh_token", "expires_in"} on success, else
        raises ValueError("invalid_credentials")."""
        email = (dto.email or "").strip().lower()
        user = self._persistence.get_by_email(email)
        if not user:
            raise ValueError("invalid_credentials")
        if not self._verify_password(dto.password, user.password_hash):
            raise ValueError("invalid_credentials")
        return self._issue_tokens(user.id, user.email)

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

        user = self._persistence.get_by_id(admin_id)
        if not user:
            raise ValueError("invalid_refresh_token")

        access_token, expires_in = self._access_token(user.id, user.email)
        return {"access_token": access_token, "expires_in": expires_in}

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

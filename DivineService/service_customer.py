import os
import re
import logging
from datetime import datetime, timedelta
from passlib.context import CryptContext
import jwt
from dotenv import load_dotenv
from Divinepersistence import persistenceCustomer
from DivineDTO.models import UserCreateDTO, UserLoginDTO
from DivineService.service_zoho import serviceZoho

load_dotenv()

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class serviceCustomer:
    def __init__(self, persistence: persistenceCustomer = None, secret_key: str = None, zoho: serviceZoho = None):
        self._persistence = persistence or persistenceCustomer()
        self._secret = secret_key or os.getenv("JWT_SECRET_KEY")
        if not self._secret:
            raise RuntimeError("JWT_SECRET_KEY environment variable must be set")
        try:
            self._zoho = zoho or serviceZoho()
        except Exception as e:
            logger.warning("zoho_service_init_failed: %s", e)
            self._zoho = None

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
        return user

    def login(self, dto: UserLoginDTO) -> str:
        username = (dto.username or "").strip().replace("\\@", "@")
        if EMAIL_RE.fullmatch(username):
            return self.login_by_email(username, dto.password)
        user = self._persistence.get_by_username(username)
        if not user:
            raise ValueError("invalid_credentials")
        if not self._verify_password(dto.password, user.password_hash):
            raise ValueError("invalid_credentials")
        return self._token_for_user(user)

    def login_by_email(self, email: str, password: str) -> str:
        if not email or not password:
            raise ValueError("invalid_credentials")
        email = (email or "").strip()
        user = self._persistence.get_by_email(email)
        if not user:
            raise ValueError("invalid_credentials")
        if not self._verify_password(password, user.password_hash):
            raise ValueError("invalid_credentials")
        return self._token_for_user(user)

    def _token_for_user(self, user) -> str:
        from datetime import timezone
        payload = {
            "sub": user.id,
            "username": user.username,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
            "role": "customer",
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token

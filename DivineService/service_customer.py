import os
from datetime import datetime, timedelta
from passlib.context import CryptContext
import jwt
from dotenv import load_dotenv
from Divinepersistence import persistenceCustomer
from DivineDTO.models import UserCreateDTO, UserLoginDTO

load_dotenv()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class serviceCustomer:
    def __init__(self, persistence: persistenceCustomer, secret_key: str = None):
        self._persistence = persistence
        self._secret = secret_key or os.getenv("JWT_SECRET_KEY")
        if not self._secret:
            raise RuntimeError("JWT_SECRET_KEY environment variable must be set")

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
        return user

    def login(self, dto: UserLoginDTO) -> str:
        user = self._persistence.get_by_username(dto.username)
        if not user:
            raise ValueError("invalid_credentials")
        if not self._verify_password(dto.password, user.password_hash):
            raise ValueError("invalid_credentials")
        from datetime import timezone
        payload = {
            "sub": user.id,
            "username": user.username,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
            "role": "customer",
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token

import uuid
from sqlalchemy import Column, String, DateTime, Integer, UniqueConstraint, text
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class PasswordResetOtpModel(Base):
    __tablename__ = "divine_password_reset_otp"
    id = Column(String(36), primary_key=True)
    role = Column(String(10), nullable=False)
    email = Column(String(255), nullable=False)
    otp_hash = Column(String(255), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime)
    expires_at = Column(DateTime, nullable=False)
    last_sent_date = Column(DateTime, nullable=False)
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)
    __table_args__ = (UniqueConstraint("role", "email", name="uq_password_reset_role_email"),)


class persistencePasswordReset:
    """One row per (role, email) - a new /forgot-password request replaces the
    previous row outright (fresh OTP, attempts reset to 0, any lockout cleared),
    so only the most recently sent code is ever valid."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("password_reset_queries.yaml")
        queries.setdefault("upsert_otp", (
            'INSERT INTO divine_password_reset_otp(id, role, email, otp_hash, attempts, locked_until, expires_at, last_sent_date, created_date, last_updated_date) '
            'VALUES (:id, :role, :email, :otp_hash, 0, NULL, :expires_at, :last_sent_date, :created_date, :last_updated_date) '
            'ON CONFLICT (role, email) DO UPDATE SET '
            'otp_hash = EXCLUDED.otp_hash, attempts = 0, locked_until = NULL, '
            'expires_at = EXCLUDED.expires_at, last_sent_date = EXCLUDED.last_sent_date, '
            'last_updated_date = EXCLUDED.last_updated_date '
            'RETURNING *;'
        ))
        queries.setdefault("get_by_role_email", (
            'SELECT * FROM divine_password_reset_otp WHERE role = :role AND lower(email) = lower(:email) LIMIT 1;'
        ))
        queries.setdefault("increment_attempts", (
            'UPDATE divine_password_reset_otp SET attempts = attempts + 1, last_updated_date = :now '
            'WHERE role = :role AND lower(email) = lower(:email) RETURNING *;'
        ))
        queries.setdefault("lock_until", (
            'UPDATE divine_password_reset_otp SET locked_until = :locked_until, last_updated_date = :now '
            'WHERE role = :role AND lower(email) = lower(:email) RETURNING *;'
        ))
        queries.setdefault("delete", (
            'DELETE FROM divine_password_reset_otp WHERE role = :role AND lower(email) = lower(:email);'
        ))
        self._queries = queries
        self._engine = engine

    def upsert_otp(self, role: str, email: str, otp_hash: str, expires_at, last_sent_date):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {
                    "id": str(uuid.uuid4()),
                    "role": role,
                    "email": email,
                    "otp_hash": otp_hash,
                    "expires_at": expires_at,
                    "last_sent_date": last_sent_date,
                    "created_date": now,
                    "last_updated_date": now,
                }
                result = db.execute(text(self._queries["upsert_otp"]), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def get_by_role_email(self, role: str, email: str):
        with self._session_factory() as db:
            result = db.execute(text(self._queries["get_by_role_email"]), {"role": role, "email": email})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def increment_attempts(self, role: str, email: str):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._queries["increment_attempts"]), {
                    "role": role, "email": email, "now": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def lock_until(self, role: str, email: str, locked_until):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._queries["lock_until"]), {
                    "role": role, "email": email, "locked_until": locked_until,
                    "now": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def delete(self, role: str, email: str) -> None:
        with self._session_factory() as db:
            try:
                db.execute(text(self._queries["delete"]), {"role": role, "email": email})
                db.commit()
            except Exception:
                db.rollback()
                raise

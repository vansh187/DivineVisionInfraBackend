import random
from sqlalchemy import Column, String, DateTime, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class AdminModel(Base):
    __tablename__ = "divine_admin_users"
    id = Column(String(6), primary_key=True)
    full_name = Column(String(150), nullable=False)
    employee_id = Column(String(32), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    profile_photo_url = Column(String(1000))
    profile_photo_path = Column(String(500))
    profile_photo_bucket = Column(String(100))
    created_by = Column(String(255))
    created_date = Column(DateTime)
    last_updated_by = Column(String(255))
    last_updated_date = Column(DateTime)


class persistenceAdmin:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("admin_queries.yaml")
        queries.setdefault("create_admin", (
            'INSERT INTO divine_admin_users(id, full_name, employee_id, email, password_hash, created_by, created_date, last_updated_by, last_updated_date) '
            'VALUES (:id, :full_name, :employee_id, :email, :password_hash, :created_by, :created_date, :last_updated_by, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_email", 'SELECT * FROM divine_admin_users WHERE lower(email) = lower(:email) LIMIT 1;')
        queries.setdefault("get_by_employee_id", 'SELECT * FROM divine_admin_users WHERE lower(employee_id) = lower(:employee_id) LIMIT 1;')
        queries.setdefault("get_by_id", 'SELECT * FROM divine_admin_users WHERE id = :id LIMIT 1;')
        queries.setdefault("get_profile_by_id", (
            'SELECT id, full_name, employee_id, email, profile_photo_url, profile_photo_path, '
            'profile_photo_bucket, created_by, created_date, last_updated_by, last_updated_date '
            'FROM divine_admin_users WHERE id = :id LIMIT 1;'
        ))
        queries.setdefault("update_password", (
            'UPDATE divine_admin_users SET password_hash = :password_hash, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        queries.setdefault("update_profile_photo", (
            'UPDATE divine_admin_users SET profile_photo_url = :profile_photo_url, '
            'profile_photo_path = :profile_photo_path, profile_photo_bucket = :profile_photo_bucket, '
            'last_updated_by = :last_updated_by, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        self._queries = queries
        self._engine = engine

    def _generate_unique_id(self, db: Session) -> str:
        for _ in range(50):
            candidate = "A" + str(random.randint(0, 99999)).zfill(5)
            if not db.query(AdminModel).filter_by(id=candidate).first():
                return candidate
        raise RuntimeError("Failed to generate unique admin id")

    def create_user(self, full_name: str, employee_id: str, email: str, password_hash: str, created_by: str = None) -> AdminModel:
        with self._session_factory() as db:
            try:
                new_id = self._generate_unique_id(db)
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_admin")
                params = {
                    "id": new_id,
                    "full_name": full_name,
                    "employee_id": employee_id,
                    "email": email,
                    "password_hash": password_hash,
                    "created_by": created_by,
                    "created_date": now,
                    "last_updated_by": created_by,
                    "last_updated_date": now,
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except IntegrityError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def get_by_email(self, email: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_email")
            result = db.execute(text(query), {"email": email})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_by_employee_id(self, employee_id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_employee_id")
            result = db.execute(text(query), {"employee_id": employee_id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_id")
            result = db.execute(text(query), {"id": id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_profile_by_id(self, id: str):
        with self._session_factory() as db:
            try:
                query = self._queries.get("get_profile_by_id")
                result = db.execute(text(query), {"id": id})
                row = result.mappings().first()
                if not row:
                    return None
                return RowWrapper(row)
            except Exception:
                raise

    def update_password(self, id: str, password_hash: str):
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_password")
                result = db.execute(text(query), {
                    "id": id, "password_hash": password_hash,
                    "last_updated_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def update_profile_photo(self, id: str, profile_photo_url: str, profile_photo_path: str,
                             profile_photo_bucket: str, updated_by: str = None):
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_profile_photo")
                result = db.execute(text(query), {
                    "id": id,
                    "profile_photo_url": profile_photo_url,
                    "profile_photo_path": profile_photo_path,
                    "profile_photo_bucket": profile_photo_bucket,
                    "last_updated_by": updated_by,
                    "last_updated_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

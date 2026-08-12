import random
import os
import yaml
from sqlalchemy import Column, String, DateTime, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine

class _RowWrapper:
    def __init__(self, mapping):
        if mapping:
            self.__dict__.update(mapping)



class CustomerModel(Base):
    __tablename__ = "divine_customer_users"
    id = Column(String(6), primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255))
    phone = Column(String(50))
    first_name = Column(String(150))
    last_name = Column(String(150))
    password_hash = Column(String(255), nullable=False)
    created_by = Column(String(255))
    created_date = Column(DateTime)
    last_updated_by = Column(String(255))
    last_updated_date = Column(DateTime)


class persistenceCustomer:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        # load queries from YAML with safe fallback
        root = os.path.dirname(os.path.dirname(__file__))
        qpath = os.path.join(root, "queries.yaml")
        queries = {}
        try:
            if os.path.exists(qpath):
                with open(qpath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                queries = data.get("customer", {})
        except Exception:
            queries = {}
        queries.setdefault("create_customer", (
            'INSERT INTO divine_customer_users(id, username, first_name, last_name, email, phone, password_hash, created_by, created_date, last_updated_by, last_updated_date) '
            'VALUES (:id, :username, :first_name, :last_name, :email, :phone, :password_hash, :created_by, :created_date, :last_updated_by, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_username", 'SELECT * FROM divine_customer_users WHERE username = :username LIMIT 1;')
        queries.setdefault("get_by_id", 'SELECT * FROM divine_customer_users WHERE id = :id LIMIT 1;')
        self._queries = queries
        self._engine = engine

    def _generate_unique_id(self, db: Session) -> str:
        for _ in range(50):
            candidate = "C" + str(random.randint(0, 99999)).zfill(5)
            if not db.query(CustomerModel).filter_by(id=candidate).first():
                return candidate
        raise RuntimeError("Failed to generate unique customer id")

    def create_user(self, username: str, password_hash: str, created_by: str = None, email: str = None, phone: str = None, first_name: str = None, last_name: str = None) -> CustomerModel:
        with self._session_factory() as db:
            try:
                new_id = self._generate_unique_id(db)
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_customer")
                params = {
                    "id": new_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "password_hash": password_hash,
                    "created_by": created_by,
                    "created_date": now,
                    "last_updated_by": created_by,
                    "last_updated_date": now,
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return _RowWrapper(row)
            except IntegrityError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def get_by_username(self, username: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_username")
            result = db.execute(text(query), {"username": username})
            row = result.mappings().first()
            if not row:
                return None
            return _RowWrapper(row)

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_id")
            result = db.execute(text(query), {"id": id})
            row = result.mappings().first()
            if not row:
                return None
            return _RowWrapper(row)

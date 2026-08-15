import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Boolean, JSON, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class KycVerificationModel(Base):
    __tablename__ = "divine_kyc_verifications"
    id = Column(String(36), primary_key=True)
    owner_id = Column(String(6), nullable=False, index=True)
    owner_role = Column(String(10), nullable=False)
    method = Column(String(20), nullable=False)
    verified = Column(Boolean, nullable=False)
    masked_aadhaar = Column(String(20), nullable=False)
    extracted_data = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    failure_reason = Column(String(100))
    created_date = Column(DateTime)


class persistenceKyc:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("kyc_queries.yaml")
        queries.setdefault("create_verification", (
            'INSERT INTO divine_kyc_verifications(id, owner_id, owner_role, method, verified, masked_aadhaar, extracted_data, failure_reason, created_date) '
            'VALUES (:id, :owner_id, :owner_role, :method, :verified, :masked_aadhaar, :extracted_data, :failure_reason, :created_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_kyc_verifications WHERE id = :id LIMIT 1;')
        self._queries = queries
        self._engine = engine

    def create_verification(self, owner_id: str, owner_role: str, method: str, verified: bool,
                             masked_aadhaar: str, extracted_data: dict, failure_reason: str = None) -> KycVerificationModel:
        with self._session_factory() as db:
            try:
                new_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_verification")
                params = {
                    "id": new_id,
                    "owner_id": owner_id,
                    "owner_role": owner_role,
                    "method": method,
                    "verified": verified,
                    "masked_aadhaar": masked_aadhaar,
                    "extracted_data": json.dumps(extracted_data),
                    "failure_reason": failure_reason,
                    "created_date": now,
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

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_id")
            result = db.execute(text(query), {"id": id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

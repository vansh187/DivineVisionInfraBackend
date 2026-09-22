import json
from sqlalchemy import Column, String, DateTime, JSON, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class DocumentModel(Base):
    __tablename__ = "divine_documents"
    id = Column(String(36), primary_key=True)
    owner_id = Column(String(6), nullable=False, index=True)
    owner_role = Column(String(10), nullable=False)
    document_type = Column(String(100), nullable=False)
    form_data = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    storage_path = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="generated")
    storage_bucket = Column(String(100), nullable=True)
    project_id = Column(String(100), nullable=True, index=True)
    payment_id = Column(String(36), nullable=True, index=True)
    # razorpay_* kept for documents created before the Zoho Payments cutover.
    razorpay_order_id = Column(String(64), nullable=True)
    razorpay_payment_id = Column(String(64), nullable=True)
    zoho_payments_session_id = Column(String(64), nullable=True)
    zoho_payment_id = Column(String(64), nullable=True)
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class persistenceDocument:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("documents_queries.yaml")
        queries.setdefault("create_document", (
            'INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, storage_path, status, storage_bucket, project_id, payment_id, zoho_payments_session_id, zoho_payment_id, created_date, last_updated_date) '
            'VALUES (:id, :owner_id, :owner_role, :document_type, :form_data, :storage_path, :status, :storage_bucket, :project_id, :payment_id, :zoho_payments_session_id, :zoho_payment_id, :created_date, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_documents WHERE id = :id LIMIT 1;')
        queries.setdefault("get_latest_by_owner_and_type", (
            'SELECT * FROM divine_documents WHERE owner_id = :owner_id AND document_type = :document_type '
            'ORDER BY created_date DESC LIMIT 1;'
        ))
        queries.setdefault("get_latest_by_payment_id", (
            'SELECT * FROM divine_documents WHERE payment_id = :payment_id '
            'ORDER BY created_date DESC LIMIT 1;'
        ))
        self._queries = queries
        self._engine = engine

    def create_document(
        self,
        id: str,
        owner_id: str,
        owner_role: str,
        document_type: str,
        form_data: dict,
        storage_path: str,
        status: str = "generated",
        storage_bucket: str = None,
        project_id: str = None,
        payment_id: str = None,
        zoho_payments_session_id: str = None,
        zoho_payment_id: str = None,
    ) -> DocumentModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_document")
                params = {
                    "id": id,
                    "owner_id": owner_id,
                    "owner_role": owner_role,
                    "document_type": document_type,
                    "form_data": json.dumps(form_data),
                    "storage_path": storage_path,
                    "status": status,
                    "storage_bucket": storage_bucket,
                    "project_id": project_id,
                    "payment_id": payment_id,
                    "zoho_payments_session_id": zoho_payments_session_id,
                    "zoho_payment_id": zoho_payment_id,
                    "created_date": now,
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

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_id")
            result = db.execute(text(query), {"id": id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_latest_by_owner_and_type(self, owner_id: str, document_type: str):
        with self._session_factory() as db:
            query = self._queries.get("get_latest_by_owner_and_type")
            result = db.execute(text(query), {"owner_id": owner_id, "document_type": document_type})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_latest_by_payment_id(self, payment_id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_latest_by_payment_id")
            result = db.execute(text(query), {"payment_id": payment_id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

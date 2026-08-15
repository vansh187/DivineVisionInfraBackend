import json
from sqlalchemy import Column, String, DateTime, Numeric, JSON, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class PaymentModel(Base):
    __tablename__ = "divine_payments"
    id = Column(String(36), primary_key=True)
    owner_id = Column(String(6), nullable=False, index=True)
    owner_role = Column(String(10), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    status = Column(String(20), nullable=False, default="created")  # created | paid | failed
    razorpay_order_id = Column(String(64), nullable=False, index=True)
    razorpay_payment_id = Column(String(64))
    razorpay_signature = Column(String(255))
    notes = Column(JSON().with_variant(JSONB, "postgresql"))
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class persistencePayment:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("payment_queries.yaml")
        queries.setdefault("create_payment", (
            'INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, razorpay_order_id, notes, created_date, last_updated_date) '
            'VALUES (:id, :owner_id, :owner_role, :amount, :currency, :status, :razorpay_order_id, :notes, :created_date, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_payments WHERE id = :id LIMIT 1;')
        queries.setdefault("get_by_razorpay_order_id", 'SELECT * FROM divine_payments WHERE razorpay_order_id = :razorpay_order_id LIMIT 1;')
        queries.setdefault("update_payment_status", (
            'UPDATE divine_payments SET status = :status, razorpay_payment_id = :razorpay_payment_id, '
            'razorpay_signature = :razorpay_signature, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        self._queries = queries
        self._engine = engine

    def create_payment(self, id: str, owner_id: str, owner_role: str, amount, currency: str, status: str, razorpay_order_id: str, notes: dict = None) -> PaymentModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_payment")
                params = {
                    "id": id,
                    "owner_id": owner_id,
                    "owner_role": owner_role,
                    "amount": amount,
                    "currency": currency,
                    "status": status,
                    "razorpay_order_id": razorpay_order_id,
                    "notes": json.dumps(notes or {}),
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

    def get_by_razorpay_order_id(self, razorpay_order_id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_razorpay_order_id")
            result = db.execute(text(query), {"razorpay_order_id": razorpay_order_id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def update_payment_status(self, id: str, status: str, razorpay_payment_id: str, razorpay_signature: str) -> PaymentModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_payment_status")
                params = {
                    "id": id,
                    "status": status,
                    "razorpay_payment_id": razorpay_payment_id,
                    "razorpay_signature": razorpay_signature,
                    "last_updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise
from sqlalchemy import Column, DateTime, Numeric, String, text
from sqlalchemy.exc import IntegrityError
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class BrokerCommissionModel(Base):
    __tablename__ = "divine_broker_commissions"
    id = Column(String(36), primary_key=True)
    broker_id = Column(String(80), nullable=False, index=True)
    serial_number = Column(String(100), nullable=False, index=True)
    unit_address = Column(String(500), nullable=False)
    customer_name = Column(String(200))
    township = Column(String(200))
    sale_value = Column(Numeric(14, 2))
    commission_amount = Column(Numeric(14, 2), nullable=False)
    status = Column(String(20), nullable=False, default="paid")
    transaction_mode = Column(String(20), nullable=False)
    razorpay_order_id = Column(String(64), index=True)
    created_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime)
    rejected_at = Column(DateTime)
    last_updated_at = Column(DateTime, nullable=False)


class persistenceBrokerCommission:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("broker_commission_queries.yaml")
        queries.setdefault("create_commission", (
            "INSERT INTO divine_broker_commissions("
            "id, broker_id, serial_number, unit_address, customer_name, township, sale_value, "
            "commission_amount, status, transaction_mode, razorpay_order_id, created_at, paid_at, rejected_at, last_updated_at"
            ") VALUES ("
            ":id, :broker_id, :serial_number, :unit_address, :customer_name, :township, :sale_value, "
            ":commission_amount, :status, :transaction_mode, :razorpay_order_id, :created_at, :paid_at, :rejected_at, :last_updated_at"
            ") RETURNING *;"
        ))
        queries.setdefault("list_by_broker", (
            "SELECT * FROM divine_broker_commissions "
            "WHERE broker_id = :broker_id ORDER BY created_at DESC, id DESC;"
        ))
        queries.setdefault("summary_by_broker", (
            "SELECT status, COALESCE(SUM(commission_amount), 0) AS total "
            "FROM divine_broker_commissions WHERE broker_id = :broker_id GROUP BY status;"
        ))
        self._queries = queries
        self._engine = engine

    def create_commission(self, **params):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._queries["create_commission"]), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except IntegrityError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def list_by_broker(self, broker_id: str):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._queries["list_by_broker"]), {"broker_id": broker_id})
                return [RowWrapper(row) for row in result.mappings().all()]
            except Exception:
                raise

    def summary_by_broker(self, broker_id: str):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._queries["summary_by_broker"]), {"broker_id": broker_id})
                return [RowWrapper(row) for row in result.mappings().all()]
            except Exception:
                raise

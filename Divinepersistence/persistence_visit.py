from sqlalchemy import Column, String, Date, Text, DateTime, text
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class VisitModel(Base):
    __tablename__ = "divine_site_visits"
    id = Column(String(36), primary_key=True)
    broker_id = Column(String(6), nullable=False, index=True)
    customer_name = Column(String(200), nullable=False)
    customer_contact = Column(String(200))
    visit_date = Column(Date, nullable=False)
    # "HH:MM", kept as text - no timezone math needed, round-trips exactly what was typed.
    visit_time = Column(String(5), nullable=False)
    notes = Column(Text)
    status = Column(String(20), nullable=False, default="scheduled")  # scheduled | cancelled
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class persistenceVisit:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("visit_queries.yaml")
        queries.setdefault("create_visit", (
            'INSERT INTO divine_site_visits(id, broker_id, customer_name, customer_contact, visit_date, visit_time, notes, status, created_date, last_updated_date) '
            'VALUES (:id, :broker_id, :customer_name, :customer_contact, :visit_date, :visit_time, :notes, :status, :created_date, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_site_visits WHERE id = :id LIMIT 1;')
        queries.setdefault("list_by_broker", (
            'SELECT * FROM divine_site_visits WHERE broker_id = :broker_id AND status != \'cancelled\' '
            'ORDER BY visit_date ASC, visit_time ASC;'
        ))
        queries.setdefault("list_history_by_broker", (
            "SELECT * FROM divine_site_visits "
            "WHERE broker_id = :broker_id "
            "AND (status = 'cancelled' OR visit_date < :today OR (visit_date = :today AND visit_time < :now_time)) "
            "ORDER BY "
            "CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END ASC, "
            "CASE WHEN status = 'cancelled' THEN last_updated_date END DESC, "
            "visit_date DESC, visit_time DESC;"
        ))
        queries.setdefault("update_status", (
            'UPDATE divine_site_visits SET status = :status, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        self._queries = queries
        self._engine = engine

    def create_visit(self, id: str, broker_id: str, customer_name: str, customer_contact: str,
                      visit_date, visit_time: str, notes: str, status: str = "scheduled") -> VisitModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_visit")
                params = {
                    "id": id,
                    "broker_id": broker_id,
                    "customer_name": customer_name,
                    "customer_contact": customer_contact,
                    "visit_date": visit_date,
                    "visit_time": visit_time,
                    "notes": notes,
                    "status": status,
                    "created_date": now,
                    "last_updated_date": now,
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
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

    def list_by_broker(self, broker_id: str):
        with self._session_factory() as db:
            query = self._queries.get("list_by_broker")
            result = db.execute(text(query), {"broker_id": broker_id})
            return [RowWrapper(row) for row in result.mappings().all()]

    def list_history_by_broker(self, broker_id: str, today, now_time: str):
        with self._session_factory() as db:
            query = self._queries.get("list_history_by_broker")
            result = db.execute(text(query), {"broker_id": broker_id, "today": today, "now_time": now_time})
            return [RowWrapper(row) for row in result.mappings().all()]

    def update_status(self, id: str, status: str) -> VisitModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_status")
                params = {"id": id, "status": status, "last_updated_date": datetime.now(timezone.utc)}
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise
import logging
from sqlalchemy import Column, String, Date, Text, DateTime, text
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class VisitModel(Base):
    __tablename__ = "divine_site_visits"
    id = Column(String(36), primary_key=True)
    # Nullable: a website "request a callback" visit (see create_visit_request)
    # has no broker yet - one picks it up and fills this in later.
    broker_id = Column(String(6), index=True)
    customer_name = Column(String(200), nullable=False)
    customer_contact = Column(String(200))
    visit_date = Column(Date, nullable=False)
    # "HH:MM", kept as text - no timezone math needed, round-trips exactly what was typed.
    # Nullable: a callback request only has a preferred_window until a broker
    # locks in an exact time.
    visit_time = Column(String(5))
    notes = Column(Text)
    # scheduled | confirmed | completed | follow_up | no_show | converted | cancelled
    status = Column(String(20), nullable=False, default="scheduled")
    # Admin-panel display fields (see scripts/add_admin_site_visit_fields.py) - all
    # nullable, deliberately not FKs. customer_name/customer_contact stay the
    # source of truth for "who" since a visit may be logged for someone who
    # never created a website account (see customer_id below for when they did).
    origin_type = Column(String(20))  # 'CUSTOMER' | 'CHANNEL_PARTNER'
    source = Column(String(100))  # e.g. 'Website' or a channel partner's name
    project_name = Column(String(200))
    plot_number = Column(String(50))
    assigned_to = Column(String(200))  # free-text staff name - no Staff table yet
    # Website "request a callback" form fields (see scripts/add_site_visit_request_fields.py).
    customer_email = Column(String(255))
    preferred_window = Column(String(20))  # 'today' | 'tomorrow' | 'weekend'
    # Set only when POST /visits/request was called with a valid customer bearer
    # token (see DivineService.auth.get_optional_customer) - null for an
    # anonymous request or a broker-scheduled visit. Deliberately not a real FK
    # (nothing else in this table is either), just an indexed lookup key for
    # GET /visits/mine - see scripts/add_site_visit_customer_id_column.py.
    customer_id = Column(String(6), index=True)
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class persistenceVisit:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("visit_queries.yaml")
        queries.setdefault("create_visit", (
            'INSERT INTO divine_site_visits('
            'id, broker_id, customer_name, customer_contact, customer_email, visit_date, visit_time, notes, status, '
            'origin_type, project_name, created_date, last_updated_date) '
            'VALUES ('
            ':id, :broker_id, :customer_name, :customer_contact, :customer_email, :visit_date, :visit_time, :notes, :status, '
            ':origin_type, :project_name, :created_date, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_site_visits WHERE id = :id LIMIT 1;')
        queries.setdefault("list_by_broker", (
            "SELECT * FROM divine_site_visits WHERE broker_id = :broker_id AND status = 'scheduled' "
            'ORDER BY visit_date ASC, visit_time ASC;'
        ))
        queries.setdefault("list_mine", (
            'SELECT * FROM divine_site_visits WHERE '
            '(customer_id = :customer_id) '
            "OR (customer_id IS NULL AND :email IS NOT NULL AND lower(customer_email) = lower(:email)) "
            'ORDER BY created_date DESC;'
        ))
        queries.setdefault("list_history_by_broker", (
            "SELECT * FROM divine_site_visits "
            "WHERE broker_id = :broker_id "
            "AND (status = 'cancelled' OR status = 'completed' "
            "OR visit_date < :today OR (visit_date = :today AND visit_time < :now_time)) "
            "ORDER BY "
            "CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END ASC, "
            "CASE WHEN status = 'cancelled' THEN last_updated_date END DESC, "
            "visit_date DESC, visit_time DESC;"
        ))
        queries.setdefault("update_status", (
            'UPDATE divine_site_visits SET status = :status, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        queries.setdefault("complete_visit", (
            "UPDATE divine_site_visits "
            "SET status = 'completed', notes = :notes, last_updated_date = :last_updated_date "
            'WHERE id = :id RETURNING *;'
        ))
        queries.setdefault("create_visit_request", (
            'INSERT INTO divine_site_visits('
            'id, customer_name, customer_contact, customer_email, customer_id, visit_date, notes, status, '
            'origin_type, source, project_name, preferred_window, created_date, last_updated_date) '
            'VALUES ('
            ':id, :customer_name, :customer_contact, :customer_email, :customer_id, :visit_date, :notes, :status, '
            ':origin_type, :source, :project_name, :preferred_window, :created_date, :last_updated_date) '
            'RETURNING *;'
        ))
        self._queries = queries
        self._engine = engine

    def create_visit_request(self, id: str, customer_name: str, customer_contact: str, customer_email: str,
                              visit_date, notes: str, project_name: str, preferred_window: str,
                              status: str = "scheduled", origin_type: str = "CUSTOMER", source: str = "Website",
                              customer_id: str = None) -> VisitModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_visit_request")
                params = {
                    "id": id,
                    "customer_id": customer_id,
                    "customer_name": customer_name,
                    "customer_contact": customer_contact,
                    "customer_email": customer_email,
                    "visit_date": visit_date,
                    "notes": notes,
                    "status": status,
                    "origin_type": origin_type,
                    "source": source,
                    "project_name": project_name,
                    "preferred_window": preferred_window,
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

    def create_visit(self, id: str, broker_id: str, customer_name: str, customer_contact: str,
                      visit_date, visit_time: str, notes: str, status: str = "scheduled",
                      project_name: str = None, customer_email: str = None) -> VisitModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_visit")
                params = {
                    "id": id,
                    "broker_id": broker_id,
                    "customer_name": customer_name,
                    "customer_contact": customer_contact,
                    "customer_email": customer_email,
                    "visit_date": visit_date,
                    "visit_time": visit_time,
                    "notes": notes,
                    "status": status,
                    "origin_type": "CHANNEL_PARTNER",
                    "project_name": project_name,
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

    def list_mine(self, customer_id: str, email: str):
        """A visit matches when its own customer_id equals the caller's, OR
        (for a row with no customer_id - an anonymous website request or a
        broker-scheduled visit) its customer_email matches the caller's
        account email. One query, not two, for the common case."""
        try:
            with self._session_factory() as db:
                query = self._queries.get("list_mine")
                result = db.execute(text(query), {"customer_id": customer_id, "email": email})
                return [RowWrapper(row) for row in result.mappings().all()]
        except SQLAlchemyError:
            logger.exception("visit_list_mine_query_failed")
            raise RuntimeError("db_error")

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

    def complete_visit(self, id: str, notes) -> VisitModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("complete_visit")
                params = {"id": id, "notes": notes, "last_updated_date": datetime.now(timezone.utc)}
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise
import logging
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, UniqueConstraint, text
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class PaymentMilestoneModel(Base):
    __tablename__ = "divine_payment_milestones"
    __table_args__ = (UniqueConstraint("booking_id", "milestone_no", name="uq_milestone_booking_no"),)
    id = Column(String(48), primary_key=True)
    booking_id = Column(String(36), nullable=False, index=True)
    customer_id = Column(String(6), nullable=False, index=True)
    project_id = Column(String(120))
    inventory_id = Column(String(36))
    milestone_no = Column(Integer, nullable=False)
    label = Column(String(120))
    percent = Column(Numeric(6, 3))
    due_days = Column(Integer)
    due_date = Column(Date)
    amount = Column(Numeric(14, 2), nullable=False, default=0)
    status = Column(String(12), nullable=False, default="upcoming")  # paid|due|overdue|upcoming
    paid_on = Column(DateTime(timezone=True))
    paid_payment_id = Column(String(36))
    created_date = Column(DateTime(timezone=True))
    last_updated_date = Column(DateTime(timezone=True))


class PaymentReminderModel(Base):
    __tablename__ = "divine_payment_reminders"
    __table_args__ = (UniqueConstraint("milestone_id", "kind", "week_key", name="uq_reminder_milestone_kind_week"),)
    id = Column(String(36), primary_key=True)
    customer_id = Column(String(6), nullable=False, index=True)
    booking_id = Column(String(36))
    milestone_id = Column(String(48), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # T_MINUS_20|T_MINUS_5|DUE_TODAY|OVERDUE
    week_key = Column(String(12), nullable=False, default="")
    amount = Column(Numeric(14, 2))
    due_date = Column(Date)
    email_to = Column(String(255))
    delivery_status = Column(String(20))
    sent_at = Column(DateTime(timezone=True))


class persistenceMilestone:
    """Persistence for booking-plan milestones and the payment-due reminder log.
    Read methods may raise on a genuine DB fault (the service layer degrades);
    write methods roll back and re-raise. Nothing here formats or leaks a raw
    error to a caller."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("milestone_queries.yaml")
        queries.setdefault("insert_milestone", (
            "INSERT INTO divine_payment_milestones "
            "(id, booking_id, customer_id, project_id, inventory_id, milestone_no, label, "
            " percent, due_days, due_date, amount, status, paid_on, paid_payment_id, "
            " created_date, last_updated_date) "
            "VALUES (:id, :booking_id, :customer_id, :project_id, :inventory_id, :milestone_no, "
            " :label, :percent, :due_days, :due_date, :amount, :status, :paid_on, :paid_payment_id, "
            " :now, :now) "
            "ON CONFLICT (booking_id, milestone_no) DO NOTHING;"
        ))
        queries.setdefault("list_for_customer", (
            "SELECT * FROM divine_payment_milestones WHERE customer_id = :customer_id "
            "ORDER BY project_id NULLS FIRST, milestone_no ASC;"
        ))
        queries.setdefault("list_for_booking", (
            "SELECT * FROM divine_payment_milestones WHERE booking_id = :booking_id "
            "ORDER BY milestone_no ASC;"
        ))
        queries.setdefault("get_by_id", "SELECT * FROM divine_payment_milestones WHERE id = :id LIMIT 1;")
        queries.setdefault("list_unpaid", (
            "SELECT * FROM divine_payment_milestones WHERE status <> 'paid' "
            "ORDER BY customer_id ASC, booking_id ASC, milestone_no ASC;"
        ))
        queries.setdefault("mark_paid", (
            "UPDATE divine_payment_milestones SET status = 'paid', paid_on = :paid_on, "
            "paid_payment_id = :payment_id, last_updated_date = :now "
            "WHERE id = :id AND status <> 'paid' RETURNING *;"
        ))
        queries.setdefault("set_status", (
            "UPDATE divine_payment_milestones SET status = :status, last_updated_date = :now "
            "WHERE id = :id AND status <> 'paid' RETURNING *;"
        ))
        queries.setdefault("update_amount", (
            "UPDATE divine_payment_milestones SET amount = :amount, last_updated_date = :now "
            "WHERE id = :id AND status <> 'paid' RETURNING *;"
        ))
        queries.setdefault("reminder_exists", (
            "SELECT 1 FROM divine_payment_reminders "
            "WHERE milestone_id = :milestone_id AND kind = :kind AND week_key = :week_key LIMIT 1;"
        ))
        queries.setdefault("insert_reminder", (
            "INSERT INTO divine_payment_reminders "
            "(id, customer_id, booking_id, milestone_id, kind, week_key, amount, due_date, "
            " email_to, delivery_status, sent_at) "
            "VALUES (:id, :customer_id, :booking_id, :milestone_id, :kind, :week_key, :amount, "
            " :due_date, :email_to, :delivery_status, :now) "
            "ON CONFLICT (milestone_id, kind, week_key) DO NOTHING;"
        ))
        queries.setdefault("last_reminder_for_customer", (
            "SELECT * FROM divine_payment_reminders WHERE customer_id = :customer_id "
            "ORDER BY sent_at DESC LIMIT 1;"
        ))
        self._queries = queries
        self._engine = engine

    # ---- reads ---------------------------------------------------------
    def list_for_customer(self, customer_id: str):
        with self._session_factory() as db:
            rows = db.execute(text(self._queries["list_for_customer"]),
                              {"customer_id": customer_id}).mappings().all()
            return [RowWrapper(r) for r in rows]

    def list_for_booking(self, booking_id: str):
        with self._session_factory() as db:
            rows = db.execute(text(self._queries["list_for_booking"]),
                              {"booking_id": booking_id}).mappings().all()
            return [RowWrapper(r) for r in rows]

    def get(self, milestone_id: str):
        with self._session_factory() as db:
            row = db.execute(text(self._queries["get_by_id"]), {"id": milestone_id}).mappings().first()
            return RowWrapper(row) if row else None

    def list_unpaid(self):
        with self._session_factory() as db:
            rows = db.execute(text(self._queries["list_unpaid"])).mappings().all()
            return [RowWrapper(r) for r in rows]

    def last_reminder_for_customer(self, customer_id: str):
        with self._session_factory() as db:
            row = db.execute(text(self._queries["last_reminder_for_customer"]),
                             {"customer_id": customer_id}).mappings().first()
            return RowWrapper(row) if row else None

    def reminder_sent(self, milestone_id: str, kind: str, week_key: str = "") -> bool:
        with self._session_factory() as db:
            row = db.execute(text(self._queries["reminder_exists"]), {
                "milestone_id": milestone_id, "kind": kind, "week_key": week_key or "",
            }).first()
            return row is not None

    # ---- writes ------------------------------------------------------------
    def create_milestones(self, rows: list) -> int:
        """Bulk insert; idempotent on (booking_id, milestone_no). Returns the count
        of rows offered (not necessarily inserted)."""
        if not rows:
            return 0
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                for r in rows:
                    params = {
                        "id": r.get("id"),
                        "booking_id": r.get("booking_id"),
                        "customer_id": r.get("customer_id"),
                        "project_id": r.get("project_id"),
                        "inventory_id": r.get("inventory_id"),
                        "milestone_no": r.get("milestone_no"),
                        "label": r.get("label"),
                        "percent": r.get("percent"),
                        "due_days": r.get("due_days"),
                        "due_date": r.get("due_date"),
                        "amount": r.get("amount") or 0,
                        "status": r.get("status") or "upcoming",
                        "paid_on": r.get("paid_on"),
                        "paid_payment_id": r.get("paid_payment_id"),
                        "now": now,
                    }
                    db.execute(text(self._queries["insert_milestone"]), params)
                db.commit()
                return len(rows)
            except Exception:
                db.rollback()
                raise

    def mark_paid(self, milestone_id: str, payment_id: str, paid_on=None):
        with self._session_factory() as db:
            try:
                row = db.execute(text(self._queries["mark_paid"]), {
                    "id": milestone_id, "payment_id": payment_id,
                    "paid_on": paid_on or datetime.now(timezone.utc),
                    "now": datetime.now(timezone.utc),
                }).mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def set_status(self, milestone_id: str, status: str):
        with self._session_factory() as db:
            try:
                row = db.execute(text(self._queries["set_status"]), {
                    "id": milestone_id, "status": status, "now": datetime.now(timezone.utc),
                }).mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def update_amount(self, milestone_id: str, amount):
        with self._session_factory() as db:
            try:
                row = db.execute(text(self._queries["update_amount"]), {
                    "id": milestone_id, "amount": amount, "now": datetime.now(timezone.utc),
                }).mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def record_reminder(self, id: str, customer_id: str, booking_id: str, milestone_id: str,
                        kind: str, week_key: str, amount, due_date, email_to: str,
                        delivery_status: str) -> None:
        with self._session_factory() as db:
            try:
                db.execute(text(self._queries["insert_reminder"]), {
                    "id": id, "customer_id": customer_id, "booking_id": booking_id,
                    "milestone_id": milestone_id, "kind": kind, "week_key": week_key or "",
                    "amount": amount, "due_date": due_date, "email_to": email_to,
                    "delivery_status": delivery_status, "now": datetime.now(timezone.utc),
                })
                db.commit()
            except Exception:
                db.rollback()
                raise

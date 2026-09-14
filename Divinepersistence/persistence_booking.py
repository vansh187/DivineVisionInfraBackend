import logging
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Numeric, Integer, Text, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class BookingModel(Base):
    """A 'booking' didn't have its own identity before this feature - it was just
    an InventoryUnitModel row with status='booked'. This table gives it one:
    a friendly id ("BKG-2026-000001"), a KYC-review lifecycle status distinct
    from the plot's own inventory status, and an optimistic-concurrency version
    so two admins can't silently clobber each other's Approve/Reject."""
    __tablename__ = "divine_bookings"
    id = Column(String(20), primary_key=True)
    payment_id = Column(String(36), nullable=False, unique=True, index=True)
    inventory_id = Column(String(36), nullable=False, index=True)
    customer_id = Column(String(6), nullable=False, index=True)
    # Denormalized for the admin queue list (avoids a join on every page load).
    project_name = Column(String(150))
    unit_number = Column(String(20))
    amount = Column(Numeric(12, 2))
    # pending_kyc_review | booked | rejected | cancelled
    status = Column(String(20), nullable=False, default="pending_kyc_review", index=True)
    # pending | verified | needs_resubmission | rejected
    kyc_status = Column(String(20), nullable=False, default="pending", index=True)
    # Optimistic concurrency: every Approve/Reject/Cancel must supply the version
    # it read and this increments by 1 on each one that succeeds - a stale write
    # (someone else already decided) is rejected rather than silently overwritten.
    version = Column(Integer, nullable=False, default=1)
    admin_note = Column(Text)
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class BookingDecisionModel(Base):
    """Append-only audit trail powering the admin 'Decision History' timeline.
    Never updated or deleted, only inserted."""
    __tablename__ = "divine_booking_decisions"
    id = Column(String(36), primary_key=True)
    booking_id = Column(String(20), nullable=False, index=True)
    # An admin id (e.g. "DV1234"), or "system" for an automated entry (payment
    # settling, a webhook-driven state change) - never blank.
    actor = Column(String(255), nullable=False)
    # payment_received | approved | rejected | cancelled
    action = Column(String(30), nullable=False)
    note = Column(Text)
    created_date = Column(DateTime)


class persistenceBooking:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("booking_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def _generate_booking_id(self, db: Session) -> str:
        """'BKG-<year>-<6-digit running count>', e.g. BKG-2026-000001. Not a
        Postgres SEQUENCE (this codebase has none) - counts existing bookings for
        the current year and retries on a collision (another insert landed on the
        same count between the read and the write), same shape as
        persistenceCustomer._generate_unique_id's retry loop."""
        year = datetime.now(timezone.utc).year
        for attempt in range(50):
            count = db.query(BookingModel).filter(
                BookingModel.id.like(f"BKG-{year}-%")
            ).count()
            candidate = f"BKG-{year}-{(count + 1 + attempt):06d}"
            if not db.query(BookingModel).filter_by(id=candidate).first():
                return candidate
        raise RuntimeError("Failed to generate unique booking id")

    def create_booking(self, payment_id: str, inventory_id: str, customer_id: str,
                        project_name: str = None, unit_number: str = None, amount=None) -> BookingModel:
        """Creates the booking row AND its first decision-history entry
        ('payment_received') in one transaction. Returns None (not an error) if a
        booking already exists for this payment_id - the unique constraint makes
        this idempotent for a payment-settlement retry (webhook redelivery after
        /verify already created it)."""
        with self._session_factory() as db:
            try:
                existing = db.execute(
                    text(self._q("get_by_payment_id")), {"payment_id": payment_id},
                ).mappings().first()
                if existing:
                    db.rollback()
                    return RowWrapper(existing)

                booking_id = self._generate_booking_id(db)
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("create_booking")), {
                    "id": booking_id, "payment_id": payment_id, "inventory_id": inventory_id,
                    "customer_id": customer_id, "project_name": project_name, "unit_number": unit_number,
                    "amount": amount, "status": "pending_kyc_review", "kyc_status": "pending",
                    "version": 1, "created_date": now, "last_updated_date": now,
                })
                row = result.mappings().first()
                db.execute(text(self._q("insert_decision")), {
                    "id": str(uuid.uuid4()), "booking_id": booking_id, "actor": "system",
                    "action": "payment_received", "note": None, "created_date": now,
                })
                db.commit()
                return RowWrapper(row)
            except SQLAlchemyError:
                db.rollback()
                logger.exception("booking_create_failed payment_id=%s", payment_id)
                raise
            except Exception:
                db.rollback()
                raise

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_by_id")), {"id": id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def get_by_payment_id(self, payment_id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_by_payment_id")), {"payment_id": payment_id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def list_queue(self, search: str = None, status: str = None, kyc_status: str = None,
                    limit: int = 20, offset: int = 0):
        """Rows carry `total_count` (a window-function total over the whole
        filtered set) so the common non-empty page never needs a second query -
        same convention as persistence_admin_visits.list_visits."""
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_queue")), {
                "search": (search or None), "status": status, "kyc_status": kyc_status,
                "limit": limit, "offset": offset,
            })
            return [RowWrapper(row) for row in result.mappings().all()]

    def count_queue(self, search: str = None, status: str = None, kyc_status: str = None) -> int:
        with self._session_factory() as db:
            result = db.execute(text(self._q("count_queue")), {
                "search": (search or None), "status": status, "kyc_status": kyc_status,
            })
            row = result.mappings().first()
            return int(row["total"]) if row else 0

    def list_by_customer(self, customer_id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_by_customer")), {"customer_id": customer_id})
            return [RowWrapper(row) for row in result.mappings().all()]

    def update_decision(self, id: str, expected_version: int, status: str, kyc_status: str, admin_note: str):
        """Optimistic-locked transition: only applies when the row's version still
        matches expected_version, and bumps it by 1. Returns None on a version
        mismatch or a missing booking - the caller (serviceBookingKyc) turns that
        into a 409 rather than silently double-applying a decision."""
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._q("update_decision")), {
                    "id": id, "expected_version": expected_version, "status": status,
                    "kyc_status": kyc_status, "admin_note": admin_note,
                    "last_updated_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def add_decision(self, booking_id: str, actor: str, action: str, note: str = None):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._q("insert_decision")), {
                    "id": str(uuid.uuid4()), "booking_id": booking_id, "actor": actor,
                    "action": action, "note": note, "created_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list_decisions(self, booking_id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_decisions")), {"booking_id": booking_id})
            return [RowWrapper(row) for row in result.mappings().all()]

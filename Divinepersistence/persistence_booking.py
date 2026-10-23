import logging
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Numeric, Integer, Text, UniqueConstraint, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries
from .persistence_document import persistenceDocument

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
    # payment_received | approved | rejected | cancelled | kyc_document_uploaded
    action = Column(String(30), nullable=False)
    note = Column(Text)
    created_date = Column(DateTime)


class BookingDocumentUploadModel(Base):
    """One row per KYC document an admin uploaded/replaced against a booking
    (POST /admin/bookings/{id}/documents/{type}). Triple duty: the detailed audit
    record (which document row replaced which, request id, byte size, hash), the
    Idempotency-Key store (unique per admin), and the source the per-admin /
    per-booking upload rate limits count from - DB-backed, so the limits hold
    across multiple app instances. Append-only. Stores a hash, never file bytes."""
    __tablename__ = "divine_booking_document_uploads"
    __table_args__ = (UniqueConstraint("admin_id", "idempotency_key", name="uq_booking_doc_upload_idem"),)
    id = Column(String(36), primary_key=True)
    booking_id = Column(String(20), nullable=False, index=True)
    admin_id = Column(String(255), nullable=False, index=True)
    document_type = Column(String(100), nullable=False)
    # divine_documents.id of the newly stored file, and of the one it superseded
    # (NULL when this was the first upload of that type). The prior row/object is
    # kept, never deleted.
    document_id = Column(String(36), nullable=False)
    previous_document_id = Column(String(36))
    idempotency_key = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    request_id = Column(String(128))
    note = Column(Text)
    byte_size = Column(Integer, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    created_date = Column(DateTime, index=True)


class persistenceBooking:
    def __init__(self, session_factory=SessionLocal, document_persistence: persistenceDocument = None):
        self._session_factory = session_factory
        self._queries = load_queries("booking_queries.yaml")
        self._engine = engine
        self._document_persistence_override = document_persistence

    def _documents(self) -> persistenceDocument:
        """Lazily built - only the admin KYC upload transaction needs it."""
        if self._document_persistence_override is None:
            self._document_persistence_override = persistenceDocument(self._session_factory)
        return self._document_persistence_override

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

    def get_document_upload_by_key(self, admin_id: str, idempotency_key: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_document_upload_by_key")), {
                "admin_id": admin_id, "idempotency_key": idempotency_key,
            })
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def _count_recent_uploads(self, db, admin_id: str, booking_id: str,
                              admin_since: datetime, booking_since: datetime) -> tuple:
        row = db.execute(text(self._q("count_recent_document_uploads")), {
            "admin_id": admin_id, "booking_id": booking_id,
            "admin_since": admin_since, "booking_since": booking_since,
        }).mappings().first()
        return (int(row["by_admin"]), int(row["by_booking"])) if row else (0, 0)

    def count_recent_document_uploads(self, admin_id: str, booking_id: str,
                                      admin_since: datetime, booking_since: datetime) -> tuple:
        """(uploads by this admin since admin_since, uploads on this booking since
        booking_since) in one round trip."""
        with self._session_factory() as db:
            return self._count_recent_uploads(db, admin_id, booking_id, admin_since, booking_since)

    def record_admin_document_upload(self, booking_id: str, expected_version: int, uploadable_status: str,
                                     admin_id: str, document: dict, idempotency_key: str,
                                     request_fingerprint: str, request_id: str, note: str, byte_size: int,
                                     content_sha256: str, admin_limit: int, admin_since: datetime,
                                     booking_limit: int, booking_since: datetime) -> tuple:
        """One transaction for an admin KYC document upload. Returns (outcome, booking):
          ("ok", updated_booking)
          ("stale", current_booking_or_None) - version/status guard didn't match
          ("admin_rate_limited" | "booking_rate_limited", None)
        Nothing is written unless the outcome is "ok".

        The admin's row and the document owner's row (customer, or broker - the
        role written on the document comes from whichever table holds the owner)
        are locked first (no-op UPDATEs - a portable row lock; SQLite serializes
        writers anyway), then the booking rows by the version bumps, always in
        that order. Under those locks the rate-limit counts and the "which
        document does this supersede" read can't race a concurrent upload, so the
        limits and the supersession chain are exact. The owner's OTHER bookings
        still under review get a version bump and a history entry too (see
        bump_sibling_pending_bookings). Raises RuntimeError when the owner exists
        in neither user table. Lets the unique (admin_id, idempotency_key)
        IntegrityError propagate - a concurrent request with the same key won
        the race."""
        documents = self._documents()
        owner_id = document["owner_id"]
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                db.execute(text(self._q("lock_admin_row")), {"id": admin_id})
                owner_role = None
                for role, query in (("customer", "lock_customer_row"), ("broker", "lock_broker_row")):
                    if db.execute(text(self._q(query)), {"id": owner_id}).rowcount:
                        owner_role = role
                        break
                if owner_role is None:
                    raise RuntimeError("booking_owner_not_found")
                booking = db.execute(text(self._q("bump_version_if_status")), {
                    "id": booking_id, "expected_version": expected_version,
                    "status": uploadable_status, "last_updated_date": now,
                }).mappings().first()
                if not booking:
                    db.rollback()
                    current = db.execute(text(self._q("get_by_id")), {"id": booking_id}).mappings().first()
                    return "stale", (RowWrapper(current) if current else None)

                by_admin, by_booking = self._count_recent_uploads(
                    db, admin_id, booking_id, admin_since, booking_since)
                if by_admin >= admin_limit:
                    db.rollback()
                    return "admin_rate_limited", None
                if by_booking >= booking_limit:
                    db.rollback()
                    return "booking_rate_limited", None

                previous = documents.get_latest_by_owner_and_type(owner_id, document["document_type"], db=db)
                documents.insert_document(db, **{**document, "owner_role": owner_role})
                db.execute(text(self._q("insert_decision")), {
                    "id": str(uuid.uuid4()), "booking_id": booking_id, "actor": admin_id,
                    "action": "kyc_document_uploaded", "note": note, "created_date": now,
                })
                siblings = db.execute(text(self._q("bump_sibling_pending_bookings")), {
                    "customer_id": owner_id, "status": uploadable_status, "id": booking_id,
                    "last_updated_date": now,
                }).mappings().all()
                sibling_note = f"Shared {document['document_type']} document updated via booking {booking_id}"
                for sibling in siblings:
                    db.execute(text(self._q("insert_decision")), {
                        "id": str(uuid.uuid4()), "booking_id": sibling["id"], "actor": admin_id,
                        "action": "kyc_document_uploaded",
                        "note": f"{sibling_note}: {note}" if note else sibling_note,
                        "created_date": now,
                    })
                db.execute(text(self._q("insert_document_upload")), {
                    "id": str(uuid.uuid4()), "booking_id": booking_id, "admin_id": admin_id,
                    "document_type": document["document_type"], "document_id": document["id"],
                    "previous_document_id": getattr(previous, "id", None) if previous else None,
                    "idempotency_key": idempotency_key, "request_fingerprint": request_fingerprint,
                    "request_id": request_id, "note": note, "byte_size": byte_size,
                    "content_sha256": content_sha256, "created_date": now,
                })
                db.commit()
                return "ok", RowWrapper(booking)
            except IntegrityError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                logger.exception("booking_document_upload_commit_failed booking_id=%s", booking_id)
                raise

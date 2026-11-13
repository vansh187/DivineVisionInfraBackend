import uuid
import weakref
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Column, DateTime, Numeric, String, UniqueConstraint, bindparam, inspect, text
from sqlalchemy.orm import Session
from DivineDTO.models import MANUAL_INVENTORY_STATUSES
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


def _to_bindable(value):
    # sqlite3's DBAPI can't bind a raw Decimal on a textual (non-ORM) execute() - Postgres
    # handles Decimal fine, but this keeps both backends working through the same code path.
    return float(value) if isinstance(value, Decimal) else value


# ---- Plot status history ---------------------------------------------------
# Every change to divine_project_inventory.status is written to
# divine_inventory_status_log in the SAME transaction as the change, with the
# status before and after, who made it and when. The log is append-only: nothing
# in the app updates or deletes its rows (unlike divine_inventory_status_history,
# which the sheet-sync rollback deletes once a batch is undone).

# What triggered a change - shown in the admin panel's plot history.
SOURCE_ADMIN_PANEL = "admin_panel"
SOURCE_PARTNER_RESERVATION = "partner_reservation"
SOURCE_PARTNER_RELEASE = "partner_release"
SOURCE_PARTNER_SALE = "partner_sale"
SOURCE_RESERVATION_EXPIRY = "reservation_expiry"
SOURCE_BOOKING_PAYMENT = "booking_payment"
SOURCE_KYC_APPROVAL = "kyc_approval"
SOURCE_KYC_REJECTION = "kyc_rejection"
SOURCE_BOOKING_CANCELLATION = "booking_cancellation"
SOURCE_STAFF_REPAIR = "staff_repair"
SOURCE_INVENTORY_INGEST = "inventory_ingest"
SOURCE_SHEET_SYNC = "sheet_sync"
SOURCE_SYNC_ROLLBACK = "sync_rollback"
SOURCE_TEST_BOOKING_CLEANUP = "test_booking_cleanup"

SYSTEM_ACTOR = {"role": "system", "id": None, "name": "System"}
# new_status of the history row written when a plot is deleted (sheet-sync
# rollback of a batch that inserted it). Not a status a plot can have.
PLOT_REMOVED = "removed"


def status_actor(role: str, actor_id: str = None, name: str = None) -> dict:
    """Who made a status change. `name` is looked up from the user tables when
    left out, and stored as it was at the time (a later rename doesn't rewrite
    history)."""
    return {"role": role, "id": actor_id, "name": name}


_ACTOR_NAME_QUERIES = {
    "admin": "SELECT full_name AS name, email AS fallback FROM divine_admin_users WHERE id = :id",
    "broker": ("SELECT TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS name, "
               "username AS fallback FROM divine_broker_users WHERE id = :id"),
    "customer": ("SELECT TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS name, "
                 "username AS fallback FROM divine_customer_users WHERE id = :id"),
}

_INSERT_STATUS_LOG = (
    "INSERT INTO divine_inventory_status_log(id, inventory_id, project_name, unit_number, old_status, "
    "new_status, changed_by_role, changed_by_id, changed_by_name, source, note, changed_at) "
    "VALUES (:id, :inventory_id, :project_name, :unit_number, :old_status, :new_status, :changed_by_role, "
    ":changed_by_id, :changed_by_name, :source, :note, :changed_at)"
)


def _actor_name(conn, actor: dict) -> str:
    name = (actor.get("name") or "").strip()
    if name:
        return name
    query = _ACTOR_NAME_QUERIES.get(actor.get("role"))
    if query and actor.get("id"):
        row = conn.execute(text(query), {"id": actor["id"]}).mappings().first()
        if row:
            name = (row["name"] or "").strip() or (row["fallback"] or "").strip()
    return name or actor.get("id") or "System"


# Engines whose database is known to have the log table as COMMITTED DDL.
# Keyed by engine object, not URL: separate in-memory SQLite databases share
# the URL "sqlite://".
_STATUS_LOG_TABLE_EXISTS = weakref.WeakSet()
_STATUS_LOG_TXN_KEY = "divine_status_log_created_in"


def ensure_status_log_table(conn) -> None:
    """Creates divine_inventory_status_log when missing. The CLI loaders and
    scripts/ingest_inventory.py can run before the app's startup create_all().

    A table that already existed is remembered per engine for the life of the
    process. One created here is remembered only for the current
    transaction: Postgres DDL is transactional, so a rollback removes it again,
    and a flag kept on the pooled connection would wrongly outlive it."""
    bind = conn.connection() if isinstance(conn, Session) else conn
    if bind.engine in _STATUS_LOG_TABLE_EXISTS:
        return
    transaction = bind.get_transaction()
    if transaction is not None and bind.info.get(_STATUS_LOG_TXN_KEY) is transaction:
        return
    if inspect(bind).has_table(InventoryStatusLogModel.__tablename__):
        _STATUS_LOG_TABLE_EXISTS.add(bind.engine)
        return
    InventoryStatusLogModel.__table__.create(bind=bind, checkfirst=True)
    bind.info[_STATUS_LOG_TXN_KEY] = transaction


def record_status_change(conn, *, inventory_id: str, old_status, new_status: str, source: str,
                         actor: dict = None, note: str = None, project_name: str = None,
                         unit_number: str = None, changed_at=None):
    """Append one row to divine_inventory_status_log on `conn` (a Session or a
    Connection - the caller's open transaction). No-op when the status did not
    actually change. ``old_status`` None = the plot was just created;
    ``new_status`` PLOT_REMOVED = it was deleted. Returns the inserted values,
    or None."""
    if not inventory_id or not new_status or old_status == new_status:
        return None
    ensure_status_log_table(conn)
    actor = actor or SYSTEM_ACTOR
    entry = {
        "id": str(uuid.uuid4()),
        "inventory_id": inventory_id,
        "project_name": project_name,
        "unit_number": unit_number,
        "old_status": old_status,
        "new_status": new_status,
        "changed_by_role": (actor.get("role") or "system")[:20],
        "changed_by_id": (str(actor["id"])[:64] if actor.get("id") else None),
        "changed_by_name": _actor_name(conn, actor)[:255],
        "source": source[:40],
        "note": ((note or "").strip()[:255] or None),
        "changed_at": changed_at or datetime.now(timezone.utc),
    }
    conn.execute(text(_INSERT_STATUS_LOG), entry)
    return entry


class InventoryUnitModel(Base):
    __tablename__ = "divine_project_inventory"
    __table_args__ = (UniqueConstraint("project_name", "unit_number", name="uq_inventory_project_unit"),)
    id = Column(String(36), primary_key=True)
    project_name = Column(String(150), nullable=False, index=True)
    city = Column(String(100), nullable=False, index=True)
    locality = Column(String(150))
    block = Column(String(20))
    unit_number = Column(String(20), nullable=False)
    unit_type = Column(String(20), nullable=False, default="plot", index=True)
    width_mtr = Column(Numeric(8, 3))
    length_mtr = Column(Numeric(8, 3))
    area_sqmt = Column(Numeric(10, 3), nullable=False)
    area_sqyd = Column(Numeric(10, 2), nullable=False)
    status = Column(String(20), nullable=False, default="available", index=True)
    reserved_by_broker_id = Column(String(6), index=True)
    reserved_at = Column(DateTime(timezone=True))
    reserved_until = Column(DateTime(timezone=True))
    booked_at = Column(DateTime(timezone=True))
    booked_payment_id = Column(String(36))
    booked_by = Column(String(6), index=True)
    # Client-sheet sync (scripts/sync_inventory_from_sheet.py): 'offline' = sold
    # outside the app, 'app' = booked through our flow.
    sale_source = Column(String(20))
    status_note = Column(String(255))
    status_updated_at = Column(DateTime(timezone=True))
    created_date = Column(DateTime(timezone=True))
    last_updated_date = Column(DateTime(timezone=True))


class InventoryStatusHistoryModel(Base):
    """Audit trail of bulk inventory changes (sheet syncs / buyer imports);
    rows of one batch_id are what --rollback restores."""
    __tablename__ = "divine_inventory_status_history"
    id = Column(String(36), primary_key=True)
    inventory_id = Column(String(36), nullable=False, index=True)
    project_name = Column(String(150))
    unit_number = Column(String(20))
    old_status = Column(String(20))
    new_status = Column(String(20))
    old_unit_number = Column(String(20))
    new_unit_number = Column(String(20))
    old_values = Column(String(2000))   # JSON snapshot of the row before the change
    source = Column(String(255))
    batch_id = Column(String(40), nullable=False, index=True)
    note = Column(String(255))
    created_date = Column(DateTime(timezone=True))


class InventoryStatusLogModel(Base):
    """Permanent history of every plot status change: before, after, who, when.
    Written by record_status_change() only; never updated or deleted."""
    __tablename__ = "divine_inventory_status_log"
    id = Column(String(36), primary_key=True)
    inventory_id = Column(String(36), nullable=False, index=True)
    project_name = Column(String(150))
    unit_number = Column(String(20))
    old_status = Column(String(20))
    new_status = Column(String(20), nullable=False)
    changed_by_role = Column(String(20), nullable=False)   # admin | broker | customer | system
    changed_by_id = Column(String(64))
    changed_by_name = Column(String(255), nullable=False)
    source = Column(String(40), nullable=False)
    note = Column(String(255))
    changed_at = Column(DateTime(timezone=True), nullable=False, index=True)


class InventoryEventModel(Base):
    __tablename__ = "divine_inventory_events"
    id = Column(String(36), primary_key=True)
    inventory_id = Column(String(36), nullable=False, index=True)
    lead_id = Column(String(36), index=True)
    session_id = Column(String(36))
    event_type = Column(String(20), nullable=False, default="view")
    created_date = Column(DateTime(timezone=True))


class InventoryReservationModel(Base):
    __tablename__ = "divine_inventory_reservations"
    id = Column(String(36), primary_key=True)
    inventory_id = Column(String(36), nullable=False, index=True)
    broker_id = Column(String(6), nullable=False, index=True)
    reserved_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True))
    outcome = Column(String(20), nullable=False, default="active")
    created_date = Column(DateTime(timezone=True))


class persistenceInventory:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("inventory_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    @staticmethod
    def _log_change(db, old_status, row, source: str, actor: dict = None, note: str = None, changed_at=None):
        record_status_change(
            db, inventory_id=row["id"], old_status=old_status, new_status=row["status"], source=source,
            actor=actor, note=note, project_name=row["project_name"], unit_number=row["unit_number"],
            changed_at=changed_at,
        )

    def upsert_unit(self, id: str, project_name: str, city: str, unit_number: str,
                     locality: str = None, block: str = None, unit_type: str = "plot",
                     width_mtr=None, length_mtr=None, area_sqmt=None, area_sqyd=None,
                     status: str = "available", actor: dict = None):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {
                    "id": id, "project_name": project_name, "city": city, "locality": locality,
                    "block": block, "unit_number": unit_number, "unit_type": unit_type,
                    "width_mtr": _to_bindable(width_mtr), "length_mtr": _to_bindable(length_mtr),
                    "area_sqmt": _to_bindable(area_sqmt), "area_sqyd": _to_bindable(area_sqyd), "status": status,
                    "created_date": now, "last_updated_date": now,
                }
                existing = db.execute(text(self._q("get_unit_status_by_project_unit")), {
                    "project_name": project_name, "unit_number": unit_number,
                }).mappings().first()
                result = db.execute(text(self._q("upsert_unit")), params)
                row = result.mappings().first()
                if row:
                    # A new plot is logged as created (old status None).
                    self._log_change(db, existing["status"] if existing else None, row,
                                     SOURCE_INVENTORY_INGEST, actor, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def expire_stale_reservations(self):
        # Lazy expiry: this repo has no scheduler, so every inventory read path calls this
        # first. It self-heals the moment anyone next touches the table - matches the same
        # lazy-status-at-read-time convention service_visit.py uses for "completed" visits.
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                expired = db.execute(text(self._q("expire_stale_units")), {"now": now}).mappings().all()
                for row in expired:
                    self._log_change(db, "reserved", row, SOURCE_RESERVATION_EXPIRY,
                                     note="Channel partner reservation expired", changed_at=now)
                db.execute(text(self._q("expire_stale_reservation_history")), {"now": now})
                db.commit()
            except Exception:
                db.rollback()
                raise

    def search(self, project_name: str = None, city: str = None, unit_type: str = None,
               status: str = None, min_area_sqyd: float = None, max_area_sqyd: float = None,
               limit: int = 20, offset: int = 0):
        self.expire_stale_reservations()
        with self._session_factory() as db:
            result = db.execute(text(self._q("search_units")), {
                "project_name": project_name, "city": city, "unit_type": unit_type, "status": status,
                "min_area_sqyd": min_area_sqyd, "max_area_sqyd": max_area_sqyd,
                "limit": limit, "offset": offset,
            })
            return [RowWrapper(row) for row in result.mappings().all()]

    def get_by_id(self, id: str):
        self.expire_stale_reservations()
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_unit_by_id")), {"id": id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def distinct_plot_sizes(self, project_name: str = None, unit_type: str = None):
        # The full size catalogue a project offers - reservation status is irrelevant here
        # (a reserved unit still represents a size on offer), so this deliberately does not
        # call expire_stale_reservations() or filter by status.
        with self._session_factory() as db:
            result = db.execute(text(self._q("distinct_plot_sizes")), {
                "project_name": project_name, "unit_type": unit_type,
            })
            return [RowWrapper(row) for row in result.mappings().all()]

    def record_event(self, id: str, inventory_id: str, lead_id: str = None,
                      session_id: str = None, event_type: str = "view"):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "inventory_id": inventory_id, "lead_id": lead_id,
                    "session_id": session_id, "event_type": event_type,
                    "created_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("record_event")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list_events_for_lead(self, lead_id: str, limit: int = 20):
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_events_for_lead")), {"lead_id": lead_id, "limit": limit})
            return [RowWrapper(row) for row in result.mappings().all()]

    def list_recent_available_units(self, limit: int = 20):
        self.expire_stale_reservations()
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_recent_available_units")), {"limit": limit})
            return [RowWrapper(row) for row in result.mappings().all()]

    # ---- Channel Partner reservations --------------------------------------
    def reserve_unit(self, id: str, broker_id: str, reservation_id: str, reserved_at, reserved_until,
                     actor: dict = None):
        self.expire_stale_reservations()
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._q("reserve_unit")), {
                    "id": id, "broker_id": broker_id, "reserved_at": reserved_at, "reserved_until": reserved_until,
                })
                row = result.mappings().first()
                if not row:
                    # Nothing to roll back (the guarded UPDATE matched zero rows), but keeping the
                    # explicit rollback+return-None here mirrors the intended "no side effect on
                    # a failed reservation attempt" contract rather than relying on the session's
                    # implicit close-without-commit behavior.
                    db.rollback()
                    return None
                db.execute(text(self._q("insert_reservation_history")), {
                    "id": reservation_id, "inventory_id": id, "broker_id": broker_id,
                    "reserved_at": reserved_at, "expires_at": reserved_until,
                })
                self._log_change(db, "available", row, SOURCE_PARTNER_RESERVATION,
                                 actor or status_actor("broker", broker_id), changed_at=reserved_at)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def release_reservation(self, id: str, broker_id: str, target_status: str, outcome: str,
                            actor: dict = None):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("release_reservation")), {
                    "id": id, "broker_id": broker_id, "target_status": target_status, "ended_at": now,
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.execute(text(self._q("close_reservation_history")), {
                    "inventory_id": id, "broker_id": broker_id, "outcome": outcome, "ended_at": now,
                })
                self._log_change(db, "reserved", row,
                                 SOURCE_PARTNER_SALE if target_status == "sold" else SOURCE_PARTNER_RELEASE,
                                 actor or status_actor("broker", broker_id), changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list_reservations_for_broker(self, broker_id: str):
        self.expire_stale_reservations()
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_reservations_for_broker")), {"broker_id": broker_id})
            return [RowWrapper(row) for row in result.mappings().all()]

    # ---- customer bookings ------------------------------------------------
    def book_unit(self, id: str, payment_id: str, customer_id: str = None,
                  actor: dict = None, source: str = SOURCE_STAFF_REPAIR, note: str = None):
        """Race-safe flip to 'booked'. The guarded UPDATE only matches a unit that is
        still 'available'; one already 'booked' by THIS same payment is matched by
        book_unit_rerun instead (so the document-upload safety-net can re-run it as
        a no-op that writes no history row). Returns the row on
        success, None when the unit is missing or locked by someone else. Never
        raises for a business miss - only a genuine DB error propagates."""
        self.expire_stale_reservations()
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {"id": id, "payment_id": payment_id, "customer_id": customer_id, "now": now}
                # The status change itself only matches an 'available' plot, so
                # exactly one of two concurrent calls for a payment logs it: the
                # loser's UPDATE re-checks the guard after the winner commits,
                # matches nothing, and falls through to the re-run step below.
                row = db.execute(text(self._q("book_unit")), params).mappings().first()
                if row:
                    self._log_change(db, "available", row, source,
                                     actor or (status_actor("customer", customer_id) if customer_id else None),
                                     note=note, changed_at=now)
                else:
                    # Already 'booked' by THIS payment (safety-net re-run):
                    # same columns as before, status unchanged, nothing logged.
                    row = db.execute(text(self._q("book_unit_rerun")), params).mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def unbook_unit(self, id: str,
                    actor: dict = None, source: str = SOURCE_STAFF_REPAIR, note: str = None):
        """Repair path: 'booked' -> 'available', clearing the booking columns. Returns
        the row on success, None if the unit isn't currently 'booked'."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("unbook_unit")), {"id": id, "now": now})
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                self._log_change(db, "booked", row, source, actor, note=note, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def unbook_unit_for_payment(self, id: str, payment_id: str,
                                actor: dict = None, source: str = SOURCE_BOOKING_CANCELLATION, note: str = None):
        """Admin-cancel-a-booked-plot path: 'booked' -> 'available', only for the
        SAME payment currently holding the unit - same payment-matched guard as
        release_from_kyc_review, so a stale/mismatched booking row can never
        release a unit some other payment now owns. Returns None if the unit
        isn't currently 'booked' under that payment."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("unbook_unit_for_payment")), {
                    "id": id, "payment_id": payment_id, "now": now,
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                self._log_change(db, "booked", row, source, actor, note=note, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    # ---- Booking KYC review -----------------------------------------------
    def hold_for_kyc_review(self, id: str, payment_id: str, customer_id: str = None,
                            actor: dict = None, source: str = SOURCE_BOOKING_PAYMENT, note: str = None):
        """Race-safe flip to 'pending_kyc_review' - the plot-booking payment's
        settlement now holds the unit for admin review instead of booking it
        outright (see DivineService/service_payment.py::_apply_booking_to_inventory).
        Same guarded-UPDATE shape as book_unit: matches 'available'/'held', or a
        unit already held by THIS same payment (idempotent re-run safety).
        Returns None for a business miss (unit missing/locked by someone else);
        only a genuine DB error raises."""
        self.expire_stale_reservations()
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {"id": id, "payment_id": payment_id, "customer_id": customer_id, "now": now}
                # The status change itself only matches an 'available' plot, so
                # exactly one of two concurrent calls for a payment logs it: the
                # loser's UPDATE re-checks the guard after the winner commits,
                # matches nothing, and falls through to the re-run step below.
                row = db.execute(text(self._q("hold_for_kyc_review")), params).mappings().first()
                if row:
                    self._log_change(db, "available", row, source,
                                     actor or (status_actor("customer", customer_id) if customer_id else None),
                                     note=note, changed_at=now)
                else:
                    # Already 'pending_kyc_review' by THIS payment (safety-net re-run):
                    # same columns as before, status unchanged, nothing logged.
                    row = db.execute(text(self._q("hold_for_kyc_review_rerun")), params).mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def confirm_booking_after_kyc(self, id: str, payment_id: str,
                                  actor: dict = None, source: str = SOURCE_KYC_APPROVAL, note: str = None):
        """Admin-approve path: 'pending_kyc_review' -> 'booked', only for the SAME
        payment that put it on hold (so an approve can't accidentally confirm a
        unit some other payment is now holding). Returns None if the unit isn't
        currently held for that payment."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("confirm_booking_after_kyc")), {
                    "id": id, "payment_id": payment_id, "now": now,
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                self._log_change(db, "pending_kyc_review", row, source, actor, note=note, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def release_from_kyc_review(self, id: str, payment_id: str,
                                actor: dict = None, source: str = SOURCE_KYC_REJECTION, note: str = None):
        """Admin-reject/cancel path: 'pending_kyc_review' -> 'available', clearing
        the booking columns - only for the SAME payment that put it on hold.
        Returns None if the unit isn't currently held for that payment."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                result = db.execute(text(self._q("release_from_kyc_review")), {
                    "id": id, "payment_id": payment_id, "now": now,
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                self._log_change(db, "pending_kyc_review", row, source, actor, note=note, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    # ---- Admin panel: Inventory section -----------------------------------
    def admin_project_status_counts(self):
        """(project_name, status, plots) rows - see admin_project_status_counts."""
        self.expire_stale_reservations()
        with self._session_factory() as db:
            result = db.execute(text(self._q("admin_project_status_counts")))
            return [RowWrapper(row) for row in result.mappings().all()]

    def admin_list_units(self, project_name: str = None, status: str = None, search: str = None,
                         limit: int = 20, offset: int = 0):
        """(rows, total) for one page of plots, each row with the time and name
        of its latest status change."""
        self.expire_stale_reservations()
        params = {"project_name": project_name, "status": status, "search": search}
        with self._session_factory() as db:
            total = db.execute(text(self._q("admin_count_units")), params).mappings().first()["total"]
            result = db.execute(text(self._q("admin_list_units")), {**params, "limit": limit, "offset": offset})
            rows = [dict(row) for row in result.mappings().all()]
            latest = {}
            if rows:
                query = text(self._q("admin_latest_changes")).bindparams(bindparam("ids", expanding=True))
                latest = {r["inventory_id"]: r for r in
                          db.execute(query, {"ids": [r["id"] for r in rows]}).mappings().all()}
            for row in rows:
                change = latest.get(row["id"])
                row["last_changed_at"] = change["changed_at"] if change else None
                row["last_changed_by_name"] = change["changed_by_name"] if change else None
            return [RowWrapper(row) for row in rows], int(total or 0)

    def admin_status_history(self, inventory_id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("admin_status_history")), {"inventory_id": inventory_id})
            return [RowWrapper(row) for row in result.mappings().all()]

    def admin_set_status(self, id: str, expected_status: str, new_status: str, actor: dict,
                         note: str = None):
        """Guarded manual change (see admin_set_status in inventory_queries.yaml)
        plus its history row, in one transaction. None when the guard didn't
        match: the plot changed since the admin loaded it, or it is locked."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = text(self._q("admin_set_status")).bindparams(
                    bindparam("manual_statuses", expanding=True))
                result = db.execute(query, {
                    "id": id, "expected_status": expected_status, "new_status": new_status,
                    "note": note, "now": now, "manual_statuses": list(MANUAL_INVENTORY_STATUSES),
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                self._log_change(db, expected_status, row, SOURCE_ADMIN_PANEL, actor, note=note, changed_at=now)
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def admin_export_links(self, payment_ids, customer_ids) -> dict:
        """What the Inventory CSV export needs beyond the plot rows, for one page
        of plots, in four batched reads: booking payments (+ booking row), the
        booking application forms by payment and by customer, and the customers."""
        payment_ids = sorted({str(i) for i in payment_ids or () if i})
        links = {"payments": [], "forms_by_payment": [], "forms_by_owner": [], "customers": []}
        with self._session_factory() as db:
            def fetch(name, ids):
                if not ids:
                    return []
                query = text(self._q(name)).bindparams(bindparam("ids", expanding=True))
                return [dict(r) for r in db.execute(query, {"ids": ids}).mappings().all()]

            links["payments"] = fetch("export_booking_payments", payment_ids)
            links["forms_by_payment"] = fetch("export_forms_by_payment", payment_ids)
            owners = sorted({str(i) for i in customer_ids or () if i}
                            | {str(p["customer_id"]) for p in links["payments"] if p.get("customer_id")})
            links["forms_by_owner"] = fetch("export_forms_by_owner", owners)
            links["customers"] = fetch("export_customers", owners)
        return links

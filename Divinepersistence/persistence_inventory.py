from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Column, DateTime, Numeric, String, UniqueConstraint, text
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


def _to_bindable(value):
    # sqlite3's DBAPI can't bind a raw Decimal on a textual (non-ORM) execute() - Postgres
    # handles Decimal fine, but this keeps both backends working through the same code path.
    return float(value) if isinstance(value, Decimal) else value


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
    created_date = Column(DateTime(timezone=True))
    last_updated_date = Column(DateTime(timezone=True))


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

    def upsert_unit(self, id: str, project_name: str, city: str, unit_number: str,
                     locality: str = None, block: str = None, unit_type: str = "plot",
                     width_mtr=None, length_mtr=None, area_sqmt=None, area_sqyd=None,
                     status: str = "available"):
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
                result = db.execute(text(self._q("upsert_unit")), params)
                row = result.mappings().first()
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
                db.execute(text(self._q("expire_stale_units")), {"now": now})
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
    def reserve_unit(self, id: str, broker_id: str, reservation_id: str, reserved_at, reserved_until):
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
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def release_reservation(self, id: str, broker_id: str, target_status: str, outcome: str):
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

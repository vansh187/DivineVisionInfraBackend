"""Admin CSV import (POST /admin/imports/{entity}) - see
DivineService/service_admin_imports.py.

Reads are plain bulk lookups used to validate a whole file up front. Writes
never commit on their own: every write method takes the caller's open session
from ``transaction()``, so one file's customers / brokers / visits / plans and
its Idempotency-Key record land in a single commit, or not at all.

The only new table is divine_admin_import_requests (the Idempotency-Key store);
it is created by the app's startup create_all(), or explicitly by
scripts/add_admin_import_requests_table.py.
"""
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, String, Text, UniqueConstraint, bindparam, text

from .persistence_db import Base, SessionLocal, new_user_ids

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL = timedelta(hours=24)
# Keeps each IN (...) list well under SQLite's bound-parameter limit.
_CHUNK = 500


class AdminImportRequestModel(Base):
    """One successful import per (admin, Idempotency-Key), kept 24 h so a retried
    request replays the stored response instead of writing again."""
    __tablename__ = "divine_admin_import_requests"
    __table_args__ = (UniqueConstraint("admin_id", "idempotency_key", name="uq_admin_import_idem"),)
    id = Column(String(36), primary_key=True)
    admin_id = Column(String(64), nullable=False)
    idempotency_key = Column(String(64), nullable=False)
    entity = Column(String(30), nullable=False)
    request_hash = Column(String(64), nullable=False)
    response_json = Column(Text, nullable=False)
    created_date = Column(DateTime(timezone=True), nullable=False, index=True)


def _utc(value):
    """SQLite hands timestamps back naive; they were written as UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _chunks(values):
    values = list(values)
    for i in range(0, len(values), _CHUNK):
        yield values[i:i + _CHUNK]


# Column allow-lists for the dynamic UPDATE ... SET: only these names ever reach
# the SQL text, never a key taken from the request.
_CUSTOMER_COLUMNS = ("username", "email", "phone", "first_name", "last_name")
_BROKER_COLUMNS = ("username", "email", "phone", "first_name", "last_name", "project")
_VISIT_COLUMNS = (
    "broker_id", "customer_id", "customer_name", "customer_contact", "customer_email", "project_name", "plot_number", "source",
    "assigned_to", "preferred_window", "visit_date", "visit_time", "status", "notes",
)


class persistenceAdminImports:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory

    @contextmanager
    def transaction(self):
        """One session for the whole import: commits on a clean exit, rolls back
        (and re-raises) on any exception."""
        with self._session_factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    # ---- lookups -----------------------------------------------------------
    def _select_in(self, sql: str, values) -> list:
        values = [v for v in dict.fromkeys(values) if v]
        if not values:
            return []
        out = []
        query = text(sql).bindparams(bindparam("values", expanding=True))
        with self._session_factory() as db:
            for chunk in _chunks(values):
                out.extend(dict(r) for r in db.execute(query, {"values": chunk}).mappings().all())
        return out

    def customers_by_id(self, ids) -> dict:
        rows = self._select_in(
            "SELECT id, username, email FROM divine_customer_users WHERE id IN :values", ids)
        return {r["id"]: r for r in rows}

    def customers_using_emails(self, emails) -> list:
        """Customers whose email OR username matches one of ``emails`` (all
        lower-case) - an admin-created account's username is its email, so both
        must stay free for a new one."""
        return self._select_in(
            "SELECT id, username, email FROM divine_customer_users "
            "WHERE lower(email) IN :values OR lower(username) IN :values", emails)

    def brokers_by_id(self, ids) -> dict:
        rows = self._select_in(
            "SELECT id, username, email FROM divine_broker_users WHERE id IN :values", ids)
        return {r["id"]: r for r in rows}

    def brokers_using_emails(self, emails) -> list:
        return self._select_in(
            "SELECT id, username, email FROM divine_broker_users "
            "WHERE lower(email) IN :values OR lower(username) IN :values", emails)

    def customer_ids_by_email(self, emails) -> dict:
        """lower(email) -> customer id, for linking an imported visit to the
        website account it belongs to (what /visits/mine reads)."""
        rows = self._select_in(
            "SELECT id, lower(email) AS email FROM divine_customer_users "
            "WHERE lower(email) IN :values ORDER BY created_date", emails)
        out = {}
        for r in rows:
            out.setdefault(r["email"], r["id"])
        return out

    def visits_by_id(self, ids) -> dict:
        rows = self._select_in(
            "SELECT id, COALESCE(origin_type, 'CHANNEL_PARTNER') AS origin_type, customer_id "
            "FROM divine_site_visits WHERE id IN :values", ids)
        return {r["id"]: r for r in rows}

    # ---- idempotency -------------------------------------------------------
    def get_request(self, admin_id: str, idempotency_key: str):
        """The stored (request_hash, response_json) for this admin's key, or None
        when there is none or it is older than 24 h."""
        with self._session_factory() as db:
            row = db.execute(text(
                "SELECT request_hash, response_json, created_date FROM divine_admin_import_requests "
                "WHERE admin_id = :admin_id AND idempotency_key = :key"
            ), {"admin_id": admin_id, "key": idempotency_key}).mappings().first()
        if not row or _utc(row["created_date"]) < datetime.now(timezone.utc) - IDEMPOTENCY_TTL:
            return None
        return dict(row)

    def save_request(self, db, admin_id: str, idempotency_key: str, entity: str,
                     request_hash: str, response_json: str) -> None:
        """Inside the import's own transaction, so the key exists if and only if
        the rows were written. A concurrent request with the same key fails here
        on the unique constraint and rolls its writes back."""
        now = datetime.now(timezone.utc)
        # Only this admin's own expired use of the key (frees it for reuse after
        # 24 h) - one row at most, so concurrent imports never wait on each
        # other here. Everyone else's expired rows: purge_expired().
        db.execute(text(
            "DELETE FROM divine_admin_import_requests "
            "WHERE admin_id = :admin_id AND idempotency_key = :key AND created_date < :cutoff"
        ), {"admin_id": admin_id, "key": idempotency_key, "cutoff": now - IDEMPOTENCY_TTL})
        db.execute(text(
            "INSERT INTO divine_admin_import_requests "
            "(id, admin_id, idempotency_key, entity, request_hash, response_json, created_date) "
            "VALUES (:id, :admin_id, :key, :entity, :hash, :response, :now)"
        ), {"id": str(uuid.uuid4()), "admin_id": admin_id, "key": idempotency_key, "entity": entity,
            "hash": request_hash, "response": response_json, "now": now})

    def purge_expired(self) -> None:
        """Drops every key older than 24 h, in its own short transaction (run
        after an import commits, never inside one). Best effort."""
        try:
            with self.transaction() as db:
                db.execute(text("DELETE FROM divine_admin_import_requests WHERE created_date < :cutoff"),
                           {"cutoff": datetime.now(timezone.utc) - IDEMPOTENCY_TTL})
        except Exception:
            logger.warning("admin_import_purge_expired_failed", exc_info=True)

    # ---- writes (caller's session, no commit) -------------------------------
    @staticmethod
    def _update(db, table: str, allowed: tuple, row_id: str, changes: dict, extra: dict) -> None:
        sets = [c for c in allowed if c in changes]
        params = {c: changes[c] for c in sets}
        params.update(extra)
        params["row_id"] = row_id
        assignments = ", ".join(f"{c} = :{c}" for c in [*sets, *extra])
        db.execute(text(f"UPDATE {table} SET {assignments} WHERE id = :row_id"), params)

    def insert_customers(self, db, customers: list, created_by: str) -> None:
        if not customers:
            return
        now = datetime.now(timezone.utc)
        ids = new_user_ids(db, "C", len(customers))
        db.execute(text(
            "INSERT INTO divine_customer_users (id, username, first_name, last_name, email, phone, "
            "password_hash, source, created_by, created_date, last_updated_by, last_updated_date) "
            "VALUES (:id, :username, :first_name, :last_name, :email, :phone, :password_hash, "
            "'website', :by, :now, :by, :now)"
        ), [{**c, "id": i, "by": created_by, "now": now} for i, c in zip(ids, customers)])

    def update_customer(self, db, customer_id: str, changes: dict, updated_by: str) -> None:
        self._update(db, "divine_customer_users", _CUSTOMER_COLUMNS, customer_id, changes,
                     {"last_updated_by": updated_by, "last_updated_date": datetime.now(timezone.utc)})

    def insert_brokers(self, db, brokers: list, created_by: str) -> None:
        if not brokers:
            return
        now = datetime.now(timezone.utc)
        ids = new_user_ids(db, "B", len(brokers))
        db.execute(text(
            "INSERT INTO divine_broker_users (id, username, first_name, last_name, email, phone, project, "
            "password_hash, created_by, created_date, last_updated_by, last_updated_date) "
            "VALUES (:id, :username, :first_name, :last_name, :email, :phone, :project, :password_hash, "
            ":by, :now, :by, :now)"
        ), [{**b, "id": i, "by": created_by, "now": now} for i, b in zip(ids, brokers)])

    def update_broker(self, db, broker_id: str, changes: dict, updated_by: str) -> None:
        self._update(db, "divine_broker_users", _BROKER_COLUMNS, broker_id, changes,
                     {"last_updated_by": updated_by, "last_updated_date": datetime.now(timezone.utc)})

    def insert_visits(self, db, visits: list) -> None:
        if not visits:
            return
        now = datetime.now(timezone.utc)
        params = []
        for v in visits:
            p = {c: v.get(c) for c in _VISIT_COLUMNS}
            p.update({"id": str(uuid.uuid4()), "origin_type": v["origin_type"], "now": now})
            params.append(p)
        db.execute(text(
            "INSERT INTO divine_site_visits (id, origin_type, broker_id, customer_id, customer_name, customer_contact, customer_email, "
            "project_name, plot_number, source, assigned_to, preferred_window, visit_date, visit_time, status, "
            "notes, created_date, last_updated_date) "
            "VALUES (:id, :origin_type, :broker_id, :customer_id, :customer_name, :customer_contact, :customer_email, :project_name, "
            ":plot_number, :source, :assigned_to, :preferred_window, :visit_date, :visit_time, :status, "
            ":notes, :now, :now)"
        ), params)

    def update_visit(self, db, visit_id: str, changes: dict) -> None:
        self._update(db, "divine_site_visits", _VISIT_COLUMNS, visit_id, changes,
                     {"last_updated_date": datetime.now(timezone.utc)})

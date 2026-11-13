"""Payment-plan catalogue (PP1/PP2/... chosen by a customer at booking) and
per-project settings (Offer of Possession date).

A plan is never edited in place once customers use it: a change is loaded as a
new ``version`` of the same ``code`` and only the newest version is active.
Bookings keep a snapshot of the version they chose (form_data.payment_plan).

Reads may raise on a genuine DB fault (the service layer degrades); writes roll
back and re-raise. Every session is opened in a ``with`` block.
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import (Boolean, Column, Date, DateTime, Integer, Numeric, String, Text,
                        UniqueConstraint, bindparam, text)

from .persistence_db import Base, SessionLocal, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class PaymentPlanModel(Base):
    __tablename__ = "divine_payment_plans"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_payment_plan_code_version"),)
    id = Column(String(36), primary_key=True)
    code = Column(String(80), nullable=False, index=True)
    short_code = Column(String(20), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(Text)
    project_id = Column(String(100), index=True)          # NULL = every project
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    created_date = Column(DateTime(timezone=True))
    last_updated_date = Column(DateTime(timezone=True))


class PaymentPlanInstalmentModel(Base):
    __tablename__ = "divine_payment_plan_instalments"
    __table_args__ = (UniqueConstraint("plan_id", "seq", name="uq_payment_plan_instalment_seq"),)
    id = Column(String(36), primary_key=True)
    plan_id = Column(String(36), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    label = Column(String(150), nullable=False)
    percent = Column(Numeric(6, 3), nullable=False)
    due_trigger = Column(String(12), nullable=False, default="booking")   # booking | possession | custom
    due_days = Column(Integer)                                            # NULL = on the trigger date
    charge_head = Column(String(10))                                      # NULL | PLC | IFMS | CAM | ALL
    due_from = Column(String(100))                                        # custom trigger: event name


class ProjectSettingsModel(Base):
    __tablename__ = "divine_project_settings"
    project_id = Column(String(100), primary_key=True)
    possession_offer_date = Column(Date)
    updated_by = Column(String(255))
    updated_date = Column(DateTime(timezone=True))


_DEFAULT_QUERIES = {
    "list_active_plans": (
        "SELECT * FROM divine_payment_plans WHERE is_active = :active "
        "AND (project_id IS NULL OR :project_id IS NULL OR project_id = :project_id) "
        "ORDER BY sort_order ASC, code ASC;"
    ),
    "get_active_plan_by_code": (
        "SELECT * FROM divine_payment_plans WHERE code = :code AND is_active = :active "
        "ORDER BY version DESC LIMIT 1;"
    ),
    "get_plan_by_code_version": (
        "SELECT * FROM divine_payment_plans WHERE code = :code AND version = :version LIMIT 1;"
    ),
    "list_instalments": (
        "SELECT * FROM divine_payment_plan_instalments WHERE plan_id = :plan_id ORDER BY seq ASC;"
    ),
    "insert_plan": (
        "INSERT INTO divine_payment_plans (id, code, short_code, name, description, project_id, "
        "is_active, sort_order, version, created_date, last_updated_date) "
        "VALUES (:id, :code, :short_code, :name, :description, :project_id, :is_active, :sort_order, "
        ":version, :now, :now);"
    ),
    "insert_instalment": (
        "INSERT INTO divine_payment_plan_instalments (id, plan_id, seq, label, percent, due_trigger, "
        "due_days, charge_head, due_from) VALUES (:id, :plan_id, :seq, :label, :percent, :due_trigger, "
        ":due_days, :charge_head, :due_from);"
    ),
    "deactivate_other_versions": (
        "UPDATE divine_payment_plans SET is_active = :inactive, last_updated_date = :now "
        "WHERE code = :code AND version <> :version;"
    ),
    "update_plan_meta": (
        "UPDATE divine_payment_plans SET short_code = :short_code, name = :name, "
        "description = :description, project_id = :project_id, is_active = :is_active, "
        "sort_order = :sort_order, last_updated_date = :now WHERE id = :id;"
    ),
    "list_all_plans": (
        "SELECT * FROM divine_payment_plans "
        "WHERE (:project_id IS NULL OR project_id IS NULL OR project_id = :project_id) "
        "ORDER BY sort_order ASC, code ASC, version DESC;"
    ),
    "list_versions": "SELECT * FROM divine_payment_plans WHERE code = :code ORDER BY version DESC;",
    "list_plan_codes": "SELECT code, short_code, is_active FROM divine_payment_plans;",
    "set_active": (
        "UPDATE divine_payment_plans SET is_active = :active, last_updated_date = :now "
        "WHERE code = :code AND version = :version;"
    ),
    # Plan code/version of every active booking (KYC review pending or booked) on
    # the given plan codes, read from the booking payment's notes.payment_plan.
    # Only these two small values come back, never the whole notes. Postgres
    # (jsonb: a non-object value yields NULL) and SQLite (test DB: json_valid
    # guards malformed text) spell the JSON access differently. Read-only.
    "list_active_booking_plan_refs_postgresql": (
        "SELECT DISTINCT t.inventory_id, t.plan_code, t.plan_version FROM ("
        "SELECT b.inventory_id, p.notes -> 'payment_plan' ->> 'payment_plan_code' AS plan_code, "
        "p.notes -> 'payment_plan' ->> 'payment_plan_version' AS plan_version "
        "FROM divine_bookings b JOIN divine_payments p ON p.id = b.payment_id "
        "WHERE b.status IN ('pending_kyc_review', 'booked') "
        # Offline buyers' plots linked to a plan (service_plot_payment_terms).
        "UNION ALL SELECT d.form_data ->> 'inventory_id', d.form_data -> 'payment_plan' ->> 'code', "
        "d.form_data -> 'payment_plan' ->> 'version' "
        "FROM divine_documents d WHERE d.status = 'imported' AND d.form_data ->> 'inventory_id' IS NOT NULL"
        ") t WHERE t.plan_code IN :codes;"
    ),
    "list_active_booking_plan_refs_sqlite": (
        "SELECT DISTINCT t.inventory_id, t.plan_code, t.plan_version FROM ("
        "SELECT b.inventory_id, "
        "CASE WHEN json_valid(p.notes) THEN json_extract(p.notes, '$.payment_plan.payment_plan_code') END AS plan_code, "
        "CASE WHEN json_valid(p.notes) THEN json_extract(p.notes, '$.payment_plan.payment_plan_version') END AS plan_version "
        "FROM divine_bookings b JOIN divine_payments p ON p.id = b.payment_id "
        "WHERE b.status IN ('pending_kyc_review', 'booked') "
        "UNION ALL SELECT json_extract(d.form_data, '$.inventory_id'), "
        "json_extract(d.form_data, '$.payment_plan.code'), json_extract(d.form_data, '$.payment_plan.version') "
        "FROM divine_documents d WHERE d.status = 'imported' AND json_valid(d.form_data) "
        "AND json_extract(d.form_data, '$.inventory_id') IS NOT NULL"
        ") t WHERE t.plan_code IN :codes;"
    ),
    "list_versions_for_codes": "SELECT code, version, is_active FROM divine_payment_plans WHERE code IN :codes;",
    "get_project_settings": "SELECT * FROM divine_project_settings WHERE project_id = :project_id LIMIT 1;",
    "insert_project_settings": (
        "INSERT INTO divine_project_settings (project_id, possession_offer_date, updated_by, updated_date) "
        "VALUES (:project_id, :possession_offer_date, :updated_by, :now);"
    ),
    "update_project_settings": (
        "UPDATE divine_project_settings SET possession_offer_date = :possession_offer_date, "
        "updated_by = :updated_by, updated_date = :now WHERE project_id = :project_id;"
    ),
}


def _plan_version(value):
    """A stored plan version as int; None when absent. 1, "1", " 2 ", "2.0" and
    2.0 are all readable; anything else (0, -1, "1.5", "v2", true) raises
    ValueError - the caller skips it rather than guessing a version."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_plan_version")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("invalid_plan_version")
    if not number.is_finite() or number != number.to_integral_value() or number < 1:
        raise ValueError("invalid_plan_version")
    return int(number)


class persistencePaymentPlan:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("payment_plan_queries.yaml")
        for name, sql in _DEFAULT_QUERIES.items():
            queries.setdefault(name, sql)
        self._queries = queries

    # ---- reads -------------------------------------------------------------
    def list_active(self, project_id: str = None) -> list:
        """Active plans (newest version of each code) for a project, each as a
        dict with an ``instalments`` list, in display order."""
        with self._session_factory() as db:
            plans = db.execute(text(self._queries["list_active_plans"]),
                               {"active": True, "project_id": project_id}).mappings().all()
            out = []
            for p in plans:
                plan = dict(p)
                plan["instalments"] = [dict(r) for r in db.execute(
                    text(self._queries["list_instalments"]), {"plan_id": plan["id"]}).mappings().all()]
                out.append(plan)
            return out

    def get_by_code(self, code: str, version: int = None):
        """A plan dict with instalments, or None. Without ``version`` only the
        active plan is returned; with it, that exact (possibly retired) version."""
        with self._session_factory() as db:
            if version is None:
                row = db.execute(text(self._queries["get_active_plan_by_code"]),
                                 {"code": code, "active": True}).mappings().first()
            else:
                row = db.execute(text(self._queries["get_plan_by_code_version"]),
                                 {"code": code, "version": int(version)}).mappings().first()
            if not row:
                return None
            plan = dict(row)
            plan["instalments"] = [dict(r) for r in db.execute(
                text(self._queries["list_instalments"]), {"plan_id": plan["id"]}).mappings().all()]
            return plan

    def list_all(self, project_id: str = None) -> list:
        """Every plan version (active and retired) with instalments - admin view."""
        with self._session_factory() as db:
            plans = db.execute(text(self._queries["list_all_plans"]),
                               {"project_id": project_id}).mappings().all()
            out = []
            for p in plans:
                plan = dict(p)
                plan["instalments"] = [dict(r) for r in db.execute(
                    text(self._queries["list_instalments"]), {"plan_id": plan["id"]}).mappings().all()]
                out.append(plan)
            return out

    def list_plan_codes(self) -> list:
        """code / short_code / is_active of every plan version - no instalments."""
        with self._session_factory() as db:
            return [dict(r) for r in db.execute(text(self._queries["list_plan_codes"])).mappings().all()]

    def list_versions(self, code: str) -> list:
        """All versions of one plan code, newest first (no instalments)."""
        with self._session_factory() as db:
            return [dict(r) for r in db.execute(text(self._queries["list_versions"]), {"code": code}).mappings().all()]

    def set_active(self, code: str, version: int, active: bool) -> None:
        """Activate one version (retiring the others) or deactivate it."""
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                db.execute(text(self._queries["set_active"]),
                           {"active": bool(active), "now": now, "code": code, "version": int(version)})
                if active:
                    db.execute(text(self._queries["deactivate_other_versions"]),
                               {"inactive": False, "now": now, "code": code, "version": int(version)})
                db.commit()
            except Exception:
                db.rollback()
                raise

    def count_plots_by_plan(self, codes) -> dict:
        """{(code, version): number of distinct plots} with an active booking
        (pending KYC review or booked), or an offline buyer's plot linked to the
        plan, on each of ``codes``. Computed live on every call.

        Terms without a version are put on the version a booking without one
        resolves to everywhere else (``get_by_code(code)``): the code's active
        version - or, when none is active, its newest. All versions of the code
        are considered, whatever project they belong to. A plot is counted once
        per version even if it appears both with and without a version. Terms
        whose version is unreadable are skipped (logged), never moved to
        another version."""
        codes = sorted({str(c) for c in codes or () if c})
        if not codes:
            return {}
        with self._session_factory() as db:
            dialect = db.get_bind().dialect.name
            key = ("list_active_booking_plan_refs_postgresql" if dialect == "postgresql"
                   else "list_active_booking_plan_refs_sqlite")
            refs = db.execute(text(self._queries[key]).bindparams(bindparam("codes", expanding=True)),
                              {"codes": codes}).mappings().all()
            if not refs:
                return {}
            versions = db.execute(
                text(self._queries["list_versions_for_codes"]).bindparams(bindparam("codes", expanding=True)),
                {"codes": codes}).mappings().all()
        resolved = {}                                    # code -> (is_active, version): max = active, else newest
        for v in versions:
            rank = (bool(v["is_active"]), int(v["version"]))
            resolved[v["code"]] = max(resolved.get(v["code"], rank), rank)
        plots, unreadable = {}, 0
        for ref in refs:
            code, inventory_id = ref["plan_code"], ref["inventory_id"]
            if not code or not inventory_id:
                continue
            try:
                version = _plan_version(ref["plan_version"])
            except ValueError:
                unreadable += 1
                continue
            if version is None:
                if code not in resolved:
                    continue
                version = resolved[code][1]
            plots.setdefault((str(code), version), set()).add(str(inventory_id))
        if unreadable:
            logger.warning("payment_plan_plot_counts_unreadable_version rows=%d", unreadable)
        return {key: len(ids) for key, ids in plots.items()}

    def get_project_settings(self, project_id: str):
        with self._session_factory() as db:
            row = db.execute(text(self._queries["get_project_settings"]),
                             {"project_id": project_id}).mappings().first()
            return RowWrapper(row) if row else None

    # ---- writes ------------------------------------------------------------
    def upsert_plan(self, plan: dict) -> str:
        """Create ``plan`` (code + version) with its instalments, or refresh the
        display fields of an existing (code, version). Instalments of an existing
        version are never rewritten - change them by loading a new version.
        Returns 'created' | 'updated'."""
        with self._session_factory() as db:
            try:
                result = self.write_plan(db, plan)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def write_plan(self, db, plan: dict) -> str:
        """upsert_plan's writes inside the caller's open session - never commits,
        so a caller can save several plans in one transaction (the admin CSV
        import, which must write all of its plans or none)."""
        now = datetime.now(timezone.utc)
        version = int(plan.get("version") or 1)
        existing = db.execute(text(self._queries["get_plan_by_code_version"]),
                              {"code": plan["code"], "version": version}).mappings().first()
        meta = {
            "short_code": plan["short_code"], "name": plan["name"],
            "description": plan.get("description"), "project_id": plan.get("project_id"),
            "is_active": bool(plan.get("is_active", True)),
            "sort_order": int(plan.get("sort_order") or 0), "now": now,
        }
        if existing:
            db.execute(text(self._queries["update_plan_meta"]), {**meta, "id": existing["id"]})
            result = "updated"
        else:
            plan_id = str(uuid.uuid4())
            db.execute(text(self._queries["insert_plan"]),
                       {**meta, "id": plan_id, "code": plan["code"], "version": version})
            for row in plan["instalments"]:
                db.execute(text(self._queries["insert_instalment"]), {
                    "id": str(uuid.uuid4()), "plan_id": plan_id, "seq": int(row["seq"]),
                    "label": row["label"], "percent": float(row["percent"]),
                    "due_trigger": row.get("due_trigger") or "booking",
                    "due_days": row.get("due_days"), "charge_head": row.get("charge_head"),
                    "due_from": row.get("due_from") if row.get("due_trigger") == "custom" else None,
                })
            result = "created"
        if meta["is_active"]:
            db.execute(text(self._queries["deactivate_other_versions"]),
                       {"inactive": False, "now": now, "code": plan["code"], "version": version})
        return result

    def set_possession_date(self, project_id: str, possession_date, updated_by: str):
        with self._session_factory() as db:
            try:
                params = {"project_id": project_id, "possession_offer_date": possession_date,
                          "updated_by": updated_by, "now": datetime.now(timezone.utc)}
                exists = db.execute(text(self._queries["get_project_settings"]),
                                    {"project_id": project_id}).first()
                db.execute(text(self._queries["update_project_settings" if exists
                                              else "insert_project_settings"]), params)
                db.commit()
                row = db.execute(text(self._queries["get_project_settings"]),
                                 {"project_id": project_id}).mappings().first()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

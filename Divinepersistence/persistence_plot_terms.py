"""Payment terms of a customer's plots: the payment plan, booking date, prices
and amount received of an offline buyer's plot, kept on the plot's booking
application record (divine_documents, status 'imported') in the same
form_data keys a website booking uses, with its instalments in
divine_payment_milestones (booking_id = the record's id).

No new tables. See DivineService/service_plot_payment_terms.py.
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import bindparam, text

from .persistence_db import SessionLocal

logger = logging.getLogger(__name__)

_BOOKING_DOC_TYPES = ("booking_application", "project_booking_application")
_CHUNK = 500


def _form(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class persistencePlotTerms:
    def __init__(self, session_factory=SessionLocal, milestone_persistence=None):
        self._session_factory = session_factory
        if milestone_persistence is None:
            from .persistence_milestone import persistenceMilestone
            milestone_persistence = persistenceMilestone(session_factory=session_factory)
        self._milestones = milestone_persistence

    # ---- reads -------------------------------------------------------------
    def booking_docs_for_customers(self, customer_ids) -> list:
        """Every booking application record of these customers, oldest first,
        with form_data parsed."""
        ids = [i for i in dict.fromkeys(customer_ids or ()) if i]
        out = []
        query = text(
            "SELECT id, owner_id, status, project_id, payment_id, form_data, created_date "
            "FROM divine_documents WHERE owner_role = 'customer' AND owner_id IN :ids "
            "AND document_type IN :types ORDER BY created_date ASC, id ASC"
        ).bindparams(bindparam("ids", expanding=True), bindparam("types", expanding=True))
        with self._session_factory() as db:
            for i in range(0, len(ids), _CHUNK):
                rows = db.execute(query, {"ids": ids[i:i + _CHUNK], "types": list(_BOOKING_DOC_TYPES)})
                for r in rows.mappings().all():
                    doc = dict(r)
                    doc["form_data"] = _form(doc["form_data"])
                    out.append(doc)
        return out

    def get_booking_doc(self, document_id: str):
        query = text(
            "SELECT id, owner_id, owner_role, status, project_id, payment_id, form_data, created_date "
            "FROM divine_documents WHERE id = :id AND document_type IN :types"
        ).bindparams(bindparam("types", expanding=True))
        with self._session_factory() as db:
            row = db.execute(query, {"id": document_id, "types": list(_BOOKING_DOC_TYPES)}).mappings().first()
        if not row:
            return None
        doc = dict(row)
        doc["form_data"] = _form(doc["form_data"])
        return doc

    def customer_units(self, customer_id: str) -> list:
        """Plots linked to this buyer (divine_project_inventory.booked_by)."""
        return self.units_for_customers([customer_id])

    def bookings_for_payments(self, payment_ids) -> dict:
        """payment id -> its divine_bookings row (status, plot) for these payments."""
        ids = [i for i in dict.fromkeys(payment_ids or ()) if i]
        out = {}
        query = text(
            "SELECT payment_id, status, inventory_id, project_name, unit_number FROM divine_bookings "
            "WHERE payment_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        with self._session_factory() as db:
            for i in range(0, len(ids), _CHUNK):
                for r in db.execute(query, {"ids": ids[i:i + _CHUNK]}).mappings().all():
                    out[r["payment_id"]] = dict(r)
        return out

    def units_for_customers(self, customer_ids) -> list:
        """Plots these customers currently hold (divine_project_inventory.booked_by:
        an offline buyer's plot, or a website booking under review / booked)."""
        ids = [i for i in dict.fromkeys(customer_ids or ()) if i]
        out = []
        query = text(
            "SELECT id, project_name, unit_number, status, booked_by FROM divine_project_inventory "
            "WHERE booked_by IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        with self._session_factory() as db:
            for i in range(0, len(ids), _CHUNK):
                out.extend(dict(r) for r in db.execute(query, {"ids": ids[i:i + _CHUNK]}).mappings().all())
        return out

    def project_units(self, project_name: str) -> list:
        """Every plot of a project, with its linked buyer (booked_by)."""
        with self._session_factory() as db:
            rows = db.execute(text(
                "SELECT id, project_name, unit_number, status, booked_by FROM divine_project_inventory "
                "WHERE lower(project_name) = lower(:p)"), {"p": project_name}).mappings().all()
        return [dict(r) for r in rows]

    def milestones_for(self, booking_id: str) -> list:
        return [dict(vars(r)) for r in self._milestones.list_for_booking(booking_id)]

    def get_customer(self, customer_id: str):
        with self._session_factory() as db:
            row = db.execute(text(
                "SELECT id, first_name, last_name, email, phone, source, created_date "
                "FROM divine_customer_users WHERE id = :id"), {"id": customer_id}).mappings().first()
        return dict(row) if row else None

    # ---- the write ---------------------------------------------------------
    def save_terms(self, document_id: str, form_data: dict, milestone_rows: list) -> None:
        """In one transaction: the record's new form_data, and its milestones
        replaced by ``milestone_rows`` (none = no schedule yet). Refuses with
        ValueError('schedule_has_payments') when a milestone was settled by a
        payment in the app - those are never rewritten."""
        with self._session_factory() as db:
            try:
                paid_in_app = db.execute(text(
                    "SELECT 1 FROM divine_payment_milestones WHERE booking_id = :b "
                    "AND paid_payment_id IS NOT NULL LIMIT 1"), {"b": document_id}).first()
                if paid_in_app:
                    raise ValueError("schedule_has_payments")
                postgres = db.get_bind().dialect.name == "postgresql"
                form_sql = "CAST(:form AS jsonb)" if postgres else ":form"
                db.execute(text(
                    f"UPDATE divine_documents SET form_data = {form_sql}, last_updated_date = :now WHERE id = :id"
                ), {"form": json.dumps(form_data, default=str), "now": datetime.now(timezone.utc),
                    "id": document_id})
                db.execute(text("DELETE FROM divine_payment_milestones WHERE booking_id = :b"),
                           {"b": document_id})
                if milestone_rows:
                    self._milestones.write_milestones(db, milestone_rows)
                db.commit()
            except Exception:
                db.rollback()
                raise

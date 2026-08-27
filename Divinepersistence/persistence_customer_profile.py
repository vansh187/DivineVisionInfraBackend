import json
import logging

from sqlalchemy import text

from .persistence_db import SessionLocal, engine, RowWrapper, load_queries


class persistenceCustomerProfile:
    """Read-only persistence used to assemble ``GET /customer/profile``.

    Design contract for this class:

    * Purely instance based - no static/class methods, no module-level SQL helpers.
    * Every public method opens its own short-lived session, runs a single
      parameterised statement (no string interpolation into SQL) and closes it.
    * No method raises: a failure is logged and a safe empty value is returned so
      that a transient problem in one section still lets the rest of the profile
      render. The profile contract explicitly allows a partial payload.
    """

    # Fallback SQL, used only when the YAML query file cannot be read. Kept in
    # sync with DivineDatabasequeries/customer_queries.yaml.
    _DEFAULT_QUERIES = {
        "profile_get_customer": (
            "SELECT id, username, email, phone, first_name, last_name "
            "FROM divine_customer_users WHERE id = :customer_id LIMIT 1;"
        ),
        "profile_get_identity_extract": (
            "SELECT extracted_data FROM divine_kyc_verifications "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' "
            "AND verified = :verified ORDER BY created_date DESC LIMIT 1;"
        ),
        "profile_get_booking_application": (
            "SELECT id, project_id, form_data, created_date FROM divine_documents "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' "
            "AND document_type = 'booking_application' "
            "ORDER BY created_date DESC LIMIT 1;"
        ),
        "profile_get_amount_received": (
            "SELECT COALESCE(SUM(amount), 0) AS amount_received FROM divine_payments "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' "
            "AND status = 'paid';"
        ),
        "profile_count_booking_projects": (
            "SELECT COUNT(DISTINCT project_id) AS project_count FROM divine_documents "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' "
            "AND document_type = 'booking_application' AND project_id IS NOT NULL;"
        ),
        "profile_get_amount_received_for_project": (
            "SELECT COALESCE(SUM(amount), 0) AS amount_received FROM divine_payments "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' AND status = 'paid' "
            "AND id IN (SELECT payment_id FROM divine_documents "
            "WHERE owner_id = :customer_id AND owner_role = 'customer' "
            "AND document_type = 'booking_application' AND project_id = :project_id "
            "AND payment_id IS NOT NULL);"
        ),
    }

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._engine = engine
        self._logger = logging.getLogger(__name__)
        try:
            queries = load_queries("customer_queries.yaml") or {}
        except Exception:
            queries = {}
        for name, sql in self._DEFAULT_QUERIES.items():
            queries.setdefault(name, sql)
        self._queries = queries

    # -- internal helpers --------------------------------------------------

    def _query(self, name):
        return self._queries.get(name) or self._DEFAULT_QUERIES[name]

    def _as_dict(self, value):
        """Coerce a JSON column value into a dict.

        Postgres ``jsonb`` comes back already decoded; SQLite stores the same
        payload as a TEXT string. Either shape - or a bad one - yields ``{}``
        rather than an exception.
        """
        try:
            if value is None:
                return {}
            if isinstance(value, dict):
                return value
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", errors="ignore")
            if isinstance(value, str):
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
        return {}

    # -- public API ------------------------------------------------------

    def get_customer(self, customer_id):
        """Return the base customer row wrapper, or ``None`` if absent/unreadable."""
        try:
            with self._session_factory() as db:
                row = db.execute(
                    text(self._query("profile_get_customer")),
                    {"customer_id": customer_id},
                ).mappings().first()
                return RowWrapper(row) if row else None
        except Exception:
            self._logger.warning(
                "profile_get_customer_failed customer_id=%s", customer_id, exc_info=True
            )
            return None

    def get_identity_extract(self, customer_id):
        """Return the newest verified KYC ``extracted_data`` dict, or ``{}``."""
        try:
            with self._session_factory() as db:
                row = db.execute(
                    text(self._query("profile_get_identity_extract")),
                    {"customer_id": customer_id, "verified": True},
                ).mappings().first()
                if not row:
                    return {}
                return self._as_dict(row.get("extracted_data"))
        except Exception:
            self._logger.warning(
                "profile_get_identity_extract_failed customer_id=%s",
                customer_id,
                exc_info=True,
            )
            return {}

    def get_booking_application(self, customer_id):
        """Return the newest booking-application document as a plain dict, or ``None``."""
        try:
            with self._session_factory() as db:
                row = db.execute(
                    text(self._query("profile_get_booking_application")),
                    {"customer_id": customer_id},
                ).mappings().first()
                if not row:
                    return None
                return {
                    "id": row.get("id"),
                    "project_id": row.get("project_id"),
                    "form_data": self._as_dict(row.get("form_data")),
                    "created_date": row.get("created_date"),
                }
        except Exception:
            self._logger.warning(
                "profile_get_booking_application_failed customer_id=%s",
                customer_id,
                exc_info=True,
            )
            return None

    def get_amount_received(self, customer_id):
        """Return the sum of the customer's paid payments in whole rupees (``0`` on failure)."""
        return self._sum_amount(
            "profile_get_amount_received", {"customer_id": customer_id}, customer_id
        )

    def get_amount_received_for_project(self, customer_id, project_id):
        """Return the paid total (whole rupees) linked - via booking-application
        documents - to a single ``project_id`` (``0`` on failure)."""
        return self._sum_amount(
            "profile_get_amount_received_for_project",
            {"customer_id": customer_id, "project_id": project_id},
            customer_id,
        )

    def count_booking_projects(self, customer_id):
        """Return how many distinct projects the customer holds a booking application for."""
        try:
            with self._session_factory() as db:
                row = db.execute(
                    text(self._query("profile_count_booking_projects")),
                    {"customer_id": customer_id},
                ).mappings().first()
                if not row:
                    return 0
                count = row.get("project_count")
                return int(count) if count is not None else 0
        except Exception:
            self._logger.warning(
                "profile_count_booking_projects_failed customer_id=%s",
                customer_id,
                exc_info=True,
            )
            return 0

    def _sum_amount(self, query_name, params, customer_id):
        try:
            with self._session_factory() as db:
                row = db.execute(
                    text(self._query(query_name)), params
                ).mappings().first()
                if not row:
                    return 0
                total = row.get("amount_received")
                return int(round(float(total))) if total is not None else 0
        except Exception:
            self._logger.warning(
                "%s_failed customer_id=%s", query_name, customer_id, exc_info=True
            )
            return 0

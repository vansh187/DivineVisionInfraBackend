import logging
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)

# Only these exact strings may reach the query's {order_by} placeholder - never
# build it from raw request input, since SQL doesn't allow parameter-binding an
# identifier/direction the way it does a value.
_SORT_COLUMNS = {
    "visit_date": "visit_date ASC, visit_time ASC",
    "-visit_date": "visit_date DESC, visit_time DESC",
    "created_at": "created_at ASC",
    "-created_at": "created_at DESC",
    "customer_name": "customer_name ASC",
    "-customer_name": "customer_name DESC",
}
_DEFAULT_SORT = "-visit_date"


class persistenceAdminVisits:
    """Read-only: lists/reads divine_site_visits for the admin panel's Site
    Visits page. No writes here - visits are created via the existing broker
    POST /visits flow (DivineAPI/visit_api.py); this class only ever displays."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("admin_visits_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def _order_by(self, sort: str) -> str:
        return _SORT_COLUMNS.get(sort, _SORT_COLUMNS[_DEFAULT_SORT])

    def list_visits(self, search: str = None, origin_type: str = None, status: str = None,
                     sort: str = _DEFAULT_SORT, limit: int = 20, offset: int = 0):
        """Rows carry a `total_count` attribute (a window-function total over the
        whole filtered set, not just this page) so the common case - a page with
        results - never needs a second query to know total_items. Only an empty
        page falls back to count_visits()."""
        params = {
            "search": (search or None), "origin_type": origin_type, "status": status,
            "limit": limit, "offset": offset,
        }
        try:
            query = self._q("list_visits").format(order_by=self._order_by(sort))
            with self._session_factory() as db:
                result = db.execute(text(query), params)
                return [RowWrapper(row) for row in result.mappings().all()]
        except SQLAlchemyError:
            logger.exception("admin_list_visits_query_failed")
            raise RuntimeError("db_error")

    def count_visits(self, search: str = None, origin_type: str = None, status: str = None) -> int:
        params = {"search": (search or None), "origin_type": origin_type, "status": status}
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("count_visits")), params)
                row = result.mappings().first()
                return int(row["total"]) if row else 0
        except SQLAlchemyError:
            logger.exception("admin_count_visits_query_failed")
            raise RuntimeError("db_error")

    def get_visit_by_id(self, visit_id: str):
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("get_visit_by_id")), {"id": visit_id})
                row = result.mappings().first()
                if not row:
                    return None
                return RowWrapper(row)
        except SQLAlchemyError:
            logger.exception("admin_get_visit_query_failed")
            raise RuntimeError("db_error")

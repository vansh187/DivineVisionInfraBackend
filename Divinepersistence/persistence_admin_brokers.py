from sqlalchemy import text
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries

# Only these two exact strings may reach the query's {order_by} placeholder - never
# build it from raw request input, since SQL doesn't allow parameter-binding an
# identifier/direction the way it does a value.
_SORT_COLUMNS = {
    "created_at": "created_at ASC",
    "-created_at": "created_at DESC",
    "full_name": "full_name ASC",
    "-full_name": "full_name DESC",
}
_DEFAULT_SORT = "-created_at"


class persistenceAdminBrokers:
    """Read-only: lists divine_broker_users for the admin panel's Brokers page.
    Brokers already self-register via /broker/signup, so unlike customers there
    is no admin-side create here."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("admin_brokers_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def _order_by(self, sort: str) -> str:
        return _SORT_COLUMNS.get(sort, _SORT_COLUMNS[_DEFAULT_SORT])

    def list_brokers(self, search: str = None, project: str = None,
                      sort: str = _DEFAULT_SORT, limit: int = 20, offset: int = 0):
        """Rows carry a `total_count` attribute (a window-function total over the
        whole filtered set, not just this page) so the common case - a page with
        results - never needs a second query to know total_items. Only an empty
        page (genuinely no matches, or past the last page) has nowhere to read
        that total from; the caller falls back to count_brokers() then."""
        params = {
            "search": (search or None), "project": project,
            "limit": limit, "offset": offset,
        }
        query = self._q("list_brokers").format(order_by=self._order_by(sort))
        with self._session_factory() as db:
            result = db.execute(text(query), params)
            return [RowWrapper(row) for row in result.mappings().all()]

    def count_brokers(self, search: str = None, project: str = None) -> int:
        params = {"search": (search or None), "project": project}
        with self._session_factory() as db:
            result = db.execute(text(self._q("count_brokers")), params)
            row = result.mappings().first()
            return int(row["total"]) if row else 0

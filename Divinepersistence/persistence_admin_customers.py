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


class persistenceAdminCustomers:
    """Read-only: lists divine_customer_users for the admin panel's Customers
    page. Creating a customer (POST /admin/customers) goes through the existing
    persistenceCustomer instead - see DivineService/service_admin_customers.py."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("admin_customers_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def _order_by(self, sort: str) -> str:
        return _SORT_COLUMNS.get(sort, _SORT_COLUMNS[_DEFAULT_SORT])

    def list_customers(self, search: str = None, source: str = None, status: str = None,
                        sort: str = _DEFAULT_SORT, limit: int = 20, offset: int = 0):
        """Rows carry a `total_count` attribute (a window-function total over the
        whole filtered set, not just this page) so the common case - a page with
        results - never needs a second query to know total_items. Only an empty
        page (genuinely no matches, or past the last page) has nowhere to read
        that total from; the caller falls back to count_customers() then."""
        params = {
            "search": (search or None), "source": source, "status": status,
            "limit": limit, "offset": offset,
        }
        query = self._q("list_customers").format(order_by=self._order_by(sort))
        with self._session_factory() as db:
            result = db.execute(text(query), params)
            return [RowWrapper(row) for row in result.mappings().all()]

    def count_customers(self, search: str = None, source: str = None, status: str = None) -> int:
        params = {"search": (search or None), "source": source, "status": status}
        with self._session_factory() as db:
            result = db.execute(text(self._q("count_customers")), params)
            row = result.mappings().first()
            return int(row["total"]) if row else 0

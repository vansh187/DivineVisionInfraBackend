import uuid
from datetime import datetime, timezone
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

    def email_in_use(self, email: str) -> bool:
        with self._session_factory() as db:
            result = db.execute(text(self._q("email_in_use")), {"email": email})
            row = result.mappings().first()
            return bool(row and int(row["total"]) > 0)

    def create_manual_lead(self, full_name: str, email: str, phone: str):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {
                    "id": str(uuid.uuid4()),
                    "visitor_name": full_name,
                    "visitor_email": email,
                    "visitor_phone": phone,
                    "created_date": now,
                    "last_updated_date": now,
                }
                result = db.execute(text(self._q("create_manual_lead")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

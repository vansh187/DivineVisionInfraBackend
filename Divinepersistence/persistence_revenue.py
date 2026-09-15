import logging
from sqlalchemy import text
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class persistenceRevenue:
    """Read-only data access for the admin panel's Revenue tab. Reads across
    divine_payments/divine_bookings/divine_customer_users (see
    admin_revenue_queries.yaml for how they're joined) - never writes anything,
    so every method here is a plain SELECT with no transaction to roll back."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("admin_revenue_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def list_transactions(self, search: str = None, revenue_status: str = None, method: str = None,
                           date_from=None, date_to=None, limit: int = 20, offset: int = 0) -> list:
        """Rows carry `total_count` (a window-function total over the whole
        filtered set) so the common non-empty page never needs a second query -
        same convention as persistence_booking.list_queue."""
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("list_transactions")), {
                    "search": (search or None), "revenue_status": (revenue_status or None),
                    "method": (method or None), "date_from": date_from, "date_to": date_to,
                    "limit": limit, "offset": offset,
                })
                return [RowWrapper(row) for row in result.mappings().all()]
        except Exception:
            logger.exception("persistence_revenue.list_transactions_failed")
            raise

    def count_transactions(self, search: str = None, revenue_status: str = None, method: str = None,
                            date_from=None, date_to=None) -> int:
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("count_transactions")), {
                    "search": (search or None), "revenue_status": (revenue_status or None),
                    "method": (method or None), "date_from": date_from, "date_to": date_to,
                })
                row = result.mappings().first()
                return int(row["total"]) if row else 0
        except Exception:
            logger.exception("persistence_revenue.count_transactions_failed")
            raise

    def get_transaction(self, transaction_id: str):
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("get_transaction_by_id")), {"transaction_id": transaction_id})
                row = result.mappings().first()
                return RowWrapper(row) if row else None
        except Exception:
            logger.exception("persistence_revenue.get_transaction_failed transaction_id=%s", transaction_id)
            raise

    def get_summary(self, date_from=None, date_to=None):
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("get_summary")), {"date_from": date_from, "date_to": date_to})
                row = result.mappings().first()
                return RowWrapper(row) if row else None
        except Exception:
            logger.exception("persistence_revenue.get_summary_failed")
            raise

import logging
from sqlalchemy import text
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries

logger = logging.getLogger(__name__)


class persistenceRefund:
    """Read-only data access for the admin panel's Refunds tab. Reads across
    divine_payments/divine_bookings/divine_customer_users (see
    admin_refunds_queries.yaml for how they're joined) - never writes anything,
    so every method here is a plain SELECT with no transaction to roll back.
    Writes to a payment's refund bookkeeping (retry, mark-collected) stay on
    persistencePayment, which already owns that table's mutations."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("admin_refunds_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def list_refunds(self, search: str = None, status: str = None, method: str = None,
                      limit: int = 20, offset: int = 0) -> list:
        """Rows carry `total_count` (a window-function total over the whole
        filtered set) so the common non-empty page never needs a second query -
        same convention as persistence_revenue.list_transactions."""
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("list_refunds")), {
                    "search": (search or None), "status": (status or None),
                    "method": (method or None), "limit": limit, "offset": offset,
                })
                return [RowWrapper(row) for row in result.mappings().all()]
        except Exception:
            logger.exception("persistence_refund.list_refunds_failed")
            raise

    def count_refunds(self, search: str = None, status: str = None, method: str = None) -> int:
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("count_refunds")), {
                    "search": (search or None), "status": (status or None), "method": (method or None),
                })
                row = result.mappings().first()
                return int(row["total"]) if row else 0
        except Exception:
            logger.exception("persistence_refund.count_refunds_failed")
            raise

    def get_refund(self, payment_id: str):
        try:
            with self._session_factory() as db:
                result = db.execute(text(self._q("get_refund_by_id")), {"id": payment_id})
                row = result.mappings().first()
                return RowWrapper(row) if row else None
        except Exception:
            logger.exception("persistence_refund.get_refund_failed payment_id=%s", payment_id)
            raise

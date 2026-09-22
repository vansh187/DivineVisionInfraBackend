import logging
from datetime import datetime, date, timezone
from Divinepersistence import persistenceRevenue

logger = logging.getLogger(__name__)

# Every settled (status='paid') divine_payments row falls into exactly one of
# these buckets - see admin_revenue_queries.yaml's CASE expression, which this
# list must stay in sync with.
REVENUE_STATUSES = ("captured", "cash_recorded", "refund_pending", "refunded")
PAYMENT_METHODS = ("zoho", "cash", "rtgs_neft", "razorpay")


class serviceRevenue:
    """Backs the admin panel's Revenue tab. Read-only and financial: every
    public method here validates its own inputs and never lets an exception
    escape as anything other than a ValueError (bad input, mapped to a 400 by
    the API layer) or a RuntimeError (an internal failure, mapped to a 500
    with no detail leaked to the caller)."""

    def __init__(self, persistence: persistenceRevenue = None):
        self._persistence = persistence or persistenceRevenue()

    def _format_customer_name(self, row) -> str:
        try:
            first = (getattr(row, "customer_first_name", None) or "").strip()
            last = (getattr(row, "customer_last_name", None) or "").strip()
            full = f"{first} {last}".strip()
            return full or None
        except Exception:
            return None

    def _to_amount(self, value) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _parse_date_bound(self, value, field_name: str, end_of_day: bool = False):
        """Accepts a 'YYYY-MM-DD' string (or None) and returns a UTC datetime
        bound suitable for comparing against divine_payments.created_date -
        end_of_day=True pushes a date-only value to 23:59:59.999999 so
        date_to is inclusive of the whole day, not just its first instant."""
        if value in (None, ""):
            return None
        try:
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, date):
                parsed = datetime(value.year, value.month, value.day)
            else:
                parsed = datetime.strptime(str(value), "%Y-%m-%d")
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return parsed.replace(tzinfo=timezone.utc)
        except Exception:
            raise ValueError(f"invalid_{field_name}")

    def _validate_filters(self, revenue_status: str, method: str, date_from, date_to):
        if revenue_status is not None and revenue_status not in REVENUE_STATUSES:
            raise ValueError("invalid_status")
        if method is not None and method not in PAYMENT_METHODS:
            raise ValueError("invalid_method")
        parsed_from = self._parse_date_bound(date_from, "date_from")
        parsed_to = self._parse_date_bound(date_to, "date_to", end_of_day=True)
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValueError("date_from_after_date_to")
        return parsed_from, parsed_to

    def _as_transaction_item(self, row) -> dict:
        return {
            "transaction_id": row.transaction_id,
            "booking_id": getattr(row, "booking_id", None),
            "customer_id": row.customer_id,
            "customer_name": self._format_customer_name(row),
            "project_name": getattr(row, "project_name", None),
            "unit_number": getattr(row, "unit_number", None),
            "amount": self._to_amount(row.amount),
            "currency": row.currency,
            "method": row.method,
            "status": row.revenue_status,
            "created_at": row.created_date,
        }

    def list_transactions(self, search: str = None, status: str = None, method: str = None,
                           date_from=None, date_to=None, page: int = 1, page_size: int = 20) -> dict:
        """GET /admin/revenue/transactions. Raises ValueError for any bad
        filter (mapped to a 400 by the router) and RuntimeError for anything
        else - a broken query, a DB outage - so no financial detail from an
        unexpected failure is ever returned to the caller."""
        try:
            if page < 1 or page_size < 1:
                raise ValueError("invalid_pagination")
            parsed_from, parsed_to = self._validate_filters(status, method, date_from, date_to)
            clean_search = (search or "").strip() or None
            offset = (page - 1) * page_size

            rows = self._persistence.list_transactions(
                search=clean_search, revenue_status=status, method=method,
                date_from=parsed_from, date_to=parsed_to, limit=page_size, offset=offset,
            )
            if rows:
                total_items = int(rows[0].total_count)
            else:
                total_items = self._persistence.count_transactions(
                    search=clean_search, revenue_status=status, method=method,
                    date_from=parsed_from, date_to=parsed_to,
                )
            # page_size < 1 already raised invalid_pagination above, so page_size
            # is always truthy here - no falsy-guard needed on this division.
            total_pages = (total_items + page_size - 1) // page_size
            return {
                "items": [self._as_transaction_item(r) for r in rows],
                "pagination": {
                    "page": page, "page_size": page_size,
                    "total_items": total_items, "total_pages": total_pages,
                },
            }
        except ValueError:
            raise
        except Exception:
            logger.exception("service_revenue.list_transactions_failed")
            raise RuntimeError("list_transactions_failed")

    def get_transaction(self, transaction_id: str) -> dict:
        """GET /admin/revenue/transactions/{transaction_id}. Raises
        ValueError('not_found') when the id doesn't match a settled payment -
        deliberately the same not_found whether the id is unknown or belongs
        to a payment that never actually settled, so this endpoint can't be
        used to probe for the existence of failed/pending payments."""
        try:
            clean_id = (transaction_id or "").strip()
            if not clean_id:
                raise ValueError("not_found")
            row = self._persistence.get_transaction(clean_id)
            if not row:
                raise ValueError("not_found")
            item = self._as_transaction_item(row)
            method = (getattr(row, "method", None) or "zoho").lower()
            # zoho_payment_id for a live-gateway payment, or the legacy
            # razorpay_payment_id for a pre-cutover one - never both.
            item["gateway_payment_id"] = (getattr(row, "zoho_payment_id", None) if method != "razorpay"
                                          else getattr(row, "razorpay_payment_id", None))
            item["utr_number"] = getattr(row, "utr_number", None)
            return item
        except ValueError:
            raise
        except Exception:
            logger.exception("service_revenue.get_transaction_failed transaction_id=%s", transaction_id)
            raise RuntimeError("get_transaction_failed")

    def get_summary(self, date_from=None, date_to=None) -> dict:
        """GET /admin/revenue/summary - the totals behind the Revenue tab's
        top-of-page stat cards, over the same settled-payments universe as
        list_transactions (and filterable by the same date range)."""
        try:
            parsed_from, parsed_to = self._validate_filters(None, None, date_from, date_to)
            row = self._persistence.get_summary(date_from=parsed_from, date_to=parsed_to)
            if not row:
                return {
                    "total_transactions": 0, "gross_amount": 0.0, "net_amount": 0.0,
                    "captured_amount": 0.0, "cash_amount": 0.0,
                    "refund_pending_amount": 0.0, "refunded_amount": 0.0,
                }
            return {
                "total_transactions": int(row.total_transactions or 0),
                "gross_amount": self._to_amount(row.gross_amount),
                "net_amount": self._to_amount(row.net_amount),
                "captured_amount": self._to_amount(row.captured_amount),
                "cash_amount": self._to_amount(row.cash_amount),
                "refund_pending_amount": self._to_amount(row.refund_pending_amount),
                "refunded_amount": self._to_amount(row.refunded_amount),
            }
        except ValueError:
            raise
        except Exception:
            logger.exception("service_revenue.get_summary_failed")
            raise RuntimeError("get_summary_failed")

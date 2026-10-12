import logging
from Divinepersistence import persistenceRefund, persistencePayment

logger = logging.getLogger(__name__)

# Every refund-in-progress row (refund_status != 'none') falls into exactly
# one of these display buckets - see admin_refunds_queries.yaml's CASE
# expression, which this tuple must stay in sync with.
REFUND_STATUSES = (
    "processing", "completed", "failed",
    "cash_refund_pending", "cash_collected",
    "bank_transfer_pending", "bank_transfer_completed",
)
PAYMENT_METHODS = ("zoho", "cash", "rtgs_neft", "razorpay")
# cash/rtgs_neft never had a gateway; 'razorpay' no longer does either - that
# gateway was retired at the Zoho Payments cutover, so any pre-cutover payment's
# refund is now confirmed manually the same way, via mark_collected.
_MANUAL_METHODS = ("cash", "rtgs_neft", "razorpay")


class serviceRefund:
    """Backs the admin panel's Refunds tab: real-time tracking of every
    refund-in-progress payment, plus the one write action manual (cash/
    rtgs_neft/legacy-razorpay) refunds need that the live Zoho gateway path
    doesn't - an admin confirming the money was actually paid out. Every public method validates
    its own inputs and never lets an exception escape as anything other than a
    ValueError (bad input / business rule, mapped to a 4xx by the API layer)
    or a RuntimeError (an internal failure, mapped to a 500 with no detail
    leaked to the caller)."""

    def __init__(self, persistence: persistenceRefund = None, payment_persistence: persistencePayment = None):
        self._persistence = persistence or persistenceRefund()
        self._payment_persistence = payment_persistence or persistencePayment()

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

    def _as_item(self, row) -> dict:
        return {
            "id": row.id,
            "booking_id": getattr(row, "booking_id", None),
            "customer_id": row.customer_id,
            "customer_name": self._format_customer_name(row),
            "project_name": getattr(row, "project_name", None),
            "unit_number": getattr(row, "unit_number", None),
            "amount": self._to_amount(row.amount),
            "currency": row.currency,
            "method": row.method,
            "status": row.display_status,
            "refund_initiated_date": getattr(row, "refund_initiated_date", None),
            "refund_completed_date": getattr(row, "refund_completed_date", None),
        }

    def list_refunds(self, search: str = None, status: str = None, method: str = None,
                      page: int = 1, page_size: int = 20) -> dict:
        """GET /admin/refunds. Raises ValueError for any bad filter (mapped to
        a 400 by the router) and RuntimeError for anything else - a broken
        query, a DB outage - so no financial detail from an unexpected
        failure is ever returned to the caller."""
        try:
            if page < 1 or page_size < 1:
                raise ValueError("invalid_pagination")
            if status is not None and status not in REFUND_STATUSES:
                raise ValueError("invalid_status")
            if method is not None and method not in PAYMENT_METHODS:
                raise ValueError("invalid_method")
            clean_search = (search or "").strip() or None
            offset = (page - 1) * page_size

            rows = self._persistence.list_refunds(
                search=clean_search, status=status, method=method, limit=page_size, offset=offset,
            )
            if rows:
                total_items = int(rows[0].total_count)
            else:
                total_items = self._persistence.count_refunds(search=clean_search, status=status, method=method)
            total_pages = (total_items + page_size - 1) // page_size
            return {
                "items": [self._as_item(r) for r in rows],
                "pagination": {
                    "page": page, "page_size": page_size,
                    "total_items": total_items, "total_pages": total_pages,
                },
            }
        except ValueError:
            raise
        except Exception:
            logger.exception("service_refund.list_refunds_failed")
            raise RuntimeError("list_refunds_failed")

    def get_refund(self, payment_id: str) -> dict:
        """GET /admin/refunds/{payment_id}. Raises ValueError('not_found') for
        an unknown payment id or one that was never actually refunded
        (refund_status='none') - deliberately the same not_found for both, so
        this can't be used to probe which payments exist."""
        try:
            clean_id = (payment_id or "").strip()
            if not clean_id:
                raise ValueError("not_found")
            row = self._persistence.get_refund(clean_id)
            if not row:
                raise ValueError("not_found")
            item = self._as_item(row)
            method = (getattr(row, "method", None) or "zoho").lower()
            item.update({
                # zoho_payment_id for a live-gateway refund, or the legacy
                # razorpay_payment_id for a pre-cutover payment - never both.
                "gateway_payment_id": (getattr(row, "zoho_payment_id", None) if method != "razorpay"
                                       else getattr(row, "razorpay_payment_id", None)),
                "zoho_refund_id": getattr(row, "zoho_refund_id", None),
                "utr_number": getattr(row, "utr_number", None),
                "refund_note": getattr(row, "refund_note", None),
                "created_at": getattr(row, "created_date", None),
            })
            return item
        except ValueError:
            raise
        except Exception:
            logger.exception("service_refund.get_refund_failed payment_id=%s", payment_id)
            raise RuntimeError("get_refund_failed")

    def mark_collected(self, payment_id: str, admin_id: str, note: str = None) -> dict:
        """Admin confirms a cash/rtgs_neft/legacy-razorpay refund was actually
        paid out - the only way those methods' refund_status can ever move from
        'pending' to 'completed', since there's no gateway webhook for a manual
        payout (and, for razorpay, no live gateway left at all). Narrowly scoped
        (an atomic DB compare-and-swap, not a plain read then write) so it can
        never mark a live 'zoho' refund "collected" or double-apply to an
        already-completed one, even under a concurrent double-click. Raises
        ValueError('not_found') / ValueError('not_a_manual_refund') /
        ValueError('refund_not_pending')."""
        try:
            clean_id = (payment_id or "").strip()
            if not clean_id:
                raise ValueError("not_found")
            record = self._payment_persistence.get_by_id(clean_id)
            if not record:
                raise ValueError("not_found")
            method = (getattr(record, "method", None) or "zoho").lower()
            if method not in _MANUAL_METHODS:
                raise ValueError("not_a_manual_refund")

            clean_note = (note or "").strip()
            final_note = f"Refund collected - confirmed by {admin_id}."
            if clean_note:
                final_note = f"{final_note} {clean_note}"

            claimed = self._payment_persistence.mark_manual_refund_collected(id=clean_id, note=final_note)
            if claimed is None:
                raise ValueError("refund_not_pending")
            return self.get_refund(clean_id)
        except ValueError:
            raise
        except Exception:
            logger.exception("service_refund.mark_collected_failed payment_id=%s", payment_id)
            raise RuntimeError("mark_collected_failed")

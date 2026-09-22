import logging
import time
from fastapi import APIRouter, Depends, HTTPException

from DivineDTO.models import RefundStatusDTO
from DivineService import servicePayment
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/payments", tags=["admin", "payments"])
_payment_service = servicePayment()


def _to_refund_status(record) -> RefundStatusDTO:
    return RefundStatusDTO(
        id=record.id,
        method=getattr(record, "method", "zoho"),
        refund_status=getattr(record, "refund_status", None) or "none",
        refund_amount=float(record.refund_amount) if getattr(record, "refund_amount", None) is not None else None,
        zoho_refund_id=getattr(record, "zoho_refund_id", None),
        refund_initiated_date=getattr(record, "refund_initiated_date", None),
        refund_completed_date=getattr(record, "refund_completed_date", None),
        refund_note=getattr(record, "refund_note", None),
    )


@router.post("/{payment_id}/refund/retry", response_model=RefundStatusDTO)
def retry_payment_refund(payment_id: str, current_admin: dict = Depends(get_current_admin)):
    """Resumes a Zoho refund stuck at refund_status='pending' after its automatic
    attempts (see servicePayment.initiate_refund's built-in one-retry) both failed
    at the gateway - e.g. following a Cancel/Reject on a booking whose Zoho refund
    call hit a timeout or transient error. Narrowly scoped: only ever re-attempts
    a genuinely-stuck Zoho refund, never touches a cash/RTGS-NEFT/legacy-razorpay
    refund (those are a manual payout owed by the business, not something to
    retry against a gateway) or one already completed. A legacy razorpay refund
    gets its own distinct 409 (razorpay_gateway_retired) rather than being lumped
    in with not_a_zoho_refund, so the admin UI can point at mark-collected."""
    start = time.monotonic()
    try:
        updated = _payment_service.retry_zoho_refund(payment_id, reason=None)
        return _to_refund_status(updated)
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="not_found")
        if code in ("not_a_zoho_refund", "razorpay_gateway_retired", "refund_not_retryable", "payment_not_paid"):
            raise HTTPException(status_code=409, detail=code)
        raise HTTPException(status_code=400, detail=code)
    except Exception:
        logger.exception("admin_retry_payment_refund_failed payment_id=%s", payment_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_retry_payment_refund_latency_ms=%.2f", (time.monotonic() - start) * 1000)

import logging
import time
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query

from DivineDTO.models import (
    RefundListResponseDTO, RefundDetailDTO, RefundMarkCollectedRequestDTO,
)
from DivineService import serviceRefund, servicePayment
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/refunds", tags=["admin", "refunds"])
_refund_service = serviceRefund()
_payment_service = servicePayment()


def _map_value_error(e: ValueError):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=RefundListResponseDTO)
def list_refunds(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(
        None, max_length=200,
        description="Matches payment id, booking id, project, unit, or customer name",
    ),
    status: Optional[Literal["processing", "completed", "failed", "cash_refund_pending", "cash_collected",
                             "bank_transfer_pending", "bank_transfer_completed"]] = Query(None),
    method: Optional[Literal["zoho", "cash", "rtgs_neft", "razorpay"]] = Query(None),
    current_admin: dict = Depends(get_current_admin),
):
    """Real-time refund tracking for the admin panel's Refunds tab. Admin-only.
    Every payment with a refund in progress or resolved (refund_status !=
    'none') appears exactly once, bucketed into one of the seven `status`
    values above."""
    start = time.monotonic()
    try:
        return _refund_service.list_refunds(
            search=search, status=status, method=method, page=page, page_size=page_size,
        )
    except ValueError as e:
        _map_value_error(e)
    except Exception:
        logger.exception("admin_list_refunds_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_refunds_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/{payment_id}", response_model=RefundDetailDTO)
def get_refund(payment_id: str, current_admin: dict = Depends(get_current_admin)):
    """Detail view for one refund (the 'View' action). Admin-only."""
    start = time.monotonic()
    try:
        return _refund_service.get_refund(payment_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_get_refund_failed payment_id=%s", payment_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_refund_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/{payment_id}/retry", response_model=RefundDetailDTO)
def retry_refund(payment_id: str, current_admin: dict = Depends(get_current_admin)):
    """Resumes a Zoho refund stuck at 'processing' (display status) after both
    of its automatic gateway attempts failed - see
    servicePayment.retry_zoho_refund for the atomic claim that makes this
    double-click safe. A legacy razorpay refund can't be retried here (that
    gateway was retired at cutover) - use mark-collected instead. Admin-only."""
    start = time.monotonic()
    try:
        _payment_service.retry_zoho_refund(payment_id, reason=None)
        return _refund_service.get_refund(payment_id)
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="not_found")
        raise HTTPException(status_code=409, detail=code)
    except Exception:
        logger.exception("admin_retry_refund_failed payment_id=%s", payment_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_retry_refund_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/{payment_id}/mark-collected", response_model=RefundDetailDTO)
def mark_refund_collected(payment_id: str, dto: RefundMarkCollectedRequestDTO,
                          current_admin: dict = Depends(get_current_admin)):
    """Admin confirms a cash/rtgs_neft refund was actually paid out - the only
    way those methods' status can move from 'pending' to 'completed', since
    there is no gateway to confirm it automatically. Admin-only."""
    start = time.monotonic()
    try:
        return _refund_service.mark_collected(payment_id, admin_id=current_admin["sub"], note=dto.note)
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="not_found")
        raise HTTPException(status_code=409, detail=code)
    except Exception:
        logger.exception("admin_mark_refund_collected_failed payment_id=%s", payment_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_mark_refund_collected_latency_ms=%.2f", (time.monotonic() - start) * 1000)

import io
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import List

from DivineDTO.models import MyBookingItemDTO
from DivineService import serviceBookingKyc
from DivineService.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bookings", tags=["bookings"])
_booking_service = serviceBookingKyc()


def _require_customer(current_user: dict) -> str:
    if current_user["role"] != "customer":
        raise HTTPException(status_code=403, detail="bookings_customer_only")
    return current_user["sub"]


@router.get("/mine", response_model=List[MyBookingItemDTO])
def list_my_bookings(current_user: dict = Depends(get_current_user)):
    """Every booking (any status) belonging to the signed-in customer, so the
    website can show 'KYC pending' / 'Booked' / 'Cancelled' and gate the
    receipt-download button on can_download_receipt."""
    start = time.monotonic()
    customer_id = _require_customer(current_user)
    try:
        return _booking_service.list_mine(customer_id)
    except Exception:
        logger.exception("bookings_mine_failed customer_id=%s", customer_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("bookings_mine_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/{booking_id}/receipt")
def get_booking_receipt(booking_id: str, current_user: dict = Depends(get_current_user)):
    """Server-rendered booking receipt PDF. Enabled ONLY once the booking's KYC
    review has been approved (status='booked') - see
    serviceBookingKyc.get_receipt. Owner only."""
    customer_id = _require_customer(current_user)
    try:
        pdf_bytes, filename = _booking_service.get_receipt(booking_id, requester_id=customer_id)
        return StreamingResponse(
            io.BytesIO(pdf_bytes), media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as e:
        if str(e) == "kyc_not_approved":
            raise HTTPException(status_code=400, detail="kyc_not_approved")
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("booking_receipt_failed booking_id=%s", booking_id)
        raise HTTPException(status_code=500, detail="receipt_failed")

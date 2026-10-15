import io
import logging
import time
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from DivineDTO.models import (
    BookingQueueResponseDTO, BookingDetailDTO, BookingDecisionRequestDTO,
    AdminInstallmentPaymentRequestDTO,
)
from DivineService import serviceBookingKyc
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/bookings", tags=["admin", "bookings"])
_booking_service = serviceBookingKyc()


@router.get("", response_model=BookingQueueResponseDTO)
def list_booking_queue(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200, description="Matches booking id, customer id, or project name"),
    status: Optional[Literal["pending_kyc_review", "booked", "rejected", "cancelled"]] = Query(None),
    kyc_status: Optional[Literal["pending", "verified", "needs_resubmission", "rejected"]] = Query(None),
    current_admin: dict = Depends(get_current_admin),
):
    """Admin panel's Booking Queue - every plot-booking payment awaiting or
    already through KYC review."""
    start = time.monotonic()
    try:
        return _booking_service.list_queue(
            search=search, status=status, kyc_status=kyc_status, page=page, page_size=page_size,
        )
    except Exception:
        logger.exception("admin_list_booking_queue_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_booking_queue_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/{booking_id}", response_model=BookingDetailDTO)
def get_booking_detail(booking_id: str, current_admin: dict = Depends(get_current_admin)):
    start = time.monotonic()
    try:
        return _booking_service.get_detail(booking_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_get_booking_detail_failed booking_id=%s", booking_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_booking_detail_latency_ms=%.2f", (time.monotonic() - start) * 1000)


def _handle_decision_errors(e: ValueError):
    code = str(e)
    if code == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    if code == "version_conflict":
        raise HTTPException(status_code=409, detail="version_conflict")
    if code == "booking_not_reviewable":
        raise HTTPException(status_code=409, detail="booking_not_reviewable")
    if code == "booking_not_cancellable":
        raise HTTPException(status_code=409, detail="booking_not_cancellable")
    if code == "inventory_confirm_failed":
        raise HTTPException(status_code=409, detail="inventory_confirm_failed")
    raise HTTPException(status_code=400, detail=code)


@router.post("/{booking_id}/approve", response_model=BookingDetailDTO)
def approve_booking(booking_id: str, dto: BookingDecisionRequestDTO, current_admin: dict = Depends(get_current_admin)):
    """KYC verified - confirms the plot as booked, notifies the customer, and
    unlocks their receipt download. `version` must match the booking's current
    version (409 version_conflict otherwise - another admin already decided)."""
    start = time.monotonic()
    try:
        return _booking_service.approve(
            booking_id, admin_id=current_admin["sub"], note=dto.note, expected_version=dto.version,
        )
    except ValueError as e:
        _handle_decision_errors(e)
    except Exception:
        logger.exception("admin_approve_booking_failed booking_id=%s", booking_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_approve_booking_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/{booking_id}/reject", response_model=BookingDetailDTO)
def reject_booking(booking_id: str, dto: BookingDecisionRequestDTO, current_admin: dict = Depends(get_current_admin)):
    """KYC rejected - releases the plot back to available and initiates a
    refund (automatic for Zoho Payments, manual-instructions for cash/RTGS-NEFT/legacy Razorpay),
    then notifies the customer with the admin's note."""
    start = time.monotonic()
    try:
        return _booking_service.reject(
            booking_id, admin_id=current_admin["sub"], note=dto.note, expected_version=dto.version,
        )
    except ValueError as e:
        _handle_decision_errors(e)
    except Exception:
        logger.exception("admin_reject_booking_failed booking_id=%s", booking_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_reject_booking_latency_ms=%.2f", (time.monotonic() - start) * 1000)


_PDF_ERROR_STATUS = {
    "not_found": 404,
    "not_uploaded": 404,
    "booking_not_booked": 409,
}


def _pdf_download(build, booking_id: str, failure_code: str):
    """Streams a (pdf_bytes, filename) builder as an attachment, mapping the
    service's ValueError codes onto HTTP statuses."""
    try:
        pdf_bytes, filename = build(booking_id)
        return StreamingResponse(
            io.BytesIO(pdf_bytes), media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=_PDF_ERROR_STATUS.get(code, 404),
                            detail=code if code in _PDF_ERROR_STATUS else "not_found")
    except Exception:
        logger.exception("admin_%s booking_id=%s", failure_code, booking_id)
        raise HTTPException(status_code=500, detail=failure_code)


@router.get("/{booking_id}/application-form")
def get_application_form(booking_id: str, current_admin: dict = Depends(get_current_admin)):
    """The customer's own uploaded, signed Booking Application Form PDF, as stored."""
    return _pdf_download(_booking_service.get_application_form, booking_id, "application_form_failed")


@router.get("/{booking_id}/allotment-letter")
def get_allotment_letter(booking_id: str, current_admin: dict = Depends(get_current_admin)):
    """Generated Provisional Allotment Letter for a booked plot (409
    booking_not_booked otherwise)."""
    return _pdf_download(_booking_service.get_allotment_letter, booking_id, "allotment_letter_failed")


@router.get("/{booking_id}/demand-letter")
def get_demand_letter(booking_id: str, current_admin: dict = Depends(get_current_admin)):
    """Generated Demand Letter for a booked plot, from its live payment schedule
    (409 booking_not_booked otherwise)."""
    return _pdf_download(_booking_service.get_demand_letter, booking_id, "demand_letter_failed")


_INSTALLMENT_PAYMENT_ERROR_STATUS = {
    "not_found": 404,
    "milestone_not_found": 404,
    "booking_not_booked": 409,
    "installment_already_paid": 409,
}


@router.post("/{booking_id}/installments/{milestone_id}/payments", response_model=BookingDetailDTO)
def record_installment_payment(
    booking_id: str, milestone_id: str, dto: AdminInstallmentPaymentRequestDTO,
    current_admin: dict = Depends(get_current_admin),
):
    """Admin records an installment payment collected outside the gateway
    (cash/cheque/RTGS-NEFT) against one milestone of the booking's payment
    plan, from the Booking Detail screen. Returns the refreshed booking detail
    (payment_schedule reflects the now-paid milestone)."""
    start = time.monotonic()
    try:
        return _booking_service.record_installment_payment(
            booking_id, admin_id=current_admin["sub"], milestone_id=milestone_id,
            amount=dto.amount, method=dto.method, utr_number=dto.utr_number,
            cheque_number=dto.cheque_number, note=dto.note,
        )
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=_INSTALLMENT_PAYMENT_ERROR_STATUS.get(code, 400), detail=code)
    except Exception:
        logger.exception("admin_record_installment_payment_failed booking_id=%s milestone_id=%s",
                          booking_id, milestone_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_record_installment_payment_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/{booking_id}/cancel", response_model=BookingDetailDTO)
def cancel_booking(booking_id: str, dto: BookingDecisionRequestDTO, current_admin: dict = Depends(get_current_admin)):
    """Admin cancels the booking outright (distinct from a KYC Reject - e.g. the
    customer asked to cancel), whether it's still awaiting KYC review or already
    booked/approved. Releases the plot, initiates a refund matched to how the
    payment arrived (automatic for Zoho Payments, manual instructions for cash /
    RTGS-NEFT/legacy Razorpay), and always emails the customer with refund instructions - `version`
    must match the booking's current version (409 version_conflict otherwise)."""
    start = time.monotonic()
    try:
        return _booking_service.cancel(
            booking_id, admin_id=current_admin["sub"], note=dto.note, expected_version=dto.version,
        )
    except ValueError as e:
        _handle_decision_errors(e)
    except Exception:
        logger.exception("admin_cancel_booking_failed booking_id=%s", booking_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_cancel_booking_latency_ms=%.2f", (time.monotonic() - start) * 1000)

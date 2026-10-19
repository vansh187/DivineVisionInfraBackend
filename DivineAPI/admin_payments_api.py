import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Query

from DivineDTO.models import (RefundStatusDTO, OfflinePaymentSubmissionAdminDTO,
                              OfflinePaymentRejectDTO)
from DivineService import servicePayment
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/payments", tags=["admin", "payments"])
_payment_service = servicePayment()


def _offline_submission_dto(row):
    first = (getattr(row, "first_name", None) or "").strip()
    last = (getattr(row, "last_name", None) or "").strip()
    return OfflinePaymentSubmissionAdminDTO(
        id=row.id, amount=float(row.amount), method=row.method, status=row.status,
        installment_no=row.installment_no, utr_number=row.utr_number,
        created_date=row.created_date, customer_id=row.customer_id,
        customer_name=f"{first} {last}".strip() or None,
        booking_id=row.booking_id, inventory_id=row.inventory_id,
        milestone_id=row.milestone_id, milestone_label=row.milestone_label,
        due_date=str(row.due_date)[:10] if row.due_date else None,
        project_name=getattr(row, "project_name", None), unit_number=getattr(row, "unit_number", None),
        reviewer_id=row.reviewer_id, reviewed_at=row.reviewed_at,
        decision=row.decision, reason=row.reason,
    )


@router.get("/offline-submissions", response_model=list[OfflinePaymentSubmissionAdminDTO])
def list_offline_submissions(status: str = Query("pending_review"), current_admin: dict = Depends(get_current_admin)):
    if status not in ("pending_review", "approved", "rejected", "all"):
        raise HTTPException(status_code=400, detail="invalid_status")
    try:
        return [_offline_submission_dto(r) for r in _payment_service.list_offline_submissions(None if status == "all" else status)]
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/offline-submissions/{submission_id}/approve", response_model=OfflinePaymentSubmissionAdminDTO)
def approve_offline_submission(submission_id: str, current_admin: dict = Depends(get_current_admin)):
    return _decide_offline_submission(submission_id, current_admin, "approved", None)


@router.post("/offline-submissions/{submission_id}/reject", response_model=OfflinePaymentSubmissionAdminDTO)
def reject_offline_submission(submission_id: str, dto: OfflinePaymentRejectDTO,
                              current_admin: dict = Depends(get_current_admin)):
    return _decide_offline_submission(submission_id, current_admin, "rejected", dto.reason)


def _decide_offline_submission(submission_id, current_admin, decision, reason):
    try:
        row = _payment_service.decide_offline_submission(submission_id, current_admin["sub"], decision, reason)
        # Enrich the response with the same queue fields, including booking identity.
        match = next((r for r in _payment_service.list_offline_submissions("all") if r.id == row.id), None)
        if match:
            return _offline_submission_dto(match)
        return OfflinePaymentSubmissionAdminDTO(
            id=row.id, amount=float(row.amount), method=row.method, status=row.status,
            installment_no=row.installment_no, utr_number=row.utr_number,
            created_date=row.created_date, customer_id=row.customer_id,
            booking_id=row.booking_id, inventory_id=row.inventory_id,
            milestone_id=row.milestone_id, milestone_label=row.milestone_label,
            due_date=str(row.due_date)[:10] if row.due_date else None,
            reviewer_id=row.reviewer_id, reviewed_at=row.reviewed_at,
            decision=row.decision, reason=row.reason,
        )
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=404 if code == "not_found" else 409, detail=code)
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


def _to_refund_status(record) -> RefundStatusDTO:
    return RefundStatusDTO(
        id=record.id,
        method=getattr(record, "method", "razorpay"),
        refund_status=getattr(record, "refund_status", None) or "none",
        refund_amount=float(record.refund_amount) if getattr(record, "refund_amount", None) is not None else None,
        razorpay_refund_id=getattr(record, "razorpay_refund_id", None),
        refund_initiated_date=getattr(record, "refund_initiated_date", None),
        refund_completed_date=getattr(record, "refund_completed_date", None),
        refund_note=getattr(record, "refund_note", None),
    )


@router.post("/{payment_id}/refund/retry", response_model=RefundStatusDTO)
def retry_payment_refund(payment_id: str, current_admin: dict = Depends(get_current_admin)):
    """Resumes a Razorpay refund stuck at refund_status='pending' after its
    automatic attempts (see servicePayment.initiate_refund's built-in one-retry)
    both failed at the gateway - e.g. following a Cancel/Reject on a booking whose
    Razorpay refund call hit a timeout or transient error. Narrowly scoped: only
    ever re-attempts a genuinely-stuck Razorpay refund, never touches a cash/
    RTGS-NEFT refund (those are a manual payout owed by the business, not
    something to retry against a gateway) or one already completed."""
    start = time.monotonic()
    try:
        updated = _payment_service.retry_razorpay_refund(payment_id, reason=None)
        return _to_refund_status(updated)
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="not_found")
        if code in ("not_a_razorpay_refund", "refund_not_retryable", "payment_not_paid"):
            raise HTTPException(status_code=409, detail=code)
        raise HTTPException(status_code=400, detail=code)
    except Exception:
        logger.exception("admin_retry_payment_refund_failed payment_id=%s", payment_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_retry_payment_refund_latency_ms=%.2f", (time.monotonic() - start) * 1000)

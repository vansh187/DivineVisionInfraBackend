import io
import logging
import time
import uuid
from typing import Optional, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from DivineDTO.models import (
    BookingQueueResponseDTO, BookingDetailDTO, BookingDecisionRequestDTO,
    AdminInstallmentPaymentRequestDTO,
)
from DivineService import serviceBookingKyc, serviceAdmin
from DivineService.auth import get_current_admin
from DivineService.service_booking_kyc import KYC_DOCUMENT_TYPES
from DivineService.service_booking_document_upload import (
    serviceBookingDocumentUpload, BookingDocumentUploadError, MAX_UPLOAD_BYTES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/bookings", tags=["admin", "bookings"])
_booking_service = serviceBookingKyc()
_document_upload_service = serviceBookingDocumentUpload(
    _booking_service.persistence, _booking_service.document_service, _booking_service.get_detail,
)
_admin_service = None


def _admin_accounts() -> serviceAdmin:
    """Lazily built: only the document upload needs the live approval check, so
    importing this router never requires the admin service's own config."""
    global _admin_service
    if _admin_service is None:
        _admin_service = serviceAdmin()
    return _admin_service


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
    refund (automatic for Razorpay, manual-instructions for cash/RTGS-NEFT),
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
    started = time.perf_counter()
    try:
        pdf_bytes, filename = build(booking_id)
        logger.info("admin_document_download_ready booking_id=%s kind=%s bytes=%s elapsed_ms=%.2f",
                    booking_id, failure_code, len(pdf_bytes), (time.perf_counter() - started) * 1000)
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
    payment arrived (automatic for Razorpay, manual instructions for cash /
    RTGS-NEFT), and always emails the customer with refund instructions - `version`
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


# ---------- KYC document upload/replacement ----------
# Unlike the endpoints above, this one uses the {"code", "message"} error
# envelope from the frontend contract (docs/BOOKING_KYC_REVIEW_ADMIN_API.md §4).

_UPLOAD_ERROR_STATUS = {
    "invalid_multipart": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "booking_not_found": 404,
    "version_conflict": 409,
    "booking_not_uploadable": 409,
    "idempotency_conflict": 409,
    "file_too_large": 413,
    "unsupported_media_type": 415,
    "validation_error": 422,
    "rate_limited": 429,
    "internal_error": 500,
    "document_storage_unavailable": 503,
    # Saved, but the detail reload failed - retrying with the same
    # Idempotency-Key replays without writing again.
    "document_saved_refresh_failed": 503,
}
# File cap plus room for the multipart boundaries and the two small text fields.
_MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def _upload_error(code: str, message: str, extra: dict = None, headers: dict = None) -> HTTPException:
    return HTTPException(
        status_code=_UPLOAD_ERROR_STATUS[code],
        detail={"code": code, "message": message, **(extra or {})},
        headers=headers,
    )


def get_current_approved_admin(authorization: str = Header(None)) -> dict:
    """A valid admin access token is not enough on its own for a write that
    touches KYC documents: the admin must still exist and be allowed to act
    right now under the same rule login uses (super admin, or approved) - a
    token minted before a rejection stays signed until it expires."""
    try:
        admin = get_current_admin(authorization)
    except HTTPException as e:
        if e.status_code == 401:
            raise _upload_error("unauthorized", "Sign in again to continue.")
        raise _upload_error("internal_error", "Unable to verify the session. Contact support with the request ID.")
    try:
        active = _admin_accounts().get_active_admin(admin["sub"])
    except Exception:
        logger.exception("admin_upload_auth_lookup_failed admin_id=%s", admin["sub"])
        raise _upload_error("internal_error", "Unable to verify the session. Contact support with the request ID.")
    if active is None:
        raise _upload_error("forbidden", "Your admin account is not permitted to perform this action.")
    return admin


async def _read_bounded(upload: UploadFile) -> bytes:
    """Reads at most MAX_UPLOAD_BYTES + 1 so an oversized file is detected without
    holding more than that in memory."""
    chunks, total = [], 0
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _upload_error("file_too_large", "File exceeds the 10 MiB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{booking_id}/documents/{document_type}", response_model=BookingDetailDTO)
async def upload_booking_document(
    booking_id: str, document_type: str, request: Request, response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_admin: dict = Depends(get_current_approved_admin),
):
    """Admin uploads or replaces one KYC checklist document (Aadhaar front/back,
    PAN, applicant photo, cancelled cheque) on a booking still under KYC review.
    multipart/form-data: `file` (PDF/JPG/PNG, max 10 MiB), `expected_version`,
    optional `note`. Returns the refreshed Booking Detail with the bumped
    `version`. Never approves anything - kyc_status/status are left unchanged."""
    start = time.monotonic()
    request_id = getattr(request.state, "request_id", None)
    admin_id = current_admin["sub"]
    result_code, replayed, byte_count = "ok", False, None
    form = None
    try:
        if document_type not in KYC_DOCUMENT_TYPES:
            raise _upload_error("validation_error", "Unsupported document type.")
        if not idempotency_key:
            raise _upload_error("validation_error", "Idempotency-Key header is required.")
        try:
            idempotency_key = str(uuid.UUID(idempotency_key.strip()))
        except ValueError:
            raise _upload_error("invalid_multipart", "Idempotency-Key must be a UUID.")

        # A declared Content-Length is mandatory: the server then never reads more
        # body than that, so this check bounds what request.form() can spool to
        # disk. A chunked/unsized body could otherwise stream gigabytes before
        # the file-size check ever ran. Browsers always send it for FormData.
        raw_length = request.headers.get("content-length")
        if raw_length is None or "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            raise _upload_error("invalid_multipart", "Content-Length header is required.")
        try:
            content_length = int(raw_length)
        except ValueError:
            raise _upload_error("invalid_multipart", "Invalid Content-Length header.")
        if content_length < 0:
            raise _upload_error("invalid_multipart", "Invalid Content-Length header.")
        if content_length > _MAX_UPLOAD_REQUEST_BYTES:
            raise _upload_error("file_too_large", "File exceeds the 10 MiB limit.")
        if not (request.headers.get("content-type") or "").lower().startswith("multipart/form-data"):
            raise _upload_error("invalid_multipart", "Request must be multipart/form-data.")
        try:
            form = await request.form(max_files=1, max_fields=5)
        except Exception:
            raise _upload_error("invalid_multipart", "Malformed multipart request.")

        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _upload_error("validation_error", "A file is required.")
        raw_version = form.get("expected_version")
        try:
            expected_version = int(raw_version) if isinstance(raw_version, str) else None
        except ValueError:
            expected_version = None
        if expected_version is None or expected_version < 1:
            raise _upload_error("validation_error", "expected_version must be an integer >= 1.")
        note = form.get("note")
        if note is not None and not isinstance(note, str):
            raise _upload_error("validation_error", "note must be plain text.")

        file_bytes = await _read_bounded(upload)
        byte_count = len(file_bytes)
        detail, replayed = await run_in_threadpool(
            _document_upload_service.upload,
            booking_id=booking_id, document_type=document_type, file_bytes=file_bytes,
            filename=upload.filename, client_content_type=upload.content_type,
            expected_version=expected_version, note=note, idempotency_key=idempotency_key,
            admin_id=admin_id, request_id=request_id,
        )
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return detail
    except HTTPException as e:
        result_code = (e.detail or {}).get("code", "error") if isinstance(e.detail, dict) else "error"
        raise
    except BookingDocumentUploadError as e:
        result_code = e.code
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        raise _upload_error(e.code, e.message, extra=e.extra, headers=headers)
    except ValueError:
        # get_detail's not_found after a successful write/replay (booking deleted mid-request).
        result_code = "booking_not_found"
        raise _upload_error("booking_not_found", "Booking not found.")
    except Exception:
        result_code = "internal_error"
        logger.exception("admin_booking_document_upload_failed booking_id=%s request_id=%s",
                         booking_id, request_id)
        raise _upload_error("internal_error", "Unable to upload document. Contact support with the request ID.")
    finally:
        if form is not None:
            await form.close()
        logger.info(
            "admin_booking_document_upload result=%s booking_id=%s admin_id=%s document_type=%s bytes=%s "
            "replayed=%s request_id=%s duration_ms=%.2f",
            result_code, booking_id, admin_id, document_type, byte_count, replayed, request_id,
            (time.monotonic() - start) * 1000,
        )

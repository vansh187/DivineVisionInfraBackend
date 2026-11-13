import os
import time
import logging
from typing import Optional, Literal
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    AdminCreateDTO, AdminLoginDTO, AdminOutDTO, AdminProfileDTO, AdminTokenDTO, AdminAccessTokenDTO, AdminRefreshDTO,
    AdminVerifySignupDTO, AdminResendSignupOtpDTO,
    AdminApprovalItemDTO, AdminApprovalListResponseDTO, AdminApprovalDecisionDTO,
    CustomerListResponseDTO, CustomerListItemDTO, CustomerCreateDTO, BrokerListResponseDTO,
    CustomerDetailDTO, CustomerPlotDTO, CustomerPlotTermsDTO,
    AdminVisitListResponseDTO, AdminVisitListItemDTO,
    ForgotPasswordDTO, ResetPasswordDTO, MessageDTO,
)
from Divinepersistence import persistenceAdmin
from DivineService import (
    serviceAdmin, serviceAdminCustomers, serviceAdminBrokers, serviceAdminVisits,
    servicePasswordReset, PasswordResetError,
)
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
_admin_service = serviceAdmin(secret_key=os.getenv("ADMIN_JWT_SECRET_KEY"))
_password_reset_service = servicePasswordReset(role="admin", user_persistence=persistenceAdmin())
_admin_customers_service = serviceAdminCustomers()
_admin_brokers_service = serviceAdminBrokers()
_admin_visits_service = serviceAdminVisits()
_plot_terms_service = None


def _plot_terms():
    global _plot_terms_service
    if _plot_terms_service is None:
        from DivineService.service_plot_payment_terms import servicePlotPaymentTerms
        _plot_terms_service = servicePlotPaymentTerms()
    return _plot_terms_service


# Plot payment-terms error code -> HTTP status (anything else is a 400).
_PLOT_TERMS_STATUS = {
    "not_found": 404,
    "plot_not_found": 404,
    "terms_from_website_booking": 409,
    "schedule_has_payments": 409,
    "payment_details_pending": 409,
}


@router.post("/signup", response_model=AdminOutDTO)
def admin_signup(request: Request, dto: AdminCreateDTO):
    """Creates the account and emails a signup OTP to the given address. The DTO
    only accepts @divinevisioninfra.com addresses, and the account cannot log in
    until POST /admin/verify-signup confirms the OTP - so an outsider who finds
    this URL still cannot self-activate an admin account."""
    start = time.monotonic()
    try:
        client_ip = request.client.host if request.client else None
        user = _admin_service.signup(dto, created_by=client_ip)
        return AdminOutDTO(
            id=user.id,
            full_name=user.full_name,
            employee_id=user.employee_id,
            email=user.email,
            created_by=user.created_by,
            created_date=user.created_date,
            last_updated_by=user.last_updated_by,
            last_updated_date=user.last_updated_date,
        )
    except ValueError as e:
        status_code = 429 if str(e) == "too_many_requests" else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except Exception:
        logger.exception("admin_signup_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_signup_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/verify-signup", response_model=MessageDTO)
def admin_verify_signup(dto: AdminVerifySignupDTO):
    start = time.monotonic()
    try:
        # "pending_approval": a super admin must approve before login works.
        # "approved": a super admin's own account, active immediately.
        return MessageDTO(message=_admin_service.verify_signup(dto.email, dto.otp))
    except ValueError as e:
        status_code = 429 if str(e) == "too_many_attempts" else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception:
        logger.exception("admin_verify_signup_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_verify_signup_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/resend-signup-otp", response_model=MessageDTO)
def admin_resend_signup_otp(dto: AdminResendSignupOtpDTO):
    start = time.monotonic()
    try:
        _admin_service.resend_signup_otp(dto.email)
        return MessageDTO(message="otp_sent")
    except ValueError as e:
        status_code = 429 if str(e) == "too_many_requests" else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception:
        logger.exception("admin_resend_signup_otp_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_resend_signup_otp_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/login", response_model=AdminTokenDTO)
def admin_login(dto: AdminLoginDTO):
    start = time.monotonic()
    try:
        tokens = _admin_service.login(dto)
        return AdminTokenDTO(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_in=tokens["expires_in"],
        )
    except ValueError as e:
        if str(e) in ("email_not_verified", "approval_pending", "approval_rejected"):
            raise HTTPException(status_code=403, detail=str(e))
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        logger.exception("admin_login_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_login_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/refresh", response_model=AdminAccessTokenDTO)
def admin_refresh(dto: AdminRefreshDTO):
    start = time.monotonic()
    try:
        result = _admin_service.refresh(dto.refresh_token)
        return AdminAccessTokenDTO(access_token=result["access_token"], expires_in=result["expires_in"])
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_refresh_token")
    except Exception:
        logger.exception("admin_refresh_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_refresh_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/profile", response_model=AdminProfileDTO)
def admin_profile(current_admin: dict = Depends(get_current_admin)):
    """Current admin profile for the admin panel header/profile page."""
    start = time.monotonic()
    try:
        return _admin_service.profile(current_admin["sub"])
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_profile_failed admin_id=%s", current_admin.get("sub"))
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_profile_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/profile/photo", response_model=AdminProfileDTO)
def upload_admin_profile_photo(
    file: UploadFile = File(...),
    current_admin: dict = Depends(get_current_admin),
):
    """Upload and apply the current admin's profile photo."""
    start = time.monotonic()
    try:
        file_bytes = file.file.read()
        return _admin_service.upload_profile_photo(current_admin["sub"], file_bytes, file.content_type)
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="not_found")
        raise HTTPException(status_code=400, detail=code)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("admin_profile_photo_upload_failed admin_id=%s", current_admin.get("sub"))
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_profile_photo_upload_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/approvals", response_model=AdminApprovalListResponseDTO)
def list_admin_approvals(
    status: Literal["pending", "approved", "rejected"] = Query("pending"),
    current_admin: dict = Depends(get_current_admin),
):
    """Super admins only: email-verified admin accounts awaiting (or past) approval."""
    start = time.monotonic()
    try:
        return {"items": _admin_service.list_approval_requests(current_admin.get("sub"), status=status)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="super_admin_required")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError:
        logger.exception("admin_list_approvals_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    except Exception:
        logger.exception("admin_list_approvals_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_approvals_latency_ms=%.2f", (time.monotonic() - start) * 1000)


def _decide_admin_approval(admin_id: str, target_id: str, decision: str, note: Optional[str]):
    start = time.monotonic()
    try:
        return _admin_service.decide_approval(admin_id, target_id, decision, note=note)
    except PermissionError:
        raise HTTPException(status_code=403, detail="super_admin_required")
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail=code)
        if code == "already_decided":
            raise HTTPException(status_code=409, detail=code)
        raise HTTPException(status_code=400, detail=code)
    except RuntimeError:
        logger.exception("admin_decide_approval_failed target=%s", target_id)
        raise HTTPException(status_code=500, detail="internal_error")
    except Exception:
        logger.exception("admin_decide_approval_failed target=%s", target_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_decide_approval_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/approvals/{admin_id}/approve", response_model=AdminApprovalItemDTO)
def approve_admin(admin_id: str, dto: Optional[AdminApprovalDecisionDTO] = None,
                  current_admin: dict = Depends(get_current_admin)):
    return _decide_admin_approval(current_admin.get("sub"), admin_id, "approved", dto.note if dto else None)


@router.post("/approvals/{admin_id}/reject", response_model=AdminApprovalItemDTO)
def reject_admin(admin_id: str, dto: Optional[AdminApprovalDecisionDTO] = None,
                 current_admin: dict = Depends(get_current_admin)):
    return _decide_admin_approval(current_admin.get("sub"), admin_id, "rejected", dto.note if dto else None)


@router.post("/forgot-password", response_model=MessageDTO)
def admin_forgot_password(dto: ForgotPasswordDTO):
    # Always 200 with the same message on a well-formed request, whether or not the
    # email is registered - see servicePasswordReset.forgot_password's own docstring
    # for why that check can't leak account existence.
    start = time.monotonic()
    try:
        _password_reset_service.forgot_password(dto.email)
        return MessageDTO(message="If that email is registered, an OTP has been sent.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_forgot_password_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_forgot_password_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/reset-password", response_model=MessageDTO)
def admin_reset_password(dto: ResetPasswordDTO):
    start = time.monotonic()
    try:
        _password_reset_service.reset_password(dto.email, dto.otp, dto.new_password)
        return MessageDTO(message="Password has been reset.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_reset_password_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_reset_password_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/customers", response_model=CustomerListResponseDTO)
def list_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200),
    source: Optional[Literal["WEBSITE", "BROKER_CHANNEL", "OFFLINE_IMPORT"]] = Query(None),
    status: Optional[Literal["ACTIVE", "BOOKED"]] = Query(None),
    sort: Literal["created_at", "-created_at", "full_name", "-full_name"] = Query("-created_at"),
    current_admin: dict = Depends(get_current_admin),
):
    start = time.monotonic()
    try:
        return _admin_customers_service.list_customers(
            search=search, source=source, status=status, sort=sort, page=page, page_size=page_size,
        )
    except Exception:
        logger.exception("admin_list_customers_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_customers_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/customers", response_model=CustomerListItemDTO, status_code=201)
def create_customer(dto: CustomerCreateDTO, current_admin: dict = Depends(get_current_admin)):
    start = time.monotonic()
    try:
        return _admin_customers_service.create_customer(dto, created_by=f"admin:{current_admin.get('sub')}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except Exception:
        logger.exception("admin_create_customer_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_create_customer_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/customers/{customer_id}", response_model=CustomerDetailDTO)
def get_customer(customer_id: str, current_admin: dict = Depends(get_current_admin)):
    """One customer with each of their plots: its payment plan, booking date,
    prices, amount received, schedule, and whether a demand letter can be
    issued. Covers website bookings and offline buyers' plots."""
    start = time.monotonic()
    try:
        return _plot_terms().customer_detail(customer_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_get_customer_failed customer_id=%s", customer_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_customer_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.put("/customers/{customer_id}/plots/{document_id}/payment-terms", response_model=CustomerPlotDTO)
def set_customer_plot_payment_terms(customer_id: str, document_id: str, dto: CustomerPlotTermsDTO,
                                    current_admin: dict = Depends(get_current_admin)):
    """Link a payment plan and booking date to an offline buyer's plot, and its
    prices and amount received to date once known (they turn on the demand
    letter). Sent again, it replaces the plot's terms. 409
    terms_from_website_booking for a website booking; 409 schedule_has_payments
    once an instalment was paid in the app; 400 with a code for bad input."""
    start = time.monotonic()
    try:
        return _plot_terms().set_terms(
            customer_id, document_id, payment_plan=dto.payment_plan, booking_date=dto.booking_date,
            sale_price=dto.sale_price, plc=dto.plc, ifms=dto.ifms, cam=dto.cam,
            amount_received=dto.amount_received, set_by=f"admin:{current_admin.get('sub')}",
        )
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=_PLOT_TERMS_STATUS.get(code, 400), detail=code)
    except Exception:
        logger.exception("admin_set_plot_terms_failed customer_id=%s document_id=%s", customer_id, document_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_set_plot_terms_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/customers/{customer_id}/plots/{document_id}/demand-letter")
def get_customer_plot_demand_letter(customer_id: str, document_id: str,
                                    current_admin: dict = Depends(get_current_admin)):
    """The plot's Demand Letter PDF - the same letter the customer downloads.
    409 payment_details_pending until the plot has prices."""
    start = time.monotonic()
    try:
        pdf_bytes, filename = _plot_terms().demand_letter(customer_id, document_id)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=_PLOT_TERMS_STATUS.get(code, 400), detail=code)
    except Exception:
        logger.exception("admin_plot_demand_letter_failed customer_id=%s document_id=%s", customer_id, document_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_plot_demand_letter_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/brokers", response_model=BrokerListResponseDTO)
def list_brokers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200),
    project: Optional[Literal["suraksha-enclave", "ops-divine-greens"]] = Query(None),
    sort: Literal["created_at", "-created_at", "full_name", "-full_name"] = Query("-created_at"),
    current_admin: dict = Depends(get_current_admin),
):
    start = time.monotonic()
    try:
        return _admin_brokers_service.list_brokers(
            search=search, project=project, sort=sort, page=page, page_size=page_size,
        )
    except Exception:
        logger.exception("admin_list_brokers_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_brokers_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/visits", response_model=AdminVisitListResponseDTO)
def list_visits(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200, description="Matches customer name or project name"),
    origin_type: Optional[Literal["CUSTOMER", "CHANNEL_PARTNER"]] = Query(None),
    status: Optional[Literal["requested", "scheduled", "confirmed", "completed", "follow_up", "no_show", "converted", "cancelled"]] = Query(None),
    sort: Literal["visit_date", "-visit_date", "created_at", "-created_at", "customer_name", "-customer_name"] = Query("-visit_date"),
    current_admin: dict = Depends(get_current_admin),
):
    """Unified customer + channel-partner site visits for the admin panel's
    Site Visits page. Read-only display data - see DivineService/service_admin_visits.py."""
    start = time.monotonic()
    try:
        return _admin_visits_service.list_visits(
            search=search, origin_type=origin_type, status=status, sort=sort, page=page, page_size=page_size,
        )
    except RuntimeError:
        # Controlled failure from the service/persistence layer (db_error,
        # visit_row_shape_mismatch, ...) - never leak the internal reason to
        # the client, just log it and answer with a generic 500.
        logger.exception("admin_list_visits_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    except Exception:
        logger.exception("admin_list_visits_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_visits_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/visits/{visit_id}", response_model=AdminVisitListItemDTO)
def get_visit(visit_id: str, current_admin: dict = Depends(get_current_admin)):
    start = time.monotonic()
    try:
        return _admin_visits_service.get_visit(visit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except RuntimeError:
        logger.exception("admin_get_visit_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    except Exception:
        logger.exception("admin_get_visit_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_visit_latency_ms=%.2f", (time.monotonic() - start) * 1000)

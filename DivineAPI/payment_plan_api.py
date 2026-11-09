"""Payment plans: public catalogue + schedule preview for the booking wizard,
and the admin endpoint that sets a project's Offer of Possession date.

Public (no login) - plans are marketing information shown before sign-in:
    GET  /payment-plans?project_id=suraksha-enclave
    GET  /payment-plans/{code}            (code contains '/', e.g. PP1/FPP/10-15-25-25-15-10)
    POST /payment-plans/preview

Admin:
    PUT  /admin/projects/{project_id}/possession-date
"""
import logging
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from DivineService.auth import get_current_admin
from DivineService.service_payment_plans import servicePaymentPlans

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payment-plans", tags=["payment-plans"])
admin_router = APIRouter(prefix="/admin/projects", tags=["admin-payment-plans"])
_plan_service = servicePaymentPlans()

_NOT_FOUND = {"payment_plan_not_found"}
_BAD_INPUT = {
    "invalid_payment_plan", "invalid_sale_price", "invalid_plc_amount", "invalid_ifms_amount",
    "invalid_cam_amount", "total_amount_too_large", "invalid_booking_date", "payment_plan_not_for_project",
    "invalid_project_id", "invalid_possession_date",
}


class PaymentPlanPreviewRequestDTO(BaseModel):
    plan_code: str = Field(..., min_length=1, max_length=80)
    sale_price: float = Field(..., gt=0, le=500000000, description="Basic Sale Price (INR)")
    plc_amount: float = Field(0, ge=0, le=500000000)
    ifms_amount: float = Field(0, ge=0, le=500000000)
    cam_amount: float = Field(0, ge=0, le=500000000)
    booking_date: Optional[date] = Field(None, description="Defaults to today (IST)")
    project_id: Optional[str] = Field(None, max_length=100)


class PossessionDateDTO(BaseModel):
    possession_offer_date: date


def _raise_for(e: ValueError):
    code = str(e)
    if code in _NOT_FOUND:
        raise HTTPException(status_code=404, detail=code)
    if code in _BAD_INPUT:
        raise HTTPException(status_code=400, detail=code)
    logger.warning("payment_plan_unexpected_value_error code=%s", code)
    raise HTTPException(status_code=400, detail="invalid_request")


@router.get("")
def list_payment_plans(project_id: Optional[str] = Query(None, max_length=100)):
    """Active plans for a project, in display order, each with its instalment
    rules and the document's worked example (sale price 50,00,000)."""
    try:
        plans = _plan_service.list_plans((project_id or "").strip() or None)
        return {"count": len(plans), "plans": plans}
    except ValueError as e:
        _raise_for(e)
    except Exception:
        logger.exception("payment_plans_list_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/preview")
def preview_payment_plan(dto: PaymentPlanPreviewRequestDTO):
    """Exact instalments (amount, due date, charges) for the customer's prices.
    The booking wizard uses `booking_amount` as the booking payment."""
    try:
        return _plan_service.preview(
            dto.plan_code.strip(), dto.sale_price, dto.plc_amount, dto.ifms_amount, dto.cam_amount,
            booking_date=dto.booking_date, project_id=(dto.project_id or "").strip() or None,
        )
    except ValueError as e:
        _raise_for(e)
    except Exception:
        logger.exception("payment_plan_preview_failed")
        raise HTTPException(status_code=500, detail="internal_error")


# Declared last: {code:path} would otherwise swallow '/preview'.
@router.get("/{code:path}")
def get_payment_plan(code: str):
    if len(code) > 80:
        raise HTTPException(status_code=404, detail="payment_plan_not_found")
    try:
        return _plan_service.get_public_plan(code.strip())
    except ValueError as e:
        _raise_for(e)
    except Exception:
        logger.exception("payment_plan_get_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@admin_router.put("/{project_id}/possession-date")
def set_possession_date(project_id: str, dto: PossessionDateDTO,
                        current_admin: dict = Depends(get_current_admin)):
    """Sets the project's Offer of Possession date. Every unpaid 'On Offer of
    Possession' instalment of that project gets its due date (and from then on
    the normal reminders)."""
    try:
        return _plan_service.set_possession_date(project_id, dto.possession_offer_date,
                                                 updated_by=f"admin:{current_admin.get('sub')}")
    except ValueError as e:
        _raise_for(e)
    except Exception:
        logger.exception("possession_date_update_failed project_id=%s", project_id)
        raise HTTPException(status_code=500, detail="internal_error")


# ---------------------------------------------------------------------------
# Admin: manage plans (add any plan, new version of a plan, edit / deactivate)
# Plan codes contain '/', so the code travels in the JSON body, not the path.
# ---------------------------------------------------------------------------
admin_plans_router = APIRouter(prefix="/admin/payment-plans", tags=["admin-payment-plans"])

_CONFLICT = {"payment_plan_exists", "short_code_in_use", "payment_plan_version_conflict"}
_ADMIN_BAD_INPUT = {
    "invalid_plan_code", "invalid_short_code", "invalid_plan_name", "invalid_description",
    "invalid_project_id", "invalid_sort_order", "invalid_instalments", "invalid_payment_plan",
    "sale_price_too_small",
}


class PlanInstalmentInDTO(BaseModel):
    label: str = Field(..., min_length=1, max_length=150)
    percent: float = Field(..., gt=0, le=100, description="% of the Basic Sale Price, up to 3 decimals")
    due_trigger: Literal["booking", "possession"] = "booking"
    due_days: Optional[int] = Field(0, ge=0, le=3650, description="days after booking / possession")
    charge_head: Optional[Literal["PLC", "IFMS", "CAM", "ALL"]] = None


class PlanCreateDTO(BaseModel):
    code: str = Field(..., min_length=2, max_length=80)
    short_code: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=2, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    project_id: Optional[str] = Field(None, max_length=100)
    sort_order: int = Field(0, ge=0, le=1000)
    instalments: List[PlanInstalmentInDTO] = Field(..., min_length=1, max_length=24)


class PlanNewVersionDTO(BaseModel):
    code: str = Field(..., min_length=2, max_length=80)
    instalments: List[PlanInstalmentInDTO] = Field(..., min_length=1, max_length=24)
    short_code: Optional[str] = Field(None, min_length=1, max_length=20)
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    project_id: Optional[str] = Field(None, max_length=100)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class PlanUpdateDTO(BaseModel):
    code: str = Field(..., min_length=2, max_length=80)
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)
    is_active: Optional[bool] = None


def _admin_raise_for(e: ValueError):
    code = str(e)
    if code in _CONFLICT:
        raise HTTPException(status_code=409, detail=code)
    if code in _ADMIN_BAD_INPUT:
        raise HTTPException(status_code=400, detail=code)
    _raise_for(e)


def _dump(dto: BaseModel) -> dict:
    return dto.model_dump()


@admin_plans_router.get("")
def admin_list_payment_plans(project_id: Optional[str] = Query(None, max_length=100),
                             current_admin: dict = Depends(get_current_admin)):
    """Every plan version, active and retired (newest version first per code)."""
    try:
        plans = _plan_service.admin_list((project_id or "").strip() or None)
        return {"count": len(plans), "plans": plans}
    except ValueError as e:
        _admin_raise_for(e)
    except Exception:
        logger.exception("admin_payment_plans_list_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@admin_plans_router.post("/validate")
def admin_validate_payment_plan(dto: PlanCreateDTO, current_admin: dict = Depends(get_current_admin)):
    """Dry run - checks the plan and returns it with the worked example
    (sale price 50,00,000 + PLC 1,00,000 + IFMS 50,000 + CAM 75,000). Saves nothing."""
    try:
        return _plan_service.validate_definition(_dump(dto))
    except ValueError as e:
        _admin_raise_for(e)
    except Exception:
        logger.exception("admin_payment_plan_validate_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@admin_plans_router.post("", status_code=201)
def admin_create_payment_plan(dto: PlanCreateDTO, current_admin: dict = Depends(get_current_admin)):
    """Add a new plan (version 1, active immediately - customers see it in the
    booking dropdown within a minute)."""
    try:
        return _plan_service.create_plan(_dump(dto), admin_id=current_admin.get("sub"))
    except ValueError as e:
        _admin_raise_for(e)
    except Exception:
        logger.exception("admin_payment_plan_create_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@admin_plans_router.post("/versions", status_code=201)
def admin_new_payment_plan_version(dto: PlanNewVersionDTO, current_admin: dict = Depends(get_current_admin)):
    """Change a plan's instalments: saved as a new version that becomes active.
    Customers who already booked keep the version they paid against."""
    try:
        body = _dump(dto)
        return _plan_service.new_version(body.pop("code"), body, admin_id=current_admin.get("sub"))
    except ValueError as e:
        _admin_raise_for(e)
    except Exception:
        logger.exception("admin_payment_plan_version_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@admin_plans_router.patch("")
def admin_update_payment_plan(dto: PlanUpdateDTO, current_admin: dict = Depends(get_current_admin)):
    """Rename / re-describe / re-order a plan, or switch it on or off
    (is_active=false hides it from new bookings; existing bookings unaffected)."""
    try:
        body = _dump(dto)
        return _plan_service.update_plan(body.pop("code"), body, admin_id=current_admin.get("sub"))
    except ValueError as e:
        _admin_raise_for(e)
    except Exception:
        logger.exception("admin_payment_plan_update_failed")
        raise HTTPException(status_code=500, detail="internal_error")

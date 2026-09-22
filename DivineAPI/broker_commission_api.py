import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    BrokerCommissionCreateDTO,
    AdminCommissionPaymentResponseDTO,
    BrokerCommissionCreateResponseDTO,
    BrokerCommissionListResponseDTO,
)
from DivineService import serviceBrokerCommission
from DivineService.auth import get_current_admin_or_broker

router = APIRouter(tags=["broker-commissions"])
_commission_service = serviceBrokerCommission()


def _require_broker(current_user: dict, broker_id: str) -> None:
    if current_user["role"] != "broker":
        raise HTTPException(status_code=403, detail="broker_only")
    if current_user["sub"] != broker_id:
        raise HTTPException(status_code=403, detail="forbidden")


def _require_admin(current_user: dict) -> None:
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin_only")


def _create_commission(dto: BrokerCommissionCreateDTO, source: str, current_user: dict):
    try:
        if source == "admin":
            _require_admin(current_user)
            commission, payment = _commission_service.initiate_admin_commission_payment(
                brokerId=dto.brokerId,
                serialNumber=dto.serialNumber,
                unitAddress=dto.unitAddress,
                customerName=dto.customerName,
                township=dto.township,
                saleValue=dto.saleValue,
                commissionAmount=dto.commissionAmount,
                transactionMode=dto.transactionMode,
                success_url=os.getenv("ZOHO_PAYMENTS_SUCCESS_URL"),
                failure_url=os.getenv("ZOHO_PAYMENTS_FAILURE_URL"),
            )
            return {"success": True, "commission": commission, "payment": payment}
        else:
            _require_broker(current_user, dto.brokerId)
            commission = _commission_service.create_paid_commission(
                brokerId=dto.brokerId,
                serialNumber=dto.serialNumber,
                unitAddress=dto.unitAddress,
                customerName=dto.customerName,
                township=dto.township,
                saleValue=dto.saleValue,
                commissionAmount=dto.commissionAmount,
                transactionMode=dto.transactionMode,
            )
        return {"success": True, "commission": commission}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/api/broker/commissions", response_model=BrokerCommissionCreateResponseDTO)
def create_broker_commission(dto: BrokerCommissionCreateDTO,
                             current_user: dict = Depends(get_current_admin_or_broker)):
    return _create_commission(dto, source="broker", current_user=current_user)


@router.post("/api/admin/commission-payments", response_model=AdminCommissionPaymentResponseDTO)
def create_admin_commission_payment(dto: BrokerCommissionCreateDTO,
                                    current_user: dict = Depends(get_current_admin_or_broker)):
    return _create_commission(dto, source="admin", current_user=current_user)


@router.get("/api/broker/commissions", response_model=BrokerCommissionListResponseDTO)
def list_broker_commissions(brokerId: str = Query(..., min_length=1, max_length=80),
                            current_user: dict = Depends(get_current_admin_or_broker)):
    try:
        if current_user["role"] == "broker" and current_user["sub"] != brokerId:
            raise HTTPException(status_code=403, detail="forbidden")
        return _commission_service.list_for_broker(brokerId)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

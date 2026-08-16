from fastapi import APIRouter, Depends, HTTPException
from typing import List

from DivineDTO.models import VisitScheduleRequestDTO, VisitOutDTO
from DivineService import serviceVisit
from DivineService.auth import get_current_user

router = APIRouter(prefix="/visits", tags=["visits"])
_visit_service = serviceVisit()


def _to_visit_out(record) -> VisitOutDTO:
    return VisitOutDTO(
        id=record.id,
        broker_id=record.broker_id,
        customer_name=record.customer_name,
        customer_contact=record.customer_contact,
        date=record.visit_date.isoformat() if hasattr(record.visit_date, "isoformat") else str(record.visit_date),
        time=record.visit_time,
        notes=record.notes,
        status=record.status,
        created_date=record.created_date,
    )


def _require_broker(current_user: dict) -> str:
    if current_user["role"] != "broker":
        raise HTTPException(status_code=403, detail="visits_broker_only")
    return current_user["sub"]


@router.post("", response_model=VisitOutDTO)
def schedule_visit(dto: VisitScheduleRequestDTO, current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        record = _visit_service.schedule_visit(
            broker_id=broker_id,
            customer_name=dto.customer_name,
            customer_contact=dto.customer_contact,
            visit_date=dto.date,
            visit_time=dto.time,
            notes=dto.notes,
        )
        return _to_visit_out(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("", response_model=List[VisitOutDTO])
def list_visits(current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        records = _visit_service.list_visits(broker_id)
        return [_to_visit_out(r) for r in records]
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/history", response_model=List[VisitOutDTO])
def get_visit_history(current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        records = _visit_service.get_visit_history(broker_id)
        return [_to_visit_out(r) for r in records]
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.delete("/{visit_id}", response_model=VisitOutDTO)
def cancel_visit(visit_id: str, current_user: dict = Depends(get_current_user)):
    _require_broker(current_user)
    try:
        record = _visit_service.cancel_visit(visit_id, requester_id=current_user["sub"])
        return _to_visit_out(record)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
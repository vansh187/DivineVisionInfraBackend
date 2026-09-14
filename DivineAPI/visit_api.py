import logging
from fastapi import APIRouter, Depends, HTTPException
from typing import List

from DivineDTO.models import (
    VisitScheduleRequestDTO, VisitCompleteRequestDTO, VisitOutDTO, VisitRequestCallbackDTO,
)
from DivineService import serviceVisit
from DivineService.auth import get_current_user
from Divinepersistence.persistence_db import format_date_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/visits", tags=["visits"])
_visit_service = serviceVisit()


def _to_visit_out(record) -> VisitOutDTO:
    # A "requested" visit (website self-service, no broker/exact slot yet) shows
    # date/time as null rather than the internally-stored placeholder date used
    # for sorting - see serviceVisit._resolve_preferred_window_date.
    is_requested = record.status == "requested"
    return VisitOutDTO(
        id=record.id,
        broker_id=getattr(record, "broker_id", None),
        customer_name=record.customer_name,
        customer_contact=record.customer_contact,
        project=getattr(record, "project_name", None) or None,
        date=None if is_requested else format_date_value(record.visit_date),
        time=None if is_requested else getattr(record, "visit_time", None),
        notes=record.notes,
        status=record.status,
        source="website" if getattr(record, "origin_type", None) == "CUSTOMER" else "broker",
        created_date=record.created_date,
    )


def _require_broker(current_user: dict) -> str:
    if current_user["role"] != "broker":
        raise HTTPException(status_code=403, detail="visits_broker_only")
    return current_user["sub"]


@router.post("/request", response_model=VisitOutDTO, status_code=201)
def request_callback(dto: VisitRequestCallbackDTO):
    """Public website 'Request a callback' form - no login required. Subject to
    the app's default IP-based rate limit (not exempted) since it's an
    unauthenticated write endpoint."""
    try:
        record = _visit_service.request_callback(
            project=dto.project,
            preferred_window=dto.preferred_window,
            customer_name=dto.customer_name,
            customer_contact=dto.customer_contact,
            customer_email=dto.customer_email,
            notes=dto.notes,
        )
        return _to_visit_out(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("visit_request_callback_failed")
        raise HTTPException(status_code=500, detail="internal_error")


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
            project=dto.project,
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


@router.patch("/{visit_id}", response_model=VisitOutDTO)
def complete_visit(visit_id: str, dto: VisitCompleteRequestDTO,
                   current_user: dict = Depends(get_current_user)):
    """Broker closes out a site visit after the meeting, recording an outcome note.
    Only the owning broker; only while the visit is still 'scheduled'."""
    _require_broker(current_user)
    try:
        record = _visit_service.complete_visit(
            visit_id, requester_id=current_user["sub"], notes=dto.notes,
        )
        return _to_visit_out(record)
    except PermissionError:
        raise HTTPException(status_code=403, detail="visit_owner_only")
    except ValueError as e:
        if str(e) == "visit_not_scheduled":
            raise HTTPException(status_code=409, detail="visit_not_scheduled")
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
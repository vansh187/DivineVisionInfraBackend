import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from typing import List

from DivineDTO.models import (
    VisitScheduleRequestDTO, VisitCompleteRequestDTO, VisitOutDTO, VisitRequestCallbackDTO,
)
from DivineService import serviceVisit
from DivineService.auth import get_current_user, get_optional_customer
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
        customer_email=getattr(record, "customer_email", None),
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


def _require_customer(current_user: dict) -> str:
    if current_user["role"] != "customer":
        raise HTTPException(status_code=403, detail="visits_customer_only")
    return current_user["sub"]


@router.post("/request", response_model=VisitOutDTO, status_code=201)
def request_callback(dto: VisitRequestCallbackDTO, current_customer: dict = Depends(get_optional_customer)):
    """Public website 'Request a callback' form - no login required. Subject to
    the app's default IP-based rate limit (not exempted) since it's an
    unauthenticated write endpoint. When called with a valid customer bearer
    token, the visit is tagged with that customer_id so it later shows up
    precisely under GET /visits/mine; an anonymous call (no token, or a
    token that isn't a valid customer one) still succeeds exactly as before,
    matched later by email only."""
    start = time.monotonic()
    customer_id = current_customer["sub"] if current_customer else None
    try:
        record = _visit_service.request_callback(
            project=dto.project,
            preferred_window=dto.preferred_window,
            customer_name=dto.customer_name,
            customer_contact=dto.customer_contact,
            customer_email=dto.customer_email,
            notes=dto.notes,
            customer_id=customer_id,
        )
        return _to_visit_out(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("visit_request_callback_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("visit_request_callback_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("", response_model=VisitOutDTO)
def schedule_visit(dto: VisitScheduleRequestDTO, current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        record = _visit_service.schedule_visit(
            broker_id=broker_id,
            customer_name=dto.customer_name,
            customer_contact=dto.customer_contact,
            customer_email=dto.customer_email,
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


@router.get("/mine", response_model=List[VisitOutDTO])
def list_my_visits(current_user: dict = Depends(get_current_user)):
    """Every site visit the signed-in customer has, whether self-requested from
    the website or booked for them by a broker - matched by the email on their
    account (see serviceVisit.list_mine). Returns [] on no matches, never 404."""
    start = time.monotonic()
    customer_id = _require_customer(current_user)
    try:
        records = _visit_service.list_mine(customer_id)
        return [_to_visit_out(r) for r in records]
    except Exception:
        # Covers both a RuntimeError surfaced deliberately by serviceVisit.list_mine
        # and any other unexpected failure - same response either way, so one
        # handler instead of two identical branches.
        logger.exception("visits_mine_failed customer_id=%s", customer_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("visits_mine_latency_ms=%.2f", (time.monotonic() - start) * 1000)


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
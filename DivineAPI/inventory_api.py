from fastapi import APIRouter, Depends, HTTPException, Query

from DivineDTO.models import (
    InventorySearchResponseDTO, NLSearchRequestDTO, NLSearchResponseDTO,
    InventoryViewRequestDTO, InventoryViewResponseDTO, RecommendationResponseDTO,
    InventoryUnitOutDTO, ReserveInventoryResponseDTO, MyReservationsResponseDTO,
    InventoryBookRepairRequestDTO, InventoryUnbookRequestDTO, InventoryBookingResultDTO,
)
from DivineService import serviceInventory
from DivineService.auth import get_current_user

router = APIRouter(prefix="/inventory", tags=["inventory"])
_inventory_service = serviceInventory()


def _require_broker(current_user: dict) -> str:
    if current_user["role"] != "broker":
        raise HTTPException(status_code=403, detail="inventory_reservation_broker_only")
    return current_user["sub"]


@router.get("/search", response_model=InventorySearchResponseDTO)
def search_inventory(
    project_name: str = Query(None, max_length=150),
    city: str = Query(None, max_length=150),
    unit_type: str = Query(None, max_length=20),
    status: str = Query(None, max_length=20),
    min_area_sqyd: float = Query(None, ge=0),
    max_area_sqyd: float = Query(None, ge=0),
    min_budget: float = Query(None, ge=0),
    max_budget: float = Query(None, ge=0),
    limit: int = Query(20, ge=1),
    offset: int = Query(0, ge=0),
):
    try:
        return _inventory_service.search(
            project_name=project_name, city=city, unit_type=unit_type, status=status,
            min_area_sqyd=min_area_sqyd, max_area_sqyd=max_area_sqyd,
            min_budget=min_budget, max_budget=max_budget, limit=limit, offset=offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/search/nl", response_model=NLSearchResponseDTO)
def search_inventory_nl(body: NLSearchRequestDTO):
    try:
        return _inventory_service.search_natural_language(query=body.query, session_id=body.session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/view", response_model=InventoryViewResponseDTO)
def record_inventory_view(inventory_id: str, body: InventoryViewRequestDTO):
    try:
        return _inventory_service.record_view(
            inventory_id=inventory_id, lead_id=body.lead_id, session_id=body.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/recommendations", response_model=RecommendationResponseDTO)
def get_recommendations(
    lead_id: str = Query(None),
    session_id: str = Query(None),
    limit: int = Query(10, ge=1),
):
    try:
        return _inventory_service.recommend(lead_id=lead_id, session_id=session_id, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/reserved/mine", response_model=MyReservationsResponseDTO)
def list_my_reservations(current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.list_my_reservations(broker_id=broker_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/reserve", response_model=ReserveInventoryResponseDTO)
def reserve_inventory(inventory_id: str, current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.reserve_unit(inventory_id=inventory_id, broker_id=broker_id)
    except ValueError as e:
        if str(e) == "unit_not_available":
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/release", response_model=InventoryUnitOutDTO)
def release_inventory_reservation(inventory_id: str, current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.release_reservation(inventory_id=inventory_id, broker_id=broker_id)
    except ValueError as e:
        if str(e) == "not_reserved_by_you":
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/mark-sold", response_model=InventoryUnitOutDTO)
def mark_inventory_sold(inventory_id: str, current_user: dict = Depends(get_current_user)):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.mark_sold(inventory_id=inventory_id, broker_id=broker_id)
    except ValueError as e:
        if str(e) == "not_reserved_by_you":
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


# --- staff booking repair (broker bearer token) -------------------------------
# This backend has no admin identity, so these reconciliation endpoints reuse the
# broker role. Customers never touch them - a customer booking is flipped
# automatically when their payment settles (see servicePayment).

@router.post("/{inventory_id}/book", response_model=InventoryBookingResultDTO)
def repair_book_unit(
    inventory_id: str,
    body: InventoryBookRepairRequestDTO,
    current_user: dict = Depends(get_current_user),
):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.book_unit_repair(
            inventory_id=inventory_id, payment_id=body.payment_id, actor_id=broker_id,
        )
    except ValueError as e:
        if str(e) == "unit_not_available":
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/unbook", response_model=InventoryBookingResultDTO)
def repair_unbook_unit(
    inventory_id: str,
    body: InventoryUnbookRequestDTO,
    current_user: dict = Depends(get_current_user),
):
    broker_id = _require_broker(current_user)
    try:
        return _inventory_service.unbook_unit(inventory_id=inventory_id, actor_id=broker_id)
    except ValueError as e:
        if str(e) == "unit_not_booked":
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

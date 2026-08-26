from fastapi import APIRouter, HTTPException, Query

from DivineDTO.models import (
    InventorySearchResponseDTO, NLSearchRequestDTO, NLSearchResponseDTO,
    InventoryViewRequestDTO, InventoryViewResponseDTO, RecommendationResponseDTO,
)
from DivineService import serviceInventory

router = APIRouter(prefix="/inventory", tags=["inventory"])
_inventory_service = serviceInventory()


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

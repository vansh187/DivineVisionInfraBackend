from fastapi import APIRouter, HTTPException, Query

from DivineDTO.models import MarketTrendListDTO
from DivineService import serviceMarketTrend

router = APIRouter(prefix="/market-trends", tags=["market-trends"])
_market_trend_service = serviceMarketTrend()


@router.get("", response_model=MarketTrendListDTO)
def list_market_trends(
    city: str = Query(None, max_length=150),
    locality: str = Query(None, max_length=150),
    property_type: str = Query(None, max_length=150),
    limit: int = Query(20, ge=1),
):
    try:
        return _market_trend_service.list_market_trends(
            city=city, locality=locality, property_type=property_type, limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

import logging
import time
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query

from DivineDTO.models import (
    RevenueTransactionListResponseDTO, RevenueTransactionDetailDTO, RevenueSummaryDTO,
)
from DivineService import serviceRevenue
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/revenue", tags=["admin", "revenue"])
_revenue_service = serviceRevenue()

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _map_value_error(e: ValueError):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("/summary", response_model=RevenueSummaryDTO)
def get_revenue_summary(
    date_from: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="YYYY-MM-DD, inclusive"),
    date_to: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="YYYY-MM-DD, inclusive"),
    current_admin: dict = Depends(get_current_admin),
):
    """Stat-card totals for the admin panel's Revenue tab. Admin-only - never
    returns anything for an unauthenticated or non-admin caller."""
    start = time.monotonic()
    try:
        return _revenue_service.get_summary(date_from=date_from, date_to=date_to)
    except ValueError as e:
        _map_value_error(e)
    except Exception:
        logger.exception("admin_get_revenue_summary_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_revenue_summary_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/transactions", response_model=RevenueTransactionListResponseDTO)
def list_revenue_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200, description="Matches booking id, customer name, or project name"),
    status: Optional[Literal["captured", "cash_recorded", "refund_pending", "refunded"]] = Query(None),
    method: Optional[Literal["zoho", "cash", "rtgs_neft", "razorpay"]] = Query(None),
    date_from: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="YYYY-MM-DD, inclusive"),
    date_to: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="YYYY-MM-DD, inclusive"),
    current_admin: dict = Depends(get_current_admin),
):
    """Paginated, filterable transaction list for the admin panel's Revenue
    tab. Admin-only. Every settled payment (status='paid') is returned exactly
    once, bucketed into one of the four `status` values."""
    start = time.monotonic()
    try:
        return _revenue_service.list_transactions(
            search=search, status=status, method=method,
            date_from=date_from, date_to=date_to, page=page, page_size=page_size,
        )
    except ValueError as e:
        _map_value_error(e)
    except Exception:
        logger.exception("admin_list_revenue_transactions_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_list_revenue_transactions_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.get("/transactions/{transaction_id}", response_model=RevenueTransactionDetailDTO)
def get_revenue_transaction(transaction_id: str, current_admin: dict = Depends(get_current_admin)):
    """Detail view for one revenue transaction. Admin-only."""
    start = time.monotonic()
    try:
        return _revenue_service.get_transaction(transaction_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_get_revenue_transaction_failed transaction_id=%s", transaction_id)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_get_revenue_transaction_latency_ms=%.2f", (time.monotonic() - start) * 1000)

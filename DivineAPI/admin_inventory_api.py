import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from DivineDTO.models import (
    AdminInventoryExportResponseDTO, AdminInventoryListResponseDTO, AdminInventoryProjectsResponseDTO,
    AdminInventoryStatusUpdateDTO, AdminInventoryUnitDetailDTO, InventoryStatusLiteral,
)
from DivineService.auth import get_current_admin
from DivineService.inventory_export import CSV_COLUMN_KEYS
from DivineService.service_admin_inventory import (
    ExportTooLargeError, InventoryStatusError, serviceAdminInventory,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/inventory", tags=["admin", "inventory"])
_service = serviceAdminInventory()


@router.get("/projects", response_model=AdminInventoryProjectsResponseDTO)
def list_inventory_projects(current_admin: dict = Depends(get_current_admin)):
    """Every project that has plots, with plot counts per status. Admin-only."""
    try:
        return _service.list_projects()
    except Exception:
        logger.exception("admin_inventory_projects_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("", response_model=AdminInventoryListResponseDTO)
def list_inventory(
    project_name: Optional[str] = Query(None, max_length=150, description="Exact project name"),
    status: Optional[InventoryStatusLiteral] = Query(None),
    search: Optional[str] = Query(None, max_length=50, description="Matches plot number or block"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: dict = Depends(get_current_admin),
):
    """Plots of a project, each with its status and its latest status change. Admin-only."""
    try:
        return _service.list_units(project_name=project_name, status=status, search=search,
                                   page=page, page_size=page_size)
    except Exception:
        logger.exception("admin_inventory_list_failed")
        raise HTTPException(status_code=500, detail="internal_error")


# Declared before /{inventory_id} so "export.csv" isn't read as a plot id.
@router.get("/export.csv", response_class=Response,
            responses={200: {"content": {"text/csv": {}}, "description": "The CSV file"}})
def export_inventory_csv(
    project_name: Optional[str] = Query(None, max_length=150, description="Exact project name"),
    status: Optional[InventoryStatusLiteral] = Query(None),
    search: Optional[str] = Query(None, max_length=50, description="Matches plot number or block"),
    columns: Optional[List[str]] = Query(
        None, max_length=len(CSV_COLUMN_KEYS),
        description="Columns to include, by key (repeat the parameter). The file keeps its own column "
                    "order. Default: the client's 20 sheet columns.",
    ),
    current_admin: dict = Depends(get_current_admin),
):
    """The whole Inventory export as one CSV download, with the same filters as
    the list: every matching plot, read in one go (no paging), with the columns
    the admin chose. Text a spreadsheet would run as a formula starts with an
    apostrophe. Admin-only."""
    unknown = sorted(set(columns or ()) - set(CSV_COLUMN_KEYS))
    if unknown:
        raise HTTPException(status_code=422, detail={
            "code": "unknown_columns", "message": f"Unknown export columns: {', '.join(unknown)}.",
        })
    try:
        body = _service.export_csv(project_name=project_name, status=status, search=search, columns=columns)
    except ExportTooLargeError as e:
        raise HTTPException(status_code=413, detail={"code": "export_too_large", "message": e.message})
    except Exception:
        logger.exception("admin_inventory_export_csv_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    filename = f"inventory-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# Declared before /{inventory_id} so "export" isn't read as a plot id.
@router.get("/export", response_model=AdminInventoryExportResponseDTO)
def export_inventory(
    project_name: Optional[str] = Query(None, max_length=150, description="Exact project name"),
    status: Optional[InventoryStatusLiteral] = Query(None),
    search: Optional[str] = Query(None, max_length=50, description="Matches plot number or block"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    current_admin: dict = Depends(get_current_admin),
):
    """Rows for the Inventory CSV export, paged like the list and with the same
    filters: each plot with its company, and for a sold / booked plot its
    payment plan, buyer, co-applicant and booking date. Text values are
    CSV-safe already (see /export.csv, which returns the whole file in one
    request and is the better choice for a download). Admin-only."""
    try:
        return _service.export_units(project_name=project_name, status=status, search=search,
                                     page=page, page_size=page_size)
    except Exception:
        logger.exception("admin_inventory_export_failed")
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/{inventory_id}", response_model=AdminInventoryUnitDetailDTO)
def get_inventory_unit(inventory_id: str, current_admin: dict = Depends(get_current_admin)):
    """One plot with its full status history, newest first. Admin-only."""
    try:
        return _service.get_unit(inventory_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_inventory_get_failed inventory_id=%s", inventory_id)
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/{inventory_id}/status", response_model=AdminInventoryUnitDetailDTO)
def change_inventory_status(
    inventory_id: str,
    body: AdminInventoryStatusUpdateDTO,
    current_admin: dict = Depends(get_current_admin),
):
    """Set a plot to available / held / sold. Records the previous and new status,
    the admin's name and the time in the plot's history. Admin-only.

    409 `status_changed` when the plot is no longer at `expected_status`;
    409 `partner_reservation` / `booking` / `buyer_linked` when the plot is locked
    to another flow; 400 `status_unchanged` when it already has that status."""
    try:
        return _service.change_status(
            inventory_id=inventory_id, new_status=body.status, expected_status=body.expected_status,
            admin_id=current_admin["sub"], admin_username=current_admin.get("username"), note=body.note,
        )
    except InventoryStatusError as e:
        raise HTTPException(status_code=e.http_status, detail={
            "code": e.code, "message": e.message, "current_status": e.current_status,
        })
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        logger.exception("admin_inventory_status_failed inventory_id=%s", inventory_id)
        raise HTTPException(status_code=500, detail="internal_error")

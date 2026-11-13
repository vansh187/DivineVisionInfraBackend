"""Admin CSV import - one endpoint, four entities:

    POST /admin/imports/{entity}     entity: customers | brokers | visits | payment-plans
    Authorization: Bearer <admin token>
    Idempotency-Key: <uuid>          one per file the admin chose

    { "rows": [ { "row": 2, ...fields }, ... ] }     at most 5,000 rows

200 {"created": n, "updated": m} - every row written in one transaction.
422 {"detail": {"code": "import_invalid", "message", "errors": [{row, field, message}]}} - nothing written.
409 {"detail": "idempotency_conflict"} - key already used with a different body.
413 more than 5,000 rows. Rules per entity: DivineService/service_admin_imports.py.
Export and the sample file are built by the admin panel from the list endpoints.
"""
import logging
import time
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from DivineAPI.payment_plan_api import _plan_service
from DivineService.auth import get_current_admin
from DivineService.service_admin_imports import AdminImportError, serviceAdminImports

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/imports", tags=["admin-imports"])
_import_service = serviceAdminImports(plan_persistence=_plan_service._persistence, plan_service=_plan_service)


class AdminImportRequestDTO(BaseModel):
    # Row objects are validated by the service, field by field, so every
    # problem in the file can be reported with its spreadsheet row - and so a
    # file over the row limit gets a 413, not a schema error.
    rows: List[Any]


@router.post("/{entity}")
def import_rows(
    entity: str,
    dto: AdminImportRequestDTO,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_admin: dict = Depends(get_current_admin),
):
    start = time.monotonic()
    try:
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(status_code=400, detail="idempotency_key_required")
        try:
            key = str(uuid.UUID(idempotency_key.strip()))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_idempotency_key")
        admin_id = str(current_admin.get("sub"))
        result, replayed = _import_service.run(entity, dto.rows, admin_id=admin_id, idempotency_key=key)
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result
    except AdminImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_import_failed entity=%s", entity)
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_import_latency_ms=%.2f entity=%s rows=%s",
                     (time.monotonic() - start) * 1000, entity, len(dto.rows))

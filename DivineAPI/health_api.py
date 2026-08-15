from fastapi import APIRouter
from fastapi.responses import JSONResponse
from DivineService import serviceHealth

router = APIRouter(tags=["health"])
_health_service = serviceHealth()


@router.get("/health")
def health():
    if not _health_service.check_connection():
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    return {"status": "ok", "database": "connected"}

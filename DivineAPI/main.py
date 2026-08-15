import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from time import time
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from DivineAPI.health_api import router as health_router
from DivineAPI.customer_api import router as customer_router
from DivineAPI.broker_api import router as broker_router
from DivineAPI.documents_api import router as documents_router
from DivineAPI.kyc_api import router as kyc_router
from DivineService import serviceHealth


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, calls: int = 10, per_seconds: int = 60):
        super().__init__(app)
        self.calls = calls
        self.per_seconds = per_seconds
        self.storage = {}

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        key = f"{client}:{request.url.path}"
        now = time()
        bucket = self.storage.get(key, [])
        # remove old
        bucket = [ts for ts in bucket if ts > now - self.per_seconds]
        if len(bucket) >= self.calls:
            return JSONResponse({"detail": "rate_limited"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)
        bucket.append(now)
        self.storage[key] = bucket
        return await call_next(request)


app = FastAPI(title="DivineAPI")
app.add_middleware(RateLimitMiddleware, calls=10, per_seconds=60)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort safety net: any exception not already caught and translated by a
    # route/dependency (middleware included) still returns a clean JSON 500 instead
    # of leaking a raw traceback or crashing the request.
    return JSONResponse({"detail": "internal_error"}, status_code=500)


@app.on_event("startup")
def startup():
    serviceHealth().init_db()


app.include_router(health_router)
app.include_router(customer_router)
app.include_router(broker_router)
app.include_router(documents_router)
app.include_router(kyc_router)

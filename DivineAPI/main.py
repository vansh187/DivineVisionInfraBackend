import logging
from collections import OrderedDict
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
from DivineAPI.payment_api import router as payment_router
from DivineService import serviceHealth


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy (this app's deployment target, Render), request.client.host
    # is the proxy's own IP for every request, not the real caller's - that would collapse
    # every distinct external client onto one shared rate-limit bucket. X-Forwarded-For's
    # first entry (nearest to the original client) is used when present.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    # Caps how many distinct client:path buckets are retained at once - without a bound,
    # a client that hits an endpoint once and never returns leaves its bucket in memory
    # for the life of the process. OrderedDict + evicting the oldest entry once the cap is
    # hit keeps this bounded without needing a background sweep thread.
    MAX_TRACKED_KEYS = 5000

    def __init__(self, app, calls: int = 10, per_seconds: int = 60):
        super().__init__(app)
        self.calls = calls
        self.per_seconds = per_seconds
        self.storage = OrderedDict()

    async def dispatch(self, request: Request, call_next):
        client = _client_ip(request)
        key = f"{client}:{request.url.path}"
        now = time()
        bucket = self.storage.get(key, [])
        # remove old
        bucket = [ts for ts in bucket if ts > now - self.per_seconds]
        if len(bucket) >= self.calls:
            self.storage[key] = bucket
            self.storage.move_to_end(key)
            return JSONResponse({"detail": "rate_limited"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)
        bucket.append(now)
        self.storage[key] = bucket
        self.storage.move_to_end(key)
        while len(self.storage) > self.MAX_TRACKED_KEYS:
            self.storage.popitem(last=False)
        return await call_next(request)


app = FastAPI(title="DivineAPI")
app.add_middleware(RateLimitMiddleware, calls=10, per_seconds=60)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_credentials=True combined with allow_origins=["*"] makes Starlette echo back
    # whatever Origin header the caller sends (the actual CORS spec forbids "*" + credentialed
    # requests together), which lets any third-party site make authenticated cross-origin
    # calls a victim's browser would otherwise block. This API authenticates via a Bearer
    # token in the Authorization header, not cookies, so credentialed CORS mode was never
    # needed - dropping it closes that gap without affecting how the frontend calls this API.
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
app.include_router(payment_router)

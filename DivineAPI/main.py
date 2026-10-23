import logging
import uuid
import weakref
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
from DivineAPI.admin_api import router as admin_router
from DivineAPI.documents_api import router as documents_router
from DivineAPI.kyc_api import router as kyc_router
from DivineAPI.payment_api import router as payment_router
from DivineAPI.visit_api import router as visit_router
from DivineAPI.market_trend_api import router as market_trend_router
from DivineAPI.broker_commission_api import router as broker_commission_router
from DivineAPI.chatbot_api import router as chatbot_router
from DivineAPI.zoho_admin_api import router as zoho_admin_router
from DivineAPI.loan_api import router as loan_router
from DivineAPI.inventory_api import router as inventory_router
from DivineAPI.jobs_api import router as jobs_router
from DivineAPI.booking_api import router as booking_router
from DivineAPI.admin_booking_kyc_api import router as admin_booking_kyc_router
from DivineAPI.admin_revenue_api import router as admin_revenue_router
from DivineAPI.admin_payments_api import router as admin_payments_router
from DivineAPI.admin_refunds_api import router as admin_refunds_router
from DivineAPI.admin_help_support_api import router as admin_help_support_router
from DivineService import serviceHealth
# Anonymous callers only - logged-in ones are keyed by verified user id.
from DivineService.client_ip import client_ip
from DivineService.env_config import positive_int_env
from DivineService.auth import rate_limit_identity


class RateLimitMiddleware(BaseHTTPMiddleware):
    RATE_LIMIT_EXEMPT_PATHS = {
        "/admin/customers", "/admin/brokers", "/admin/visits", "/admin/bookings",
        "/admin/profile", "/admin/revenue/summary", "/admin/revenue/transactions",
        "/admin/refunds",
    }

    # Caps how many distinct client:path buckets are retained at once - without a bound,
    # a client that hits an endpoint once and never returns leaves its bucket in memory
    # for the life of the process. OrderedDict + evicting the oldest entry once the cap is
    # hit keeps this bounded without needing a background sweep thread.
    MAX_TRACKED_KEYS = 5000

    # Credential endpoints - password guessing / OTP brute force targets - keep
    # the strict budget; everything else gets the general one.
    AUTH_PATHS = frozenset(
        f"/{role}/{action}"
        for role in ("admin", "customer", "broker")
        for action in ("login", "signup", "forgot-password", "reset-password")
    ) | {"/admin/verify-signup", "/admin/resend-signup-otp", "/admin/refresh"}

    # Live instances, so tests (and an ops hook) can reset counters through a
    # supported API instead of walking Starlette's private middleware stack.
    _instances = weakref.WeakSet()

    def __init__(self, app, calls: int = 60, per_seconds: int = 60, auth_calls: int = 10):
        super().__init__(app)
        self.calls = calls
        self.auth_calls = auth_calls
        self.per_seconds = per_seconds
        self.storage = OrderedDict()
        RateLimitMiddleware._instances.add(self)

    @classmethod
    def reset_all(cls) -> None:
        """Clears every live limiter's counters."""
        for instance in list(cls._instances):
            instance.storage.clear()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "GET" and path in self.RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)

        # Logged-in callers are bucketed by their verified identity - unspoofable
        # and independent of proxy headers, and colleagues behind one office IP
        # no longer share a budget. Only anonymous requests fall back to IP.
        identity = rate_limit_identity(request.headers.get("authorization"))
        client = f"user:{identity}" if identity else f"ip:{client_ip(request)}"
        key = f"{client}:{path}"
        limit = self.auth_calls if path in self.AUTH_PATHS else self.calls
        now = time()
        bucket = self.storage.get(key, [])
        # remove old
        bucket = [ts for ts in bucket if ts > now - self.per_seconds]
        if len(bucket) >= limit:
            self.storage[key] = bucket
            self.storage.move_to_end(key)
            return JSONResponse({"detail": "rate_limited"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)
        bucket.append(now)
        self.storage[key] = bucket
        self.storage.move_to_end(key)
        while len(self.storage) > self.MAX_TRACKED_KEYS:
            self.storage.popitem(last=False)
        return await call_next(request)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Correlation id for every request, always minted server-side - an inbound
    X-Request-ID is ignored, so ids are unique and can't be chosen by the caller
    (they land in audit rows and logs). Exposed to routes as
    request.state.request_id and returned as the X-Request-ID response header."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


DEFAULT_RATE_LIMIT_CALLS = 60
DEFAULT_RATE_LIMIT_AUTH_CALLS = 10
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60


app = FastAPI(title="DivineAPI")
app.add_middleware(
    RateLimitMiddleware,
    calls=positive_int_env("RATE_LIMIT_CALLS", DEFAULT_RATE_LIMIT_CALLS),
    auth_calls=positive_int_env("RATE_LIMIT_AUTH_CALLS", DEFAULT_RATE_LIMIT_AUTH_CALLS),
    per_seconds=positive_int_env("RATE_LIMIT_WINDOW_SECONDS", DEFAULT_RATE_LIMIT_WINDOW_SECONDS),
)
# Added after the rate limiter so it wraps it - a 429 still carries the id.
app.add_middleware(RequestIdMiddleware)
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
    # Lets browser clients read the server-chosen filename on PDF downloads, the
    # correlation id, and upload back-off/replay signals.
    expose_headers=["Content-Disposition", "X-Request-ID", "Retry-After", "Idempotent-Replayed"],
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
app.include_router(admin_router)
app.include_router(documents_router)
app.include_router(kyc_router)
app.include_router(payment_router)

app.include_router(visit_router)
app.include_router(market_trend_router)
app.include_router(broker_commission_router)
app.include_router(chatbot_router)
app.include_router(zoho_admin_router)
app.include_router(loan_router)
app.include_router(inventory_router)
app.include_router(jobs_router)
app.include_router(booking_router)
app.include_router(admin_booking_kyc_router)
app.include_router(admin_revenue_router)
app.include_router(admin_payments_router)
app.include_router(admin_refunds_router)
app.include_router(admin_help_support_router)

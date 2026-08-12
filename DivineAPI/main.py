import os
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from time import time
from dotenv import load_dotenv

load_dotenv()

from DivineDTO.models import UserCreateDTO, UserLoginDTO, TokenDTO, UserOutDTO
from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence import persistenceCustomer, persistenceBroker
from DivineService import serviceCustomer, serviceBroker
from sqlalchemy.exc import IntegrityError


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, calls: int = 10, per_seconds: int = 60):
        super().__init__(app)
        self.calls = calls
        self.per_seconds = per_seconds
        self.storage = {}

    async def dispatch(self, request: Request, call_next):
        client = request.client.host or "unknown"
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


@app.on_event("startup")
def startup():
    db = PersistenceDB()
    db.create_tables()


# Initialize persistence and services
_cust_persistence = persistenceCustomer()
_broker_persistence = persistenceBroker()
_cust_service = serviceCustomer(_cust_persistence, secret_key=os.getenv("SECRET_KEY"))
_broker_service = serviceBroker(_broker_persistence, secret_key=os.getenv("SECRET_KEY"))


@app.post("/customer/signup", response_model=UserOutDTO)
def customer_signup(request: Request, dto: UserCreateDTO):
    try:
        client_ip = request.client.host if request.client else None
        user = _cust_service.signup(dto, created_by=client_ip)
        return UserOutDTO(
            id=user.id,
            username=user.username,
            email=getattr(user, 'email', None),
            phone=getattr(user, 'phone', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            created_by=user.created_by,
            created_date=user.created_date,
            last_updated_by=user.last_updated_by,
            last_updated_date=user.last_updated_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@app.post("/customer/login", response_model=TokenDTO)
def customer_login(dto: UserLoginDTO):
    try:
        token = _cust_service.login(dto)
        return TokenDTO(access_token=token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@app.post("/broker/signup", response_model=UserOutDTO)
def broker_signup(request: Request, dto: UserCreateDTO):
    try:
        client_ip = request.client.host if request.client else None
        user = _broker_service.signup(dto, created_by=client_ip)
        return UserOutDTO(
            id=user.id,
            username=user.username,
            email=getattr(user, 'email', None),
            phone=getattr(user, 'phone', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            created_by=user.created_by,
            created_date=user.created_date,
            last_updated_by=user.last_updated_by,
            last_updated_date=user.last_updated_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@app.post("/broker/login", response_model=TokenDTO)
def broker_login(dto: UserLoginDTO):
    try:
        token = _broker_service.login(dto)
        return TokenDTO(access_token=token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

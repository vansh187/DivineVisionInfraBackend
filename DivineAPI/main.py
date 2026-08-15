import json
import os
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from time import time
from dotenv import load_dotenv

load_dotenv()

from DivineDTO.models import (
    UserCreateDTO,
    UserLoginDTO,
    TokenDTO,
    UserOutDTO,
    DocumentGenerateRequestDTO,
    DocumentOutDTO,
    KycVerificationOutDTO,
)
from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence import persistenceCustomer, persistenceBroker, persistenceDocument, persistenceKyc
from DivineService import serviceCustomer, serviceBroker, serviceDocument, serviceKyc
from DivineService.auth import get_current_user
from sqlalchemy.exc import IntegrityError


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
    db = PersistenceDB()
    db.create_tables()


# Initialize persistence and services
_cust_persistence = persistenceCustomer()
_broker_persistence = persistenceBroker()
_doc_persistence = persistenceDocument()
_kyc_persistence = persistenceKyc()
_cust_service = serviceCustomer(_cust_persistence, secret_key=os.getenv("JWT_SECRET_KEY"))
_broker_service = serviceBroker(_broker_persistence, secret_key=os.getenv("JWT_SECRET_KEY"))
_doc_service = serviceDocument(_doc_persistence)
_kyc_service = serviceKyc(_kyc_persistence)


@app.get("/health")
def health():
    db_ok = PersistenceDB().test_connection()
    if not db_ok:
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    return {"status": "ok", "database": "connected"}


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


@app.post("/documents/generate", response_model=DocumentOutDTO)
def generate_document(dto: DocumentGenerateRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        doc, signed_url, expires_in = _doc_service.generate(
            dto, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@app.get("/documents/{document_id}", response_model=DocumentOutDTO)
def get_document(document_id: str, current_user: dict = Depends(get_current_user)):
    try:
        doc, signed_url, expires_in = _doc_service.get(document_id, requester_id=current_user["sub"])
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


def _kyc_result_to_dto(record) -> KycVerificationOutDTO:
    extracted_data = record.extracted_data
    if isinstance(extracted_data, str):
        # Postgres (jsonb) hands back a dict already; SQLite (used in tests) hands back
        # the raw JSON text we stored, since this goes through raw SQL, not the ORM.
        try:
            extracted_data = json.loads(extracted_data)
        except ValueError:
            extracted_data = {}
    if not isinstance(extracted_data, dict):
        extracted_data = {}
    return KycVerificationOutDTO(
        id=record.id,
        owner_id=record.owner_id,
        owner_role=record.owner_role,
        method=record.method,
        verified=record.verified,
        masked_aadhaar=record.masked_aadhaar,
        extracted_data=extracted_data,
        failure_reason=record.failure_reason,
        created_date=record.created_date,
    )


@app.post("/kyc/aadhaar/qr/verify", response_model=KycVerificationOutDTO)
async def verify_aadhaar_qr(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        image_bytes = await file.read()
        record = _kyc_service.verify_qr(
            image_bytes, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return _kyc_result_to_dto(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@app.post("/kyc/aadhaar/xml/verify", response_model=KycVerificationOutDTO)
async def verify_aadhaar_offline_xml(
    file: UploadFile = File(...),
    share_code: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    try:
        zip_bytes = await file.read()
        record = _kyc_service.verify_offline_xml(
            zip_bytes, share_code, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return _kyc_result_to_dto(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

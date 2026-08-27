import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    UserCreateDTO, UserLoginDTO, TokenDTO, UserOutDTO, CustomerProfileDTO,
)
from DivineService import serviceCustomer, serviceCustomerProfile
from DivineService.auth import get_current_user

router = APIRouter(prefix="/customer", tags=["customer"])
_cust_service = serviceCustomer(secret_key=os.getenv("JWT_SECRET_KEY"))
_profile_service = serviceCustomerProfile()


@router.post("/signup", response_model=UserOutDTO)
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


@router.post("/login", response_model=TokenDTO)
def customer_login(dto: UserLoginDTO):
    try:
        token = _cust_service.login(dto)
        return TokenDTO(access_token=token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/profile", response_model=CustomerProfileDTO, response_model_exclude_none=True)
def customer_profile(current_user: dict = Depends(get_current_user)):
    # Auth (missing/expired/invalid token -> 401 token_expired / invalid_token) is
    # enforced by the get_current_user dependency before this body runs.
    try:
        return _profile_service.get_profile(current_user.get("sub"), current_user.get("role"))
    except PermissionError:
        raise HTTPException(status_code=403, detail="customer_only")
    except LookupError:
        raise HTTPException(status_code=404, detail="profile_not_found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

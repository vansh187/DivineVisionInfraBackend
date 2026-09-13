import os
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    BrokerCreateDTO, UserLoginDTO, TokenDTO, UserOutDTO, ForgotPasswordDTO, ResetPasswordDTO, MessageDTO,
)
from Divinepersistence import persistenceBroker
from DivineService import serviceBroker, servicePasswordReset, PasswordResetError

router = APIRouter(prefix="/broker", tags=["broker"])
_broker_service = serviceBroker(secret_key=os.getenv("JWT_SECRET_KEY"))
_password_reset_service = servicePasswordReset(role="broker", user_persistence=persistenceBroker())


@router.post("/signup", response_model=UserOutDTO)
def broker_signup(request: Request, dto: BrokerCreateDTO):
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
            project=getattr(user, 'project', None),
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
def broker_login(dto: UserLoginDTO):
    try:
        token = _broker_service.login(dto)
        return TokenDTO(access_token=token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/forgot-password", response_model=MessageDTO)
def broker_forgot_password(dto: ForgotPasswordDTO):
    # Always 200 with the same message on a well-formed request, whether or not the
    # email is registered - see servicePasswordReset.forgot_password's own docstring
    # for why that check can't leak account existence.
    try:
        _password_reset_service.forgot_password(dto.email)
        return MessageDTO(message="If that email is registered, an OTP has been sent.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/reset-password", response_model=MessageDTO)
def broker_reset_password(dto: ResetPasswordDTO):
    try:
        _password_reset_service.reset_password(dto.email, dto.otp, dto.new_password)
        return MessageDTO(message="Password has been reset.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")

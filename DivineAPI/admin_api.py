import os
import time
import logging
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    AdminCreateDTO, AdminLoginDTO, AdminOutDTO, AdminTokenDTO, AdminAccessTokenDTO, AdminRefreshDTO,
    ForgotPasswordDTO, ResetPasswordDTO, MessageDTO,
)
from Divinepersistence import persistenceAdmin
from DivineService import serviceAdmin, servicePasswordReset, PasswordResetError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
_admin_service = serviceAdmin(secret_key=os.getenv("ADMIN_JWT_SECRET_KEY"))
_password_reset_service = servicePasswordReset(role="admin", user_persistence=persistenceAdmin())


@router.post("/signup", response_model=AdminOutDTO)
def admin_signup(request: Request, dto: AdminCreateDTO):
    start = time.monotonic()
    try:
        client_ip = request.client.host if request.client else None
        user = _admin_service.signup(dto, created_by=client_ip)
        return AdminOutDTO(
            id=user.id,
            full_name=user.full_name,
            employee_id=user.employee_id,
            email=user.email,
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
        logger.exception("admin_signup_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_signup_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/login", response_model=AdminTokenDTO)
def admin_login(dto: AdminLoginDTO):
    start = time.monotonic()
    try:
        tokens = _admin_service.login(dto)
        return AdminTokenDTO(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_in=tokens["expires_in"],
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except Exception:
        logger.exception("admin_login_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_login_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/refresh", response_model=AdminAccessTokenDTO)
def admin_refresh(dto: AdminRefreshDTO):
    start = time.monotonic()
    try:
        result = _admin_service.refresh(dto.refresh_token)
        return AdminAccessTokenDTO(access_token=result["access_token"], expires_in=result["expires_in"])
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_refresh_token")
    except Exception:
        logger.exception("admin_refresh_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_refresh_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/forgot-password", response_model=MessageDTO)
def admin_forgot_password(dto: ForgotPasswordDTO):
    # Always 200 with the same message on a well-formed request, whether or not the
    # email is registered - see servicePasswordReset.forgot_password's own docstring
    # for why that check can't leak account existence.
    start = time.monotonic()
    try:
        _password_reset_service.forgot_password(dto.email)
        return MessageDTO(message="If that email is registered, an OTP has been sent.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_forgot_password_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_forgot_password_latency_ms=%.2f", (time.monotonic() - start) * 1000)


@router.post("/reset-password", response_model=MessageDTO)
def admin_reset_password(dto: ResetPasswordDTO):
    start = time.monotonic()
    try:
        _password_reset_service.reset_password(dto.email, dto.otp, dto.new_password)
        return MessageDTO(message="Password has been reset.")
    except PasswordResetError as e:
        raise HTTPException(status_code=e.status_code, detail=e.code)
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_reset_password_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_reset_password_latency_ms=%.2f", (time.monotonic() - start) * 1000)

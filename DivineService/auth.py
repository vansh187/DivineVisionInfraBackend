import os
import jwt
from fastapi import Header, HTTPException
from dotenv import load_dotenv

load_dotenv()


def _get_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY environment variable must be set")
    return secret


def _decode_current_user(authorization: str, allowed_roles, reject_refresh: bool = False) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token_expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid_token")
    except RuntimeError:
        raise HTTPException(status_code=500, detail="server_misconfigured")
    sub = payload.get("sub")
    role = payload.get("role")
    if not sub or role not in allowed_roles:
        raise HTTPException(status_code=401, detail="invalid_token")
    # A refresh token carries the same role/sub as its matching access token (so a
    # leaked refresh token can't be used here to reach a protected route) - only
    # POST /admin/refresh should ever accept one, and it decodes the JWT itself.
    if reject_refresh and payload.get("type") == "refresh":
        raise HTTPException(status_code=401, detail="invalid_token")
    return {"sub": sub, "username": payload.get("username"), "role": role}


def get_current_user(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("customer", "broker"))


def get_current_admin_or_broker(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("admin", "broker"), reject_refresh=True)


def get_current_admin(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("admin",), reject_refresh=True)

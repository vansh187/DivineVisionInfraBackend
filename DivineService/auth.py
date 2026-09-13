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


def _get_admin_secret() -> str:
    # Deliberately separate from JWT_SECRET_KEY - a leaked customer/broker secret
    # can't forge an admin token, and vice versa.
    secret = os.getenv("ADMIN_JWT_SECRET_KEY")
    if not secret:
        raise RuntimeError("ADMIN_JWT_SECRET_KEY environment variable must be set")
    return secret


def _secrets_for_roles(allowed_roles) -> list:
    """A dependency's allowed_roles decides which secret(s) a token could legitimately
    be signed with - admin tokens use ADMIN_JWT_SECRET_KEY, customer/broker use the
    shared JWT_SECRET_KEY. A dependency that allows both role families (e.g. "admin
    or broker") must try both secrets in turn, since the token's role isn't known
    until after it's been decoded."""
    # Each secret is fetched best-effort: a dependency allowing both "admin" and
    # "broker" must still authenticate a broker even in a deployment where
    # ADMIN_JWT_SECRET_KEY hasn't been configured yet (or vice versa) - only raise
    # if NO usable secret could be found at all.
    secrets = []
    if "admin" in allowed_roles:
        try:
            secrets.append(_get_admin_secret())
        except RuntimeError:
            pass
    if set(allowed_roles) - {"admin"}:
        try:
            secrets.append(_get_secret())
        except RuntimeError:
            pass
    if not secrets:
        raise RuntimeError("no JWT signing secret configured for the allowed roles")
    return secrets


def _decode_current_user(authorization: str, allowed_roles, reject_refresh: bool = False) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        candidate_secrets = _secrets_for_roles(allowed_roles)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    payload = None
    expired = False
    for secret in candidate_secrets:
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            break
        except jwt.ExpiredSignatureError:
            # Signature matched this secret - the token is genuinely expired
            # regardless of what any other candidate secret would say.
            expired = True
            break
        except jwt.PyJWTError:
            continue  # wrong secret for this token (or malformed) - try the next one

    if expired:
        raise HTTPException(status_code=401, detail="token_expired")
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid_token")

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

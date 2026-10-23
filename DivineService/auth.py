import os
import threading
import time
from collections import OrderedDict
from typing import Optional
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


_VERIFY_CACHE_MAX = 4096
_verify_cache = OrderedDict()
_verify_cache_lock = threading.Lock()
_BAD_SIGNATURE = object()


def _verify(token: str, secret: str) -> dict:
    """jwt.decode(token, secret, HS256), memoized per (token, secret) so the rate
    limiter and the route's auth dependency share one HMAC verification per
    request. Whether a signature is valid for a given pair never changes, so that
    result (including "bad signature") is cached; the time-based claims are
    re-checked on every call exactly as PyJWT would (exp <= now -> expired,
    nbf/iat in the future -> immature). The secret is part of the key, so a token
    is never accepted under a secret it wasn't verified with."""
    key = (token, secret)
    with _verify_cache_lock:
        payload = _verify_cache.get(key)
        if payload is not None:
            _verify_cache.move_to_end(key)
    if payload is None:
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"],
                                 options={"verify_exp": False, "verify_nbf": False, "verify_iat": False})
        except jwt.PyJWTError:
            payload = _BAD_SIGNATURE
        with _verify_cache_lock:
            _verify_cache[key] = payload
            while len(_verify_cache) > _VERIFY_CACHE_MAX:
                _verify_cache.popitem(last=False)
    if payload is _BAD_SIGNATURE:
        raise jwt.InvalidSignatureError("invalid token")

    now = time.time()
    for claim in ("exp", "nbf", "iat"):
        value = payload.get(claim)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise jwt.DecodeError(f"{claim} must be a number")
    if payload.get("exp") is not None and payload["exp"] <= now:
        raise jwt.ExpiredSignatureError("token expired")
    if payload.get("nbf") is not None and payload["nbf"] > now:
        raise jwt.ImmatureSignatureError("token not yet valid")
    if payload.get("iat") is not None and payload["iat"] > now:
        raise jwt.ImmatureSignatureError("token issued in the future")
    return payload


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
            payload = _verify(token, secret)
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


def rate_limit_identity(authorization: Optional[str]) -> Optional[str]:
    """"<role>:<sub>" for a validly SIGNED, unexpired token of any role, else None.
    Lets the rate limiter key logged-in callers by who they are rather than by
    IP. The signature check matters: an unverified decode would let a caller
    mint a fresh bucket per request just by inventing a new `sub`. Never raises -
    any problem (missing/expired/forged token, unconfigured secret) just means
    "anonymous" and the caller falls back to IP."""
    if not authorization:
        return None
    try:
        user = _decode_current_user(authorization, ("admin", "customer", "broker"))
    except HTTPException:
        return None
    return f"{user['role']}:{user['sub']}"


def get_current_user(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("customer", "broker"))


def get_current_admin_or_broker(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("admin", "broker"), reject_refresh=True)


def get_current_admin(authorization: str = Header(None)) -> dict:
    return _decode_current_user(authorization, ("admin",), reject_refresh=True)


def get_optional_customer(authorization: str = Header(None)) -> Optional[dict]:
    """For a public endpoint that behaves better for a signed-in customer but
    must keep working for an anonymous visitor: a missing header, an expired/
    malformed token, or a token for a different role all just come back as
    None instead of raising - the caller decides what "no identity" means for
    it. Never use this where the endpoint's own security depends on the
    caller actually being a customer; use get_current_user for that.

    A genuine server misconfiguration (no JWT signing secret available at all -
    _decode_current_user's 500 "server_misconfigured") is NOT swallowed the
    same way: treating that as "anonymous caller" would silently stop tagging
    every request with its customer_id and hide the env problem instead of
    surfacing it."""
    if not authorization:
        return None
    try:
        return _decode_current_user(authorization, ("customer",), reject_refresh=True)
    except HTTPException as e:
        if e.status_code == 500:
            raise
        return None

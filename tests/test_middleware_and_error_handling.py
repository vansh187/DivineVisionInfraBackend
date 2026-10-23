import os
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from DivineAPI.main import RateLimitMiddleware, unhandled_exception_handler


def _rate_limited_app(calls=3, per_seconds=60):
    # A throwaway FastAPI app, deliberately separate from DivineAPI.main's `app` -
    # this gets its own RateLimitMiddleware.storage dict, so bursting it here can't
    # eat into the shared 10-req/60s budget the other test files rely on.
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, calls=calls, per_seconds=per_seconds)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/other")
    def other():
        return {"ok": True}

    @app.get("/admin/customers")
    def admin_customers():
        return {"ok": True}

    @app.get("/admin/brokers")
    def admin_brokers():
        return {"ok": True}

    return app


def test_allows_requests_up_to_the_limit():
    client = TestClient(_rate_limited_app(calls=3))
    for _ in range(3):
        assert client.get("/ping").status_code == 200


def test_blocks_requests_over_the_limit():
    client = TestClient(_rate_limited_app(calls=3))
    for _ in range(3):
        client.get("/ping")
    r = client.get("/ping")
    assert r.status_code == 429
    assert r.json() == {"detail": "rate_limited"}


def test_limit_is_tracked_independently_per_path():
    client = TestClient(_rate_limited_app(calls=1))
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    assert client.get("/other").status_code == 200  # different path - separate bucket


def test_admin_customer_and_broker_lists_are_not_rate_limited():
    client = TestClient(_rate_limited_app(calls=1))

    for _ in range(5):
        assert client.get("/admin/customers").status_code == 200
        assert client.get("/admin/brokers").status_code == 200

    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429


def test_limit_resets_after_the_time_window_elapses():
    client = TestClient(_rate_limited_app(calls=2, per_seconds=60))
    with patch("DivineAPI.main.time") as mock_time:
        mock_time.return_value = 1_000_000.0
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 429

        mock_time.return_value = 1_000_000.0 + 61  # past the 60s window
        assert client.get("/ping").status_code == 200


def test_unhandled_exception_returns_generic_500_without_leaking_details():
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    def boom():
        raise ValueError("super secret internal detail")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal_error"}
    assert "super secret internal detail" not in r.text


# ---------- per-user keying ----------

from datetime import datetime, timedelta, timezone
import jwt
import pytest
from DivineService.auth import rate_limit_identity


def _token(sub, role="admin", secret=None, expires_in=3600):
    secret = secret or ("admintestsecret" if role == "admin" else "testsecret")
    payload = {"sub": sub, "role": role, "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in)}
    return {"Authorization": f"Bearer {jwt.encode(payload, secret, algorithm='HS256')}"}


def _post_app(calls=2):
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, calls=calls, per_seconds=60)

    @app.post("/write")
    def write():
        return {"ok": True}

    return TestClient(app)


def test_rate_limit_identity_for_each_role():
    assert rate_limit_identity(_token("DV0002")["Authorization"]) == "admin:DV0002"
    assert rate_limit_identity(_token("C00001", role="customer")["Authorization"]) == "customer:C00001"
    assert rate_limit_identity(_token("B00001", role="broker")["Authorization"]) == "broker:B00001"


@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Basic abc", "Bearer not.a.jwt",
    _token("DV0002", secret="attacker-secret")["Authorization"],   # forged signature
    _token("DV0002", expires_in=-60)["Authorization"],             # expired
    _token("X", role="superuser", secret="admintestsecret")["Authorization"],  # unknown role
])
def test_rate_limit_identity_is_none_for_unverifiable_tokens(authorization):
    assert rate_limit_identity(authorization) is None


def test_rate_limit_identity_never_raises_when_secrets_missing(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("ADMIN_JWT_SECRET_KEY", raising=False)
    assert rate_limit_identity(_token("DV0002")["Authorization"]) is None


def test_same_user_shares_one_bucket_across_ips():
    client = _post_app(calls=2)
    codes = [client.post("/write", headers={**_token("DV0002"), "X-Forwarded-For": f"10.0.0.{i}"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]


def test_different_users_behind_one_ip_get_separate_buckets():
    client = _post_app(calls=1)
    office = {"X-Forwarded-For": "10.9.9.9"}
    assert client.post("/write", headers={**_token("DV0002"), **office}).status_code == 200
    assert client.post("/write", headers={**_token("DV0003"), **office}).status_code == 200
    assert client.post("/write", headers={**_token("DV0002"), **office}).status_code == 429


def test_same_id_in_different_roles_is_a_different_user():
    client = _post_app(calls=1)
    assert client.post("/write", headers=_token("X00001", role="admin")).status_code == 200
    assert client.post("/write", headers=_token("X00001", role="customer")).status_code == 200


def test_forged_tokens_cannot_mint_fresh_buckets():
    """Inventing a new sub per request must not help: an unverifiable token is
    treated as anonymous and lands in the caller's IP bucket."""
    client = _post_app(calls=2)
    codes = [client.post("/write", headers=_token(f"FAKE{i}", secret="attacker-secret")).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]


def test_anonymous_requests_still_limited_by_ip():
    client = _post_app(calls=1)
    assert client.post("/write", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.post("/write", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert client.post("/write", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_user_bucket_and_ip_bucket_are_independent():
    client = _post_app(calls=1)
    ip = {"X-Forwarded-For": "3.3.3.3"}
    assert client.post("/write", headers=ip).status_code == 200                      # anonymous uses ip bucket
    assert client.post("/write", headers={**_token("DV0002"), **ip}).status_code == 200  # user bucket untouched


# ---------- default limit / env configuration ----------

def test_rate_limiter_defaults_to_60_per_minute():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/write")
    def write():
        return {"ok": True}

    client = TestClient(app)
    codes = [client.post("/write", headers={"X-Forwarded-For": "4.4.4.4"}).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60
    assert codes[60] == 429


@pytest.mark.parametrize("raw,expected", [
    (None, 60), ("", 60), ("120", 120), (" 30 ", 30), ("abc", 60), ("0", 60), ("-5", 60), ("1.5", 60),
])
def test_rate_limit_env_overrides_and_bad_values_fall_back(monkeypatch, raw, expected):
    from DivineService.env_config import positive_int_env
    if raw is None:
        monkeypatch.delenv("RATE_LIMIT_CALLS", raising=False)
    else:
        monkeypatch.setenv("RATE_LIMIT_CALLS", raw)
    assert positive_int_env("RATE_LIMIT_CALLS", 60) == expected


# ---------- login/credential paths keep the strict limit ----------

def _auth_app():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)  # production defaults: 60 general, 10 auth

    @app.post("/customer/login")
    def login():
        return {"ok": True}

    @app.post("/customer/visits")
    def other():
        return {"ok": True}

    return TestClient(app)


def test_login_is_limited_to_10_per_minute_while_other_paths_get_60():
    client = _auth_app()
    ip = {"X-Forwarded-For": "6.6.6.6"}
    login = [client.post("/customer/login", headers=ip).status_code for _ in range(11)]
    assert login == [200] * 10 + [429]
    other = [client.post("/customer/visits", headers=ip).status_code for _ in range(61)]
    assert other == [200] * 60 + [429]


def test_every_credential_endpoint_is_an_auth_path():
    expected = {
        "/admin/login", "/customer/login", "/broker/login",
        "/admin/signup", "/customer/signup", "/broker/signup",
        "/admin/forgot-password", "/customer/forgot-password", "/broker/forgot-password",
        "/admin/reset-password", "/customer/reset-password", "/broker/reset-password",
        "/admin/verify-signup", "/admin/resend-signup-otp", "/admin/refresh",
    }
    assert RateLimitMiddleware.AUTH_PATHS == expected


def test_auth_paths_exist_on_the_real_app():
    """Guards against a renamed route silently escaping the strict limit."""
    from DivineAPI.main import app as real_app
    routes = {(getattr(r, "path", None)) for r in real_app.routes}
    assert RateLimitMiddleware.AUTH_PATHS <= routes


def test_auth_limit_env_override(monkeypatch):
    from DivineService.env_config import positive_int_env
    monkeypatch.setenv("RATE_LIMIT_AUTH_CALLS", "5")
    assert positive_int_env("RATE_LIMIT_AUTH_CALLS", 10) == 5


def test_reset_all_clears_live_limiters():
    client = _post_app(calls=1)
    assert client.post("/write", headers={"X-Forwarded-For": "7.7.7.7"}).status_code == 200
    assert client.post("/write", headers={"X-Forwarded-For": "7.7.7.7"}).status_code == 429
    RateLimitMiddleware.reset_all()
    assert client.post("/write", headers={"X-Forwarded-For": "7.7.7.7"}).status_code == 200


# ---------- memoized JWT verification ----------

import time as _time
from unittest.mock import patch as _patch
from DivineService import auth as _auth


def test_token_is_verified_once_across_limiter_and_dependency():
    header = _token("DV0042")["Authorization"]
    token = header.split(" ", 1)[1]
    _auth._verify_cache.clear()
    with _patch.object(_auth.jwt, "decode", wraps=jwt.decode) as spy:
        assert rate_limit_identity(header) == "admin:DV0042"
        assert _auth.get_current_admin(header)["sub"] == "DV0042"
        assert rate_limit_identity(header) == "admin:DV0042"
    assert spy.call_count == 1
    assert (token, "admintestsecret") in _auth._verify_cache


def test_cached_token_still_expires():
    payload = {"sub": "DV0043", "role": "admin", "exp": int(_time.time()) + 2}
    header = f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"
    assert _auth.get_current_admin(header)["sub"] == "DV0043"
    with _patch.object(_auth.time, "time", return_value=payload["exp"]):
        with pytest.raises(Exception) as exc:
            _auth.get_current_admin(header)
    assert exc.value.status_code == 401 and exc.value.detail == "token_expired"


def test_cache_never_accepts_a_token_under_a_secret_it_failed():
    """A customer-secret token claiming role=admin must stay rejected by the
    admin-only dependency even after the limiter has verified it."""
    forged = f"Bearer {jwt.encode({'sub': 'X1', 'role': 'admin', 'exp': int(_time.time()) + 60}, 'testsecret', algorithm='HS256')}"
    rate_limit_identity(forged)  # verified under the customer secret, cached
    with pytest.raises(Exception) as exc:
        _auth.get_current_admin(forged)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("claims,detail", [
    ({"nbf": 10 ** 10}, "invalid_token"),
    ({"iat": 10 ** 10}, "invalid_token"),
    ({"exp": "soon"}, "invalid_token"),
])
def test_time_claims_are_still_enforced(claims, detail):
    payload = {"sub": "DV0044", "role": "admin", "exp": int(_time.time()) + 60, **claims}
    header = f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"
    with pytest.raises(Exception) as exc:
        _auth.get_current_admin(header)
    assert exc.value.status_code == 401 and exc.value.detail == detail


def test_verify_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(_auth, "_VERIFY_CACHE_MAX", 3)
    _auth._verify_cache.clear()
    for i in range(5):
        rate_limit_identity(_token(f"DV10{i}")["Authorization"])
    assert len(_auth._verify_cache) <= 3

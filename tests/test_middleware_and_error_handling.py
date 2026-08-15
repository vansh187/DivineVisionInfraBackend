import os
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

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

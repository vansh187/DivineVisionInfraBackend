"""DivineService.client_ip - best-effort IP for anonymous callers - and how
the rate limiter / chatbot use it."""
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from DivineService.client_ip import client_ip
from DivineAPI.main import RateLimitMiddleware
from DivineAPI.chatbot_api import _client_ip as chatbot_client_ip


def _req(headers=None, peer="10.0.0.9"):
    # Real Starlette Headers - case-insensitive, like a live request's.
    return SimpleNamespace(headers=Headers(headers=headers or {}),
                           client=SimpleNamespace(host=peer) if peer else None)


def test_uses_first_forwarded_for_entry():
    assert client_ip(_req({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})) == "1.2.3.4"


def test_ignores_cf_connecting_ip():
    assert client_ip(_req({"CF-Connecting-IP": "203.0.113.5"})) == "10.0.0.9"
    assert client_ip(_req({"CF-Connecting-IP": "203.0.113.5", "X-Forwarded-For": "1.2.3.4"})) == "1.2.3.4"


def test_falls_back_to_peer_for_missing_or_garbage_forwarded_for():
    assert client_ip(_req()) == "10.0.0.9"
    assert client_ip(_req({"X-Forwarded-For": "garbage, 1.2.3.4"})) == "10.0.0.9"
    assert client_ip(_req({"X-Forwarded-For": ""})) == "10.0.0.9"


def test_unknown_only_when_nothing_is_available():
    assert client_ip(_req(peer=None)) == "unknown"
    assert client_ip(_req(peer="not-an-ip")) == "unknown"


def test_ipv6_is_normalised():
    assert client_ip(_req({"X-Forwarded-For": "2001:DB8::1"})) == "2001:db8::1"


def test_chatbot_maps_unknown_to_none():
    assert chatbot_client_ip(_req(peer=None)) is None
    assert chatbot_client_ip(_req({"X-Forwarded-For": "1.2.3.4"})) == "1.2.3.4"


def test_distinct_anonymous_callers_get_distinct_buckets():
    """No shared 'unknown' bucket: one noisy anonymous caller can't lock
    everyone else out."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, calls=1, per_seconds=60)

    @app.post("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/ping", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.post("/ping", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert client.post("/ping", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200

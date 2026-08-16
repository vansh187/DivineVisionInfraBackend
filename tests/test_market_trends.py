import os
from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineAPI.market_trend_api import _market_trend_service
from DivineAPI.main import app

client = TestClient(app)


class _FakeMarketTrendService:
    def list_market_trends(self, city=None, locality=None, property_type=None, limit=20):
        return {
            "count": 1,
            "trends": [{
                "id": "trend1",
                "city": city or "Gurugram",
                "locality": locality,
                "property_type": property_type or "plot",
                "period_label": "Q3 2026",
                "as_of_date": date(2026, 9, 30).isoformat(),
                "price_per_sqyd": 100000.0,
                "previous_price_per_sqyd": None,
                "price_change_percent": None,
                "trend_direction": "new",
                "rental_yield_percent": None,
                "demand_score": None,
                "supply_score": None,
                "demand_label": "unknown",
                "sample_size": 0,
            }],
        }


def setup_module(module):
    _market_trend_service._persistence = SimpleNamespace()


def test_market_trends_endpoint_is_public_and_returns_trends(monkeypatch):
    monkeypatch.setattr("DivineAPI.market_trend_api._market_trend_service", _FakeMarketTrendService())

    r = client.get("/market-trends?city=Gurugram&property_type=plot")

    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert r.json()["trends"][0]["city"] == "Gurugram"


def test_market_trends_rejects_invalid_limit():
    r = client.get("/market-trends?limit=0")

    assert r.status_code == 422

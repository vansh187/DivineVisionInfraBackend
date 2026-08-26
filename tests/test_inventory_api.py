import os
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineAPI.inventory_api import _inventory_service
from DivineAPI.main import app

client = TestClient(app)


class _FakeInventoryService:
    def search(self, **kwargs):
        return {"count": 1, "units": [_unit(city=kwargs.get("city") or "Sonipat")]}

    def search_natural_language(self, query, session_id=None, limit=20):
        if not query.strip():
            raise ValueError("query_required")
        return {"count": 1, "units": [_unit()], "parsed_filters": {"city": "Sonipat"}}

    def record_view(self, inventory_id, lead_id=None, session_id=None):
        if inventory_id == "missing":
            raise ValueError("inventory_id_required")
        return {"recorded": True}

    def recommend(self, lead_id=None, session_id=None, limit=10):
        return {"best_fit": [_unit()], "similar_alternatives": {}}


def _unit(**overrides):
    values = {
        "id": "u1", "project_name": "Suraksha Enclave", "city": "Sonipat", "locality": "Sector-15",
        "block": "C", "unit_number": "C1", "unit_type": "plot", "width_mtr": 7.588, "length_mtr": 13.404,
        "area_sqmt": 101.71, "area_sqyd": 121.65, "status": "available", "estimated_price": 12165000.0,
    }
    values.update(overrides)
    return values


def setup_module(module):
    _inventory_service._persistence = SimpleNamespace()


def test_search_inventory_endpoint_returns_units(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.get("/inventory/search?city=Sonipat")

    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert r.json()["units"][0]["city"] == "Sonipat"


def test_search_inventory_rejects_invalid_limit():
    r = client.get("/inventory/search?limit=0")

    assert r.status_code == 422


def test_search_nl_endpoint_returns_parsed_filters(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/search/nl", json={"query": "150 sq yd plot in Sonipat"})

    assert r.status_code == 200, r.text
    assert r.json()["parsed_filters"] == {"city": "Sonipat"}


def test_search_nl_endpoint_rejects_empty_query(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/search/nl", json={"query": " "})

    assert r.status_code == 400


def test_record_view_endpoint(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/u1/view", json={"lead_id": "lead1"})

    assert r.status_code == 200, r.text
    assert r.json() == {"recorded": True}


def test_record_view_endpoint_maps_value_error_to_400(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/missing/view", json={})

    assert r.status_code == 400


def test_recommendations_endpoint(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.get("/inventory/recommendations?lead_id=lead1")

    assert r.status_code == 200, r.text
    assert r.json()["best_fit"][0]["id"] == "u1"

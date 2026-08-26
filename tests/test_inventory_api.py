import os
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.inventory_api import _inventory_service
from DivineAPI.main import app

client = TestClient(app)

_BROKER_TOKEN = None
_OTHER_BROKER_TOKEN = None
_CUSTOMER_TOKEN = None


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

    def reserve_unit(self, inventory_id, broker_id):
        if inventory_id == "taken":
            raise ValueError("unit_not_available")
        return _unit(id=inventory_id, status="reserved", reserved_at="2026-01-01T00:00:00+00:00",
                     reserved_until="2026-01-04T00:00:00+00:00")

    def release_reservation(self, inventory_id, broker_id):
        if inventory_id == "not-mine":
            raise ValueError("not_reserved_by_you")
        return _unit(id=inventory_id, status="available")

    def mark_sold(self, inventory_id, broker_id):
        if inventory_id == "not-mine":
            raise ValueError("not_reserved_by_you")
        return _unit(id=inventory_id, status="sold")

    def list_my_reservations(self, broker_id):
        return {"count": 1, "reservations": [_unit(id="mine1", status="reserved",
                 reserved_at="2026-01-01T00:00:00+00:00", reserved_until="2026-01-04T00:00:00+00:00")]}


def _unit(**overrides):
    values = {
        "id": "u1", "project_name": "Suraksha Enclave", "city": "Sonipat", "locality": "Sector-15",
        "block": "C", "unit_number": "C1", "unit_type": "plot", "width_mtr": 7.588, "length_mtr": 13.404,
        "area_sqmt": 101.71, "area_sqyd": 121.65, "status": "available", "estimated_price": 12165000.0,
    }
    values.update(overrides)
    return values


def setup_module(module):
    global _BROKER_TOKEN, _OTHER_BROKER_TOKEN, _CUSTOMER_TOKEN
    _inventory_service._persistence = SimpleNamespace()

    # Wipe and rebuild from the current models (mirrors tests/test_visits.py) rather than
    # relying on create_tables() alone - create_all() only creates missing tables, it never
    # alters an existing one, so a test_db.sqlite left over from before the reservation columns
    # were added would otherwise still be missing them and every reservation query would 500.
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    client.post("/broker/signup", json={"username": "invbroker_shared", "password": "strongpassword"})
    lr = client.post("/broker/login", json={"username": "invbroker_shared", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _BROKER_TOKEN = lr.json()["access_token"]

    client.post("/broker/signup", json={"username": "invbroker_other", "password": "strongpassword"})
    lr2 = client.post("/broker/login", json={"username": "invbroker_other", "password": "strongpassword"})
    assert lr2.status_code == 200, lr2.text
    _OTHER_BROKER_TOKEN = lr2.json()["access_token"]

    client.post("/customer/signup", json={"username": "invcust_shared", "password": "strongpassword"})
    lr3 = client.post("/customer/login", json={"username": "invcust_shared", "password": "strongpassword"})
    assert lr3.status_code == 200, lr3.text
    _CUSTOMER_TOKEN = lr3.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


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


def test_reserve_endpoint_requires_broker_auth(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/u1/reserve")
    assert r.status_code == 401

    r2 = client.post("/inventory/u1/reserve", headers=_auth_headers(_CUSTOMER_TOKEN))
    assert r2.status_code == 403


def test_reserve_endpoint_success(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/u1/reserve", headers=_auth_headers(_BROKER_TOKEN))

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reserved"
    assert r.json()["reserved_until"] is not None


def test_reserve_endpoint_maps_conflict_to_409(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/taken/reserve", headers=_auth_headers(_BROKER_TOKEN))

    assert r.status_code == 409


def test_release_endpoint_success(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/u1/release", headers=_auth_headers(_BROKER_TOKEN))

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "available"


def test_release_endpoint_maps_other_brokers_reservation_to_409(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/not-mine/release", headers=_auth_headers(_OTHER_BROKER_TOKEN))

    assert r.status_code == 409


def test_mark_sold_endpoint_success(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/u1/mark-sold", headers=_auth_headers(_BROKER_TOKEN))

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sold"


def test_mark_sold_endpoint_maps_other_brokers_reservation_to_409(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.post("/inventory/not-mine/mark-sold", headers=_auth_headers(_OTHER_BROKER_TOKEN))

    assert r.status_code == 409


def test_reserved_mine_endpoint_requires_broker_auth(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.get("/inventory/reserved/mine", headers=_auth_headers(_CUSTOMER_TOKEN))

    assert r.status_code == 403


def test_reserved_mine_endpoint_returns_only_callers_reservations(monkeypatch):
    monkeypatch.setattr("DivineAPI.inventory_api._inventory_service", _FakeInventoryService())

    r = client.get("/inventory/reserved/mine", headers=_auth_headers(_BROKER_TOKEN))

    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert r.json()["reservations"][0]["id"] == "mine1"

"""Admin Inventory section over real HTTP + the plot status history log."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_admin import persistenceAdmin
from Divinepersistence.persistence_broker import persistenceBroker
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_inventory import persistenceInventory
from DivineAPI.main import app

client = TestClient(app)
inventory = persistenceInventory()

PROJECT = "Admin Inventory Greens"
OTHER_PROJECT = "Admin Inventory Enclave"
UNITS = {}
ADMIN_ID = None
BROKER_ID = None
CUSTOMER_ID = None


def _unit(project, number, status="available", block="A"):
    row = inventory.upsert_unit(
        id=str(uuid.uuid4()), project_name=project, city="Karnal", unit_number=number, block=block,
        width_mtr=10, length_mtr=20, area_sqmt=200, area_sqyd=239.2, status=status,
    )
    return row.id


def setup_module(module):
    global ADMIN_ID, BROKER_ID, CUSTOMER_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    ADMIN_ID = persistenceAdmin().create_user(
        full_name="Ritika Sharma", employee_id=f"EMP{uuid.uuid4().hex[:6]}",
        email=f"inv.admin.{uuid.uuid4().hex[:6]}@example.com", password_hash="x",
    ).id
    BROKER_ID = persistenceBroker().create_user(
        username=f"inv_broker_{uuid.uuid4().hex[:6]}", password_hash="x", first_name="Aman", last_name="Gupta",
        phone="9000000501", email=f"inv.broker.{uuid.uuid4().hex[:6]}@example.com", project="ops-divine-greens",
    ).id
    CUSTOMER_ID = persistenceCustomer().create_user(
        username=f"inv_customer_{uuid.uuid4().hex[:6]}", password_hash="x", first_name="Neha", last_name="Verma",
        phone="9000000502", email=f"inv.customer.{uuid.uuid4().hex[:6]}@example.com",
    ).id

    UNITS["a9"] = _unit(PROJECT, "A-9")
    UNITS["a10"] = _unit(PROJECT, "A-10")
    UNITS["a2"] = _unit(PROJECT, "A-2", status="held")
    UNITS["b1"] = _unit(PROJECT, "B-1", status="sold", block="B")
    UNITS["reserve"] = _unit(PROJECT, "B-2", block="B")
    UNITS["booking"] = _unit(PROJECT, "B-3", block="B")
    UNITS["other"] = _unit(OTHER_PROJECT, "C-1", block="C")


def _token(role="admin", sub=None, username="inventory_admin@example.com", secret="admintestsecret"):
    payload = {"sub": sub or ADMIN_ID, "username": username, "role": role,
               "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return {"Authorization": f"Bearer {jwt.encode(payload, secret, algorithm='HS256')}"}


def _log_rows(inventory_id):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT * FROM divine_inventory_status_log WHERE inventory_id = :i ORDER BY changed_at, id"
        ), {"i": inventory_id}).mappings().all()


def _change(inventory_id, status, expected, note=None, headers=None):
    return client.post(f"/admin/inventory/{inventory_id}/status", headers=headers or _token(),
                       json={"status": status, "expected_status": expected, "note": note})


def test_admin_only():
    assert client.get("/admin/inventory/projects").status_code == 401
    broker = _token(role="broker", sub="B00001", secret="testsecret")
    assert client.get("/admin/inventory", headers=broker).status_code in (401, 403)
    assert _change(UNITS["a9"], "sold", "available", headers=broker).status_code in (401, 403)


def test_projects_summary_counts_each_status():
    res = client.get("/admin/inventory/projects", headers=_token())
    assert res.status_code == 200
    by_name = {p["project_name"]: p for p in res.json()["items"]}
    p = by_name[PROJECT]
    assert (p["total"], p["available"], p["held"], p["sold"]) == (6, 4, 1, 1)
    assert by_name[OTHER_PROJECT]["total"] == 1


def test_list_filters_and_natural_plot_order():
    res = client.get("/admin/inventory", params={"project_name": PROJECT, "page_size": 100}, headers=_token())
    assert res.status_code == 200
    body = res.json()
    assert body["pagination"]["total_items"] == 6
    numbers = [u["unit_number"] for u in body["items"]]
    assert numbers[:3] == ["A-2", "A-9", "A-10"]
    assert all(u["project_name"] == PROJECT for u in body["items"])
    first = body["items"][0]
    assert first["width_mtr"] == 10 and first["area_sqyd"] == 239.2

    held = client.get("/admin/inventory", params={"project_name": PROJECT, "status": "held"}, headers=_token())
    assert [u["unit_number"] for u in held.json()["items"]] == ["A-2"]

    found = client.get("/admin/inventory", params={"project_name": PROJECT, "search": "a-1"}, headers=_token())
    assert [u["unit_number"] for u in found.json()["items"]] == ["A-10"]
    # '_' and '%' are matched literally, not as LIKE wildcards.
    for literal in ("a_1", "%", "a\\1"):
        res = client.get("/admin/inventory", params={"project_name": PROJECT, "search": literal}, headers=_token())
        assert res.status_code == 200 and res.json()["items"] == [], literal
        assert res.json()["pagination"]["total_items"] == 0

    assert client.get("/admin/inventory", params={"status": "nope"}, headers=_token()).status_code == 422


def test_status_change_records_previous_and_new_status_with_admin_name_and_time():
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    res = _change(UNITS["a9"], "sold", "available", note="Sold at the site office")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "sold"
    assert body["sale_source"] == "offline"
    assert body["last_changed_by_name"] == "Ritika Sharma"
    # Newest first; the plot's first row is its creation by the inventory ingest.
    assert [(h["old_status"], h["new_status"]) for h in body["history"]] == [("available", "sold"), (None, "available")]
    assert (body["history"][-1]["source"], body["history"][-1]["changed_by_role"]) == ("inventory_ingest", "system")
    entry = body["history"][0]
    assert (entry["old_status"], entry["new_status"]) == ("available", "sold")
    assert entry["changed_by_name"] == "Ritika Sharma"
    assert entry["changed_by_role"] == "admin"
    assert entry["source"] == "admin_panel"
    assert entry["note"] == "Sold at the site office"
    assert datetime.fromisoformat(entry["changed_at"].replace("Z", "+00:00")) >= before

    back = _change(UNITS["a9"], "available", "sold")
    assert back.status_code == 200
    history = back.json()["history"]
    assert [(h["old_status"], h["new_status"]) for h in history] == [
        ("sold", "available"), ("available", "sold"), (None, "available")]
    assert back.json()["sale_source"] is None

    rows = _log_rows(UNITS["a9"])
    assert len(rows) == 3 and rows[0]["changed_by_id"] is None and rows[1]["changed_by_id"] == ADMIN_ID

    listed = client.get("/admin/inventory", params={"project_name": PROJECT, "search": "A-9"}, headers=_token())
    item = listed.json()["items"][0]
    assert item["last_changed_by_name"] == "Ritika Sharma" and item["last_changed_at"]


def test_admin_without_profile_row_is_named_by_token_username():
    res = _change(UNITS["a10"], "held", "available", headers=_token(sub="ZZ9999", username="ops.lead@example.com"))
    assert res.status_code == 200
    assert res.json()["history"][0]["changed_by_name"] == "ops.lead@example.com"


def test_stale_expected_status_is_refused_and_nothing_is_written():
    count = len(_log_rows(UNITS["a2"]))
    res = _change(UNITS["a2"], "sold", "available")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "status_changed" and detail["current_status"] == "held"
    assert len(_log_rows(UNITS["a2"])) == count
    assert client.get(f"/admin/inventory/{UNITS['a2']}", headers=_token()).json()["status"] == "held"


def test_same_status_and_flow_only_statuses_are_refused():
    assert _change(UNITS["b1"], "sold", "sold").json()["detail"]["code"] == "status_unchanged"
    assert _change(UNITS["b1"], "booked", "sold").status_code == 422
    assert client.post(f"/admin/inventory/{UNITS['b1']}/status", headers=_token(),
                       json={"status": "held", "expected_status": "sold", "note": "x" * 256}).status_code == 422


def test_unknown_plot_is_404():
    assert client.get("/admin/inventory/does-not-exist", headers=_token()).status_code == 404
    assert _change("does-not-exist", "sold", "available").status_code == 404


def test_partner_reservation_is_logged_with_broker_name_and_locks_manual_changes():
    now = datetime.now(timezone.utc)
    assert inventory.reserve_unit(id=UNITS["reserve"], broker_id=BROKER_ID, reservation_id=str(uuid.uuid4()),
                                  reserved_at=now, reserved_until=now + timedelta(days=3))
    detail = client.get(f"/admin/inventory/{UNITS['reserve']}", headers=_token()).json()
    assert detail["status"] == "reserved"
    assert detail["status_lock"] == "partner_reservation" and detail["allowed_statuses"] == []
    entry = detail["history"][0]
    assert (entry["old_status"], entry["new_status"]) == ("available", "reserved")
    assert entry["changed_by_name"] == "Aman Gupta" and entry["source"] == "partner_reservation"

    res = _change(UNITS["reserve"], "sold", "reserved")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "partner_reservation"


def test_expired_reservation_is_logged_as_system():
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_project_inventory SET reserved_until = :t WHERE id = :i"),
                     {"t": datetime.now(timezone.utc) - timedelta(minutes=1), "i": UNITS["reserve"]})
    detail = client.get(f"/admin/inventory/{UNITS['reserve']}", headers=_token()).json()
    assert detail["status"] == "available"
    entry = detail["history"][0]
    assert (entry["old_status"], entry["new_status"]) == ("reserved", "available")
    assert entry["changed_by_role"] == "system" and entry["source"] == "reservation_expiry"


def test_booking_flow_changes_are_logged_and_lock_the_plot():
    payment_id = str(uuid.uuid4())
    assert inventory.hold_for_kyc_review(id=UNITS["booking"], payment_id=payment_id, customer_id=CUSTOMER_ID)
    # Re-running the same hold is a no-op and must not add a history row.
    assert inventory.hold_for_kyc_review(id=UNITS["booking"], payment_id=payment_id, customer_id=CUSTOMER_ID)
    assert inventory.confirm_booking_after_kyc(
        id=UNITS["booking"], payment_id=payment_id,
        actor={"role": "admin", "id": ADMIN_ID, "name": None}, source="kyc_approval")

    detail = client.get(f"/admin/inventory/{UNITS['booking']}", headers=_token()).json()
    assert detail["status"] == "booked" and detail["status_lock"] == "booking"
    steps = [(h["old_status"], h["new_status"], h["changed_by_name"], h["source"]) for h in detail["history"]]
    assert steps == [
        ("pending_kyc_review", "booked", "Ritika Sharma", "kyc_approval"),
        ("available", "pending_kyc_review", "Neha Verma", "booking_payment"),
        (None, "available", "System", "inventory_ingest"),
    ]
    res = _change(UNITS["booking"], "available", "booked")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "booking"

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB, SessionLocal
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata
from DivineAPI.main import app, RateLimitMiddleware
from tests.admin_signup_helpers import mint_admin_access_token, signup_and_verify_admin

client = TestClient(app)
_ADMIN_TOKEN = None
_ADMIN_ID = None


def setup_module(module):
    global _ADMIN_TOKEN, _ADMIN_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()
    admin_email = "imports_admin@divinevisioninfra.com"
    signup = signup_and_verify_admin(client, {
        "full_name": "Imports Admin", "employee_id": "DV6161",
        "email": admin_email, "password": "strongpassword",
    })
    _ADMIN_ID = signup.json()["id"]
    _ADMIN_TOKEN = mint_admin_access_token(_ADMIN_ID, admin_email)


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    RateLimitMiddleware.reset_all()
    yield


def _post(entity, rows, key=None, token=None):
    headers = {"Authorization": f"Bearer {token or _ADMIN_TOKEN}",
               "Idempotency-Key": key or str(uuid.uuid4())}
    return client.post(f"/admin/imports/{entity}", json={"rows": rows}, headers=headers)


def _count(sql, **params):
    with SessionLocal() as db:
        return db.execute(text(sql), params).scalar()


def _tag():
    return uuid.uuid4().hex[:8]


# ---- auth / envelope --------------------------------------------------------
def test_import_requires_admin_token():
    rows = [{"row": 2, "full_name": "No Auth", "email": f"noauth-{_tag()}@example.com", "phone": "1"}]
    r = client.post("/admin/imports/customers", json={"rows": rows},
                    headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 401, r.text

    customer_token = jwt.encode({
        "sub": "C00001", "username": "someone@example.com", "role": "customer", "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }, "testsecret", algorithm="HS256")
    r = _post("customers", rows, token=customer_token)
    assert r.status_code in (401, 403), r.text


def test_unknown_entity_is_404_and_key_is_required():
    assert _post("refunds", [{"row": 2}]).status_code == 404
    r = client.post("/admin/imports/customers", json={"rows": [{"row": 2}]},
                    headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"})
    assert r.status_code == 400 and r.json()["detail"] == "idempotency_key_required"
    r = _post("customers", [{"row": 2}], key="not-a-uuid")
    assert r.status_code == 400 and r.json()["detail"] == "invalid_idempotency_key"


def test_more_than_5000_rows_is_413():
    rows = [{"row": i + 2, "full_name": "Big File", "email": f"big{i}@example.com", "phone": "1"}
            for i in range(5001)]
    r = _post("customers", rows)
    assert r.status_code == 413, r.text
    assert _count("SELECT count(*) FROM divine_customer_users WHERE email = 'big0@example.com'") == 0


# ---- customers --------------------------------------------------------------
def test_valid_customer_file_creates_every_row_and_lists_them():
    tag = _tag()
    rows = [{"row": i + 2, "full_name": f"Import Person{i} {tag}", "email": f"Imp{i}-{tag}@Example.com",
             "phone": f"98765{i:05d}"} for i in range(3)]
    r = _post("customers", rows)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 3, "updated": 0}

    listed = client.get(f"/admin/customers?search={tag}", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"})
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert {i["email"] for i in items} == {f"imp{i}-{tag}@example.com" for i in range(3)}
    assert all(i["source"] == "WEBSITE" and i["status"] == "ACTIVE" for i in items)

    # A real account (username = email, a password hash nobody knows).
    from Divinepersistence import persistenceCustomer
    stored = persistenceCustomer().get_by_email(f"imp0-{tag}@example.com")
    assert stored.username == f"imp0-{tag}@example.com"
    assert stored.password_hash.startswith("$2")
    assert stored.first_name == "Import" and stored.last_name == f"Person0 {tag}"


def test_one_bad_row_refuses_the_whole_file_and_writes_nothing():
    tag = _tag()
    before = _count("SELECT count(*) FROM divine_customer_users")
    rows = [{"row": i + 2, "full_name": f"Batch {tag}", "email": f"batch{i}-{tag}@example.com", "phone": "12345"}
            for i in range(10)]
    rows[4] = {"row": 6, "full_name": "X", "email": "not-an-email"}          # 3 problems, one row
    rows[7] = {"row": 9, "id": "C99999", "phone": "1"}                        # unknown id
    r = _post("customers", rows)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "import_invalid"
    assert detail["message"] == "2 rows have problems. Nothing was imported."
    by_row = {}
    for e in detail["errors"]:
        by_row.setdefault(e["row"], set()).add(e.get("field"))
    assert by_row == {6: {"full_name", "email", "phone"}, 9: {"id"}}
    assert _count("SELECT count(*) FROM divine_customer_users") == before


def test_update_by_id_changes_only_the_fields_sent():
    tag = _tag()
    assert _post("customers", [{"row": 2, "full_name": "Asha Verma", "email": f"asha-{tag}@example.com",
                                "phone": "9000000001"}]).status_code == 200
    from Divinepersistence import persistenceCustomer
    created = persistenceCustomer().get_by_email(f"asha-{tag}@example.com")

    r = _post("customers", [{"row": 2, "id": created.id, "phone": "9111111111"}])
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 0, "updated": 1}
    after = persistenceCustomer().get_by_id(created.id)
    assert after.phone == "9111111111"
    assert (after.first_name, after.last_name, after.email) == ("Asha", "Verma", f"asha-{tag}@example.com")

    # An email change moves the login username with it (it was the email).
    r = _post("customers", [{"row": 2, "id": created.id, "email": f"asha.new-{tag}@example.com"}])
    assert r.status_code == 200, r.text
    after = persistenceCustomer().get_by_id(created.id)
    assert after.email == after.username == f"asha.new-{tag}@example.com"
    assert after.phone == "9111111111"


def test_duplicate_emails_within_file_and_against_database_are_refused():
    tag = _tag()
    assert _post("customers", [{"row": 2, "full_name": "Existing One", "email": f"taken-{tag}@example.com",
                                "phone": "1"}]).status_code == 200
    rows = [
        {"row": 2, "full_name": "New A", "email": f"twice-{tag}@example.com", "phone": "1"},
        {"row": 3, "full_name": "New B", "email": f"TWICE-{tag}@example.com", "phone": "1"},
        {"row": 4, "full_name": "New C", "email": f"Taken-{tag}@example.com", "phone": "1"},
    ]
    r = _post("customers", rows)
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]["errors"]
    assert {(e["row"], e["field"]) for e in errors} == {(3, "email"), (4, "email")}
    assert any("row 2" in e["message"] for e in errors if e["row"] == 3)
    assert any(e["message"] == "A customer with this email already exists." for e in errors)
    assert _count("SELECT count(*) FROM divine_customer_users WHERE lower(email) = :e",
                  e=f"twice-{tag}@example.com") == 0


def test_same_key_and_body_replays_and_different_body_is_409():
    tag = _tag()
    key = str(uuid.uuid4())
    rows = [{"row": 2, "full_name": "Replay Person", "email": f"replay-{tag}@example.com", "phone": "1"}]
    first = _post("customers", rows, key=key)
    second = _post("customers", rows, key=key)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"created": 1, "updated": 0}
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert _count("SELECT count(*) FROM divine_customer_users WHERE email = :e",
                  e=f"replay-{tag}@example.com") == 1

    changed = [{**rows[0], "phone": "2"}]
    r = _post("customers", changed, key=key)
    assert r.status_code == 409 and r.json()["detail"] == "idempotency_conflict"


def test_failed_import_does_not_store_its_key():
    key = str(uuid.uuid4())
    assert _post("customers", [{"row": 2, "full_name": "Bad"}], key=key).status_code == 422
    tag = _tag()
    ok = _post("customers", [{"row": 2, "full_name": "Fixed File", "email": f"fixed-{tag}@example.com",
                              "phone": "1"}], key=key)
    assert ok.status_code == 200, ok.text


def test_unknown_columns_are_refused():
    r = _post("customers", [{"row": 2, "full_name": "Col Test", "email": f"col-{_tag()}@example.com",
                             "phone": "1", "status": "BOOKED"}])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == [
        {"row": 2, "field": "status", "message": "This column can't be imported."}]


# ---- brokers ----------------------------------------------------------------
def test_brokers_create_update_and_validation():
    tag = _tag()
    r = _post("brokers", [{"row": 2, "full_name": "Ravi Partner", "email": f"ravi-{tag}@example.com",
                           "phone": "9800000000", "project": "suraksha-enclave"}])
    assert r.status_code == 200, r.text
    listed = client.get(f"/admin/brokers?search=ravi-{tag}", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"})
    item = listed.json()["items"][0]
    assert item["project"] == "suraksha-enclave" and item["full_name"] == "Ravi Partner"

    r = _post("brokers", [{"row": 2, "id": item["id"], "project": "ops-divine-greens"}])
    assert r.status_code == 200 and r.json() == {"created": 0, "updated": 1}
    from Divinepersistence import persistenceBroker
    stored = persistenceBroker().get_by_id(item["id"])
    assert stored.project == "ops-divine-greens" and stored.phone == "9800000000"

    r = _post("brokers", [
        {"row": 2, "full_name": "No Project", "email": f"np-{tag}@example.com", "phone": "1"},
        {"row": 3, "full_name": "Dup", "email": f"RAVI-{tag}@example.com", "phone": "1", "project": "nowhere"},
    ])
    assert r.status_code == 422
    assert {(e["row"], e["field"]) for e in r.json()["detail"]["errors"]} == {
        (2, "project"), (3, "email"), (3, "project")}


# ---- visits -----------------------------------------------------------------
def test_visits_create_update_and_rules():
    tag = _tag()
    r = _post("visits", [
        {"row": 2, "origin_type": "CUSTOMER", "customer_name": f"Visitor {tag}", "customer_contact": "98111",
         "project_name": "suraksha-enclave", "visit_date": "2026-12-01", "visit_time": "10:30"},
        {"row": 3, "origin_type": "CHANNEL_PARTNER", "customer_name": f"Partner Lead {tag}",
         "visit_date": "2026-12-02", "status": "confirmed", "assigned_to": "Neha"},
    ])
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2, "updated": 0}

    listed = client.get(f"/admin/visits?search={tag}", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"})
    items = {i["origin_type"]: i for i in listed.json()["items"]}
    assert items["CUSTOMER"]["status"] == "scheduled" and items["CUSTOMER"]["source"] == "Website"
    assert items["CUSTOMER"]["visit_date"] == "2026-12-01" and items["CUSTOMER"]["visit_time"] == "10:30"
    assert items["CHANNEL_PARTNER"]["status"] == "confirmed"

    visit_id = items["CUSTOMER"]["id"]
    r = _post("visits", [{"row": 2, "id": visit_id, "status": "completed", "notes": "Liked plot 12"}])
    assert r.status_code == 200, r.text
    detail = client.get(f"/admin/visits/{visit_id}", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"}).json()
    assert detail["status"] == "completed" and detail["notes"] == "Liked plot 12"
    assert detail["customer_name"] == f"Visitor {tag}" and detail["visit_time"] == "10:30"

    r = _post("visits", [
        {"row": 2, "id": visit_id, "origin_type": "CHANNEL_PARTNER"},
        {"row": 3, "origin_type": "CUSTOMER", "customer_name": "No Date"},
        {"row": 4, "origin_type": "CUSTOMER", "customer_name": "Bad Values", "visit_date": "2026-02-30",
         "visit_time": "25:00", "status": "done", "preferred_window": "next month"},
    ])
    assert r.status_code == 422
    assert {(e["row"], e["field"]) for e in r.json()["detail"]["errors"]} == {
        (2, "origin_type"), (3, "visit_date"),
        (4, "visit_date"), (4, "visit_time"), (4, "status"), (4, "preferred_window")}


# ---- payment plans ----------------------------------------------------------
def _plan(code, short, row=2):
    return {"row": row, "code": code, "short_code": short, "name": "Construction Linked Plan",
            "description": None, "project_id": "suraksha-enclave", "sort_order": 4,
            "instalments": [
                {"label": "Booking amount", "percent": 10, "due_trigger": "booking", "due_days": 0,
                 "charge_head": None},
                {"label": "Within 45 days", "percent": 40, "due_trigger": "booking", "due_days": 45,
                 "charge_head": "PLC"},
                {"label": "On possession", "percent": 50, "due_trigger": "possession", "due_days": 0,
                 "charge_head": "IFMS+CAM"},
            ]}


def test_payment_plans_import_creates_only_new_valid_plans():
    tag = _tag().upper()
    code_a, code_b = f"IMP{tag}A/CLP/10-40-50", f"IMP{tag}B/CLP/10-40-50"
    r = _post("payment-plans", [_plan(code_a, f"A{tag}"), _plan(code_b, f"B{tag}", row=6)])
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2, "updated": 0}
    listed = client.get("/admin/payment-plans", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"}).json()
    codes = {p["code"]: p for p in listed["plans"]}
    assert codes[code_a]["is_active"] and codes[code_a]["version"] == 1

    before = _count("SELECT count(*) FROM divine_payment_plans")
    bad_percent = _plan(f"IMP{tag}D/X", f"D{tag}", row=10)
    bad_percent["instalments"][2]["percent"] = 40                    # adds up to 90
    r = _post("payment-plans", [
        _plan(code_a, f"C{tag}", row=2),                             # code exists
        _plan(f"IMP{tag}C/X", f"b{tag}", row=6),                     # active short code (any case)
        bad_percent,
        _plan(f"IMP{tag}E/X", f"E{tag}", row=14),
        _plan(f"IMP{tag}E/X", f"F{tag}", row=18),                    # code repeated in the file
    ])
    assert r.status_code == 422, r.text
    assert {(e["row"], e.get("field")) for e in r.json()["detail"]["errors"]} == {
        (2, "code"), (6, "short_code"), (10, "instalments"), (18, "code")}
    assert _count("SELECT count(*) FROM divine_payment_plans") == before


def test_payment_plan_schema_errors_point_at_the_instalment():
    plan = _plan(f"IMP{_tag().upper()}/S", "S1")
    plan["instalments"][1]["percent"] = -5
    r = _post("payment-plans", [plan])
    assert r.status_code == 422
    (err,) = r.json()["detail"]["errors"]
    assert err["row"] == 2 and err["field"] == "instalments" and err["message"].startswith("Instalment 2, percent")


def test_failure_during_write_rolls_back_every_row_and_the_key(monkeypatch):
    import DivineAPI.admin_imports_api as imports_api
    tag = _tag()
    assert _post("customers", [{"row": 2, "full_name": "Roll Back", "email": f"rb-{tag}@example.com",
                                "phone": "1"}]).status_code == 200
    from Divinepersistence import persistenceCustomer
    existing = persistenceCustomer().get_by_email(f"rb-{tag}@example.com")

    def boom(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(imports_api._import_service._persistence, "update_customer", boom)
    key = str(uuid.uuid4())
    rows = [{"row": 2, "full_name": "Fresh Row", "email": f"fresh-{tag}@example.com", "phone": "1"},
            {"row": 3, "id": existing.id, "phone": "2"}]
    r = _post("customers", rows, key=key)
    assert r.status_code == 500
    # The insert that ran before the failure was rolled back with it.
    assert _count("SELECT count(*) FROM divine_customer_users WHERE email = :e", e=f"fresh-{tag}@example.com") == 0
    assert _count("SELECT count(*) FROM divine_admin_import_requests WHERE idempotency_key = :k", k=key) == 0


# ---- review fixes -----------------------------------------------------------
def test_blank_plan_cells_get_the_create_defaults():
    tag = _tag().upper()
    plan = _plan(f"IMP{tag}/BLANK", f"K{tag}")
    plan["sort_order"] = ""
    plan["description"] = ""
    plan["instalments"][0]["charge_head"] = ""
    plan["instalments"][0]["due_days"] = None
    r = _post("payment-plans", [plan])
    assert r.status_code == 200, r.text
    listed = client.get("/admin/payment-plans", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"}).json()
    stored = next(p for p in listed["plans"] if p["code"] == f"IMP{tag}/BLANK")
    assert stored["sort_order"] == 0


def test_unknown_plan_and_instalment_columns_are_refused():
    tag = _tag().upper()
    plan = _plan(f"IMP{tag}/COLS", f"L{tag}")
    plan["sortorder"] = 3
    plan["instalments"][1]["is_paid"] = True
    r = _post("payment-plans", [plan])
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]["errors"]
    assert {(e["row"], e["field"]) for e in errors} == {(2, "sortorder"), (2, "instalments")}
    assert any("Instalment 2" in e["message"] and "is_paid" in e["message"] for e in errors)

    missing = _plan(f"IMP{tag}/MISS", f"M{tag}")
    missing["name"] = ""
    r = _post("payment-plans", [missing])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == [{"row": 2, "field": "name", "message": "name: This field is required."}]


def test_name_parts_longer_than_the_columns_are_refused():
    tag = _tag()
    r = _post("customers", [{"row": 2, "full_name": "Asha " + "V" * 151, "email": f"long-{tag}@example.com",
                             "phone": "1"}])
    assert r.status_code == 422
    assert [(e["row"], e["field"]) for e in r.json()["detail"]["errors"]] == [(2, "full_name")]


def test_imported_visits_link_to_broker_and_customer_account():
    tag = _tag()
    assert _post("brokers", [{"row": 2, "full_name": "Link Partner", "email": f"lp-{tag}@example.com",
                              "phone": "1", "project": "ops-divine-greens"}]).status_code == 200
    assert _post("customers", [{"row": 2, "full_name": "Link Buyer", "email": f"lb-{tag}@example.com",
                                "phone": "1"}]).status_code == 200
    from Divinepersistence import persistenceBroker, persistenceCustomer
    broker = persistenceBroker().get_by_email(f"lp-{tag}@example.com")
    customer = persistenceCustomer().get_by_email(f"lb-{tag}@example.com")

    r = _post("visits", [
        {"row": 2, "origin_type": "CHANNEL_PARTNER", "customer_name": f"CP Visit {tag}", "broker_id": broker.id,
         "visit_date": "2026-12-05"},
        {"row": 3, "origin_type": "CUSTOMER", "customer_name": f"Web Visit {tag}",
         "customer_email": f"LB-{tag}@example.com", "visit_date": "2026-12-06",
         # Not 'scheduled': test_visit_request_api assumes every scheduled
         # CUSTOMER visit in the shared test DB is its own.
         "status": "confirmed"},
    ])
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        rows = {m["customer_name"]: dict(m) for m in db.execute(text(
            "SELECT customer_name, broker_id, customer_id FROM divine_site_visits WHERE customer_name LIKE :t"),
            {"t": f"%{tag}"}).mappings()}
    assert rows[f"CP Visit {tag}"]["broker_id"] == broker.id
    assert rows[f"Web Visit {tag}"]["customer_id"] == customer.id

    r = _post("visits", [{"row": 2, "origin_type": "CHANNEL_PARTNER", "customer_name": "Nobody's",
                          "broker_id": "B00000", "visit_date": "2026-12-05"}])
    assert r.status_code == 422
    assert [(e["row"], e["field"]) for e in r.json()["detail"]["errors"]] == [(2, "broker_id")]


def test_an_id_collision_at_write_time_is_retried_with_fresh_ids(monkeypatch):
    import DivineAPI.admin_imports_api as imports_api
    from sqlalchemy.exc import IntegrityError
    persistence = imports_api._import_service._persistence
    real_insert = persistence.insert_customers
    calls = {"n": 0}

    def flaky(db, customers, created_by):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: divine_customer_users.id"))
        return real_insert(db, customers, created_by)

    monkeypatch.setattr(persistence, "insert_customers", flaky)
    tag = _tag()
    r = _post("customers", [{"row": 2, "full_name": "Retry Person", "email": f"retry-{tag}@example.com",
                             "phone": "1"}])
    assert r.status_code == 200, r.text
    assert calls["n"] == 2
    assert _count("SELECT count(*) FROM divine_customer_users WHERE email = :e", e=f"retry-{tag}@example.com") == 1


def test_expired_keys_are_purged_after_an_import_and_can_be_reused():
    old_key = str(uuid.uuid4())
    with SessionLocal() as db:
        db.execute(text(
            "INSERT INTO divine_admin_import_requests (id, admin_id, idempotency_key, entity, request_hash, "
            "response_json, created_date) VALUES (:id, 'someone-else', :k, 'customers', 'x', '{}', :t)"
        ), {"id": str(uuid.uuid4()), "k": old_key, "t": datetime.now(timezone.utc) - timedelta(hours=25)})
        db.commit()
    tag = _tag()
    assert _post("customers", [{"row": 2, "full_name": "Purge Trigger", "email": f"purge-{tag}@example.com",
                                "phone": "1"}]).status_code == 200
    assert _count("SELECT count(*) FROM divine_admin_import_requests WHERE idempotency_key = :k", k=old_key) == 0


def test_new_user_ids_are_unique_and_skip_taken_ids():
    from Divinepersistence.persistence_db import new_user_ids
    with SessionLocal() as db:
        taken = {r[0] for r in db.execute(text("SELECT id FROM divine_customer_users")).all()}
        ids = new_user_ids(db, "C", 2000)
    assert len(ids) == len(set(ids)) == 2000
    assert not taken & set(ids)
    assert all(i.startswith("C") and len(i) == 6 and i[1:].isdigit() for i in ids)

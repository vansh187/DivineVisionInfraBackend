"""End-to-end over HTTP: the whole payment-plan journey exactly as the customer
website and the admin panel call it, through the real FastAPI app, routers,
DTOs, services and database (SQLite test DB; external Supabase storage and
email are stubbed). If any layer's contract drifts, this breaks.

  admin adds a plan (pair charges + custom event)  -> customer previews it
  -> books with an RTGS payment carrying the plan  -> uploads the application
  -> profile + demand letter                        -> admin approves, sees plan
  -> admin bookings filter by customer              -> customer pays the dated
     instalment after the undated event             -> reminder job -> possession date
"""
import io
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

import jwt
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from DivineAPI.main import app
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_inventory import persistenceInventory

client = TestClient(app)
ADMIN_SECRET = "flow-admin-secret"
PDF = b"%PDF-1.4\n%flow booking application\n%%EOF"
RUN = uuid.uuid4().hex[:6].upper()
PLAN_CODE = f"PPFLOW{RUN}/CLP/10-30-30-30"
SHORT = f"PF{RUN}"


def _token(sub, role, secret):
    return jwt.encode({"sub": sub, "role": role, "username": sub,
                       "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, secret, algorithm="HS256")


PROJECT_NAME = f"Flow Estate {RUN}"          # own project: never touches other tests' data
PROJECT_ID = f"flow-estate-{RUN.lower()}"
_ENV_KEYS = ("ADMIN_JWT_SECRET_KEY", "DIVINE_JOBS_TOKEN")


@pytest.fixture(scope="module")
def world():
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["ADMIN_JWT_SECRET_KEY"] = ADMIN_SECRET
    os.environ["DIVINE_JOBS_TOKEN"] = "flow-jobs-token"
    import DivineAPI.payment_plan_api as plan_api
    plan_api._plan_service.clear_cache()
    customer = persistenceCustomer().create_user(
        username=f"flow_{RUN}@gmail.com", password_hash="x", email=f"flow_{RUN}@gmail.com", phone="9876500000",
        first_name="Flow", last_name="Buyer")
    unit_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit_id, project_name=PROJECT_NAME, city="Sonipat", unit_number=f"F{RUN}",
        area_sqmt=99.86, area_sqyd=119.43, status="available")
    data = {
        "customer_id": customer.id, "unit_id": unit_id,
        "customer": {"Authorization": f"Bearer {_token(customer.id, 'customer', 'testsecret')}"},
        "admin": {"Authorization": f"Bearer {_token('DV0009', 'admin', ADMIN_SECRET)}"},
        "state": {},
    }
    try:
        yield data
    finally:
        for k, v in saved_env.items():                     # leave the environment as we found it
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _cleanup(data)
        plan_api._plan_service.clear_cache()


def _cleanup(data):
    """Remove every row this module created from the shared test database."""
    from sqlalchemy import text as sql
    from Divinepersistence.persistence_db import engine
    cid, state = data["customer_id"], data["state"]
    with engine.begin() as c:
        bid, doc = state.get("booking_id"), state.get("document_id")
        if doc:
            c.execute(sql("DELETE FROM divine_payment_reminders WHERE booking_id = :d"), {"d": doc})
            c.execute(sql("DELETE FROM divine_payment_milestones WHERE booking_id = :d"), {"d": doc})
        if bid:
            c.execute(sql("DELETE FROM divine_booking_decisions WHERE booking_id = :b"), {"b": bid})
            c.execute(sql("DELETE FROM divine_bookings WHERE id = :b"), {"b": bid})
        c.execute(sql("DELETE FROM divine_documents WHERE owner_id = :c"), {"c": cid})
        c.execute(sql("DELETE FROM divine_payments WHERE owner_id = :c"), {"c": cid})
        c.execute(sql("DELETE FROM divine_customer_users WHERE id = :c"), {"c": cid})
        c.execute(sql("DELETE FROM divine_project_inventory WHERE id = :u"), {"u": data["unit_id"]})
        c.execute(sql("DELETE FROM divine_project_settings WHERE project_id = :p"), {"p": PROJECT_ID})
        c.execute(sql("DELETE FROM divine_payment_plan_instalments WHERE plan_id IN "
                      "(SELECT id FROM divine_payment_plans WHERE code = :code)"), {"code": PLAN_CODE})
        c.execute(sql("DELETE FROM divine_payment_plans WHERE code = :code"), {"code": PLAN_CODE})


PLAN = {
    "code": PLAN_CODE, "short_code": SHORT, "name": "Flow Construction Linked Plan",
    "description": "10% (+PLC, IFMS) on booking, 30% after plinth, 30% at 180 days, 30% (+CAM) on possession.",
    "project_id": PROJECT_ID, "sort_order": 900,
    "instalments": [
        {"label": "Booking Amount", "percent": 10, "due_trigger": "booking", "due_days": 0, "charge_head": "PLC+IFMS"},
        {"label": "On completion of plinth", "percent": 30, "due_trigger": "custom", "due_days": 30,
         "due_from": "Completion of plinth", "charge_head": None},
        {"label": "Within 180 days of booking", "percent": 30, "due_trigger": "booking", "due_days": 180,
         "charge_head": None},
        {"label": "On Offer of Possession", "percent": 30, "due_trigger": "possession", "due_days": 0,
         "charge_head": "CAM"},
    ],
}
PRICES = {"sale_price": 2627460, "plc_amount": 50000, "ifms_amount": 20000, "cam_amount": 30000}


def test_01_admin_adds_the_plan(world):
    assert client.post("/admin/payment-plans", json=PLAN).status_code == 401           # needs an admin
    r = client.post("/admin/payment-plans/validate", json=PLAN, headers=world["admin"])
    assert r.status_code == 200, r.text
    r = client.post("/admin/payment-plans", json=PLAN, headers=world["admin"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["booking_percent"] == 10 and body["valid"] and body["is_active"]
    assert body["instalments"][1]["due_text"] == "30 days after Completion of plinth"
    listed = client.get("/payment-plans", params={"project_id": PROJECT_ID}).json()["plans"]
    assert SHORT in [p["short_code"] for p in listed]


def test_02_customer_previews(world):
    r = client.post("/payment-plans/preview", json={"plan_code": SHORT, "project_id": PROJECT_ID, **PRICES})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["total"] == 2627460 + 100000
    assert p["rows"][0]["charge_head"] == "PLC+IFMS" and p["rows"][0]["amount"] == 262746 + 70000
    assert p["rows"][1]["due_date"] is None and p["rows"][3]["due_date"] is None
    assert sum(r["amount"] for r in p["rows"]) == p["total"]
    world["state"]["preview"] = p


def test_03_booking_payment_must_match_the_plan(world):
    p = world["state"]["preview"]
    base = {"purpose": "plot_booking", "inventory_id": world["unit_id"], "method": "rtgs_neft",
            "utr_number": f"UTR{RUN}", "payment_plan_code": SHORT, **PRICES}
    r = client.post("/payments/cash", json={**base, "amount": 262746}, headers=world["customer"])   # plain 10%
    assert r.status_code == 400 and r.json()["detail"] == "booking_amount_mismatch"
    r = client.post("/payments/cash", json={**base, "amount": p["booking_amount"]}, headers=world["customer"])
    assert r.status_code == 200, r.text
    pay = r.json()
    assert pay["status"] == "paid" and pay["booking_id"] and pay["inventory_status"] == "pending_kyc_review"
    world["state"].update(payment_id=pay["id"], booking_id=pay["booking_id"])


def _upload(world, form_extra=None):
    import json
    form = {"applicantName": "Flow Buyer", "unitNo": f"F{RUN}", "payment_plan_code": SHORT,
            "total_amount": world["state"]["preview"]["total"], **(form_extra or {})}
    with patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None), \
         patch("DivineService.service_document.serviceDocument._sign_url", return_value="https://signed.example/x"):
        return client.post(
            "/documents/project-booking-application",
            files={"file": ("booking.pdf", PDF, "application/pdf")},
            data={"document_type": "project_booking_application", "project_id": PROJECT_ID,
                  "payment_id": world["state"]["payment_id"], "form_data": json.dumps(form),
                  "kyc_document_ids": "[]", "aadhaar_verification_id": "av-1"},
            headers=world["customer"])


def test_04_upload_builds_the_plan_schedule(world):
    r = _upload(world, {"total_amount": 100})                                # tampered total
    assert r.status_code == 400 and r.json()["detail"] == "plan_mismatch"
    r = _upload(world)
    assert r.status_code == 200, r.text
    plan = r.json()["payment_plan"]
    assert plan["payment_plan"]["code"] == PLAN_CODE and plan["total_receivable"] == 2727460
    assert [row["status"] for row in plan["rows"]] == ["paid", "due", "due", "due"]
    world["state"]["document_id"] = r.json()["id"]
    again = _upload(world)                                                    # retry
    assert again.status_code == 409 and again.json()["detail"] == "booking_application_exists"


def test_05_customer_profile_shows_plan_and_event_rows(world):
    r = client.get("/customer/profile", headers=world["customer"])
    assert r.status_code == 200, r.text
    booking = next(b for b in r.json()["bookings"] if b.get("document_id") == world["state"]["document_id"])
    assert booking["payment_plan"]["code"] == PLAN_CODE
    rows = booking["payment_schedule"]
    assert rows[0]["charge_head"] == "PLC+IFMS" and rows[0]["status"] == "paid"
    assert rows[1]["due_trigger"] == "custom" and rows[1].get("due_date") is None   # profile omits nulls
    assert rows[1]["due_text"] == "30 days after Completion of plinth"
    assert rows[3]["due_trigger"] == "possession" and rows[3]["charge_head"] == "CAM"


def test_06_customer_demand_letter(world):
    r = client.get(f"/documents/{world['state']['document_id']}/demand-letter", headers=world["customer"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store, private"
    text_ = " ".join("".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages).split())
    assert PLAN_CODE in text_ and "30 days after Completion of plinth" in text_ and "BSP + CAM" in text_
    assert f"Plot No. F{RUN}" in text_
    other = {"Authorization": f"Bearer {_token('C00000', 'customer', 'testsecret')}"}
    assert client.get(f"/documents/{world['state']['document_id']}/demand-letter", headers=other).status_code == 403


def test_07_admin_approves_and_sees_the_plan(world):
    bid = world["state"]["booking_id"]
    r = client.post(f"/admin/bookings/{bid}/approve", json={"note": "KYC ok", "version": 1}, headers=world["admin"])
    assert r.status_code == 200, r.text
    detail = client.get(f"/admin/bookings/{bid}", headers=world["admin"]).json()
    assert detail["payment_plan"]["code"] == PLAN_CODE and detail["payment_plan"]["sale_price"] == 2627460
    rows = detail["payment_schedule"]
    assert rows[1]["due_text"] == "30 days after Completion of plinth" and rows[1]["due_from"] == "Completion of plinth"
    assert rows[0]["charge_head"] == "PLC+IFMS"
    listed = client.get("/admin/bookings", params={"customer_id": world["customer_id"], "page_size": 100},
                        headers=world["admin"]).json()
    assert [i["id"] for i in listed["items"]] == [bid]
    letter = client.get(f"/admin/bookings/{bid}/demand-letter", headers=world["admin"])
    assert letter.status_code == 200 and letter.headers["cache-control"] == "no-store, private"


def test_08_dated_instalment_after_the_event_can_be_paid(world):
    rows = client.get(f"/admin/bookings/{world['state']['booking_id']}", headers=world["admin"]).json()["payment_schedule"]
    m3 = rows[2]
    r = client.post("/payments/cash", json={
        "amount": m3["amount"], "purpose": "installment", "installment_no": 3, "inventory_id": world["unit_id"],
        "method": "rtgs_neft", "utr_number": f"UTRI{RUN}"}, headers=world["customer"])
    assert r.status_code == 200, r.text
    assert r.json()["installment_status"] == "paid"
    after = client.get(f"/admin/bookings/{world['state']['booking_id']}", headers=world["admin"]).json()["payment_schedule"]
    assert after[2]["status"] == "paid" and after[1]["status"] != "paid"


def test_09_reminder_job_runs(world):
    assert client.post("/jobs/payment-reminders", headers={"X-Jobs-Token": "wrong"}).status_code == 401
    with patch("DivineService.service_payment_reminders.dispatch_payment_reminder_email", return_value=True):
        r = client.post("/jobs/payment-reminders", headers={"X-Jobs-Token": "flow-jobs-token"})
    assert r.status_code == 200, r.text
    assert "errors" in r.json()


def test_10_possession_date_dates_the_possession_row_only(world):
    r = client.put(f"/admin/projects/{PROJECT_ID}/possession-date",
                   json={"possession_offer_date": "2028-06-01"}, headers=world["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["instalments_dated"] >= 1
    rows = client.get(f"/admin/bookings/{world['state']['booking_id']}", headers=world["admin"]).json()["payment_schedule"]
    assert rows[3]["due_date"] == "2028-06-01" and rows[1]["due_date"] is None


def test_11_admin_switches_the_plan_off(world):
    r = client.patch("/admin/payment-plans", json={"code": PLAN_CODE, "is_active": False}, headers=world["admin"])
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert SHORT not in [p["short_code"] for p in client.get("/payment-plans", params={"project_id": PROJECT_ID}).json()["plans"]]
    r = client.post("/payment-plans/preview", json={"plan_code": SHORT, **PRICES})
    assert r.status_code == 404
    # the existing booking keeps working
    r = client.get(f"/documents/{world['state']['document_id']}/demand-letter", headers=world["customer"])
    assert r.status_code == 200

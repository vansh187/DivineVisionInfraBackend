"""Payment plans for new customers: catalogue, calculation (client document
'Payment Plan's For Web'), booking-payment check, schedule at upload,
milestones, possession date, reminders, demand letter."""
import io
import json
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

import pytest
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import Divinepersistence  # noqa: F401
from Divinepersistence.persistence_db import Base
from Divinepersistence.persistence_milestone import persistenceMilestone
from Divinepersistence.persistence_payment_plan import persistencePaymentPlan
import DivineService.service_payment_plans as pp
from DivineService.service_payment_plans import (
    compute_schedule, load_plan_file, public_plan, servicePaymentPlans, sync_plans, validate_plan,
)

PLANS = {p["short_code"]: p for p in load_plan_file()}
DOC_PRICES = dict(sale_price=5000000, plc=100000, ifms=50000, cam=75000)


@pytest.fixture
def factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'pp.sqlite'}")
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng)


@pytest.fixture
def plan_persistence(factory):
    persistence = persistencePaymentPlan(session_factory=factory)
    sync_plans(persistence)
    return persistence


@pytest.fixture
def plan_service(factory, plan_persistence):
    return servicePaymentPlans(plan_persistence, persistenceMilestone(session_factory=factory))


def amounts(schedule):
    return [r["amount"] for r in schedule["rows"]]


# --------------------------------------------------------------------------
# Calculation - exactly the document
# --------------------------------------------------------------------------

def test_pp1_matches_the_document_example():
    s = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date="2026-11-01")
    assert amounts(s) == [500000, 750000, 1350000, 1300000, 825000, 500000]
    assert s["total"] == 5225000 and s["booking_amount"] == 500000
    assert [r["due_date"] for r in s["rows"]] == [
        "2026-11-01", "2026-12-16", "2027-01-30", "2027-04-30", "2027-07-29", None]
    assert [r["charge_head"] for r in s["rows"]] == [None, None, "PLC", "IFMS", "CAM", None]
    assert [r["basic_amount"] for r in s["rows"]] == [500000, 750000, 1250000, 1250000, 750000, 500000]
    assert s["rows"][5]["due_trigger"] == "possession" and s["rows"][5]["due_text"] == "On Offer of Possession"


def test_possession_row_gets_a_date_once_possession_is_offered():
    s = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date="2026-11-01", possession_date="2028-03-01")
    assert s["rows"][5]["due_date"] == "2028-03-01"


@pytest.mark.parametrize("code, booking_share", [("PP1", 10), ("PP2", 10), ("PP3", 50), ("PP4", 100)])
def test_every_plan_sums_to_total_and_books_its_first_instalment(code, booking_share):
    s = compute_schedule(PLANS[code], 2627461, 50000, 10000, 20000, booking_date="2026-11-09")
    assert sum(amounts(s)) == s["total"] == 2627461 + 80000
    assert sum(r["basic_amount"] for r in s["rows"]) == 2627461
    assert s["booking_amount"] == s["rows"][0]["amount"]
    assert s["rows"][0]["percent"] == booking_share


def test_pp4_carries_every_charge_on_booking():
    s = compute_schedule(PLANS["PP4"], **DOC_PRICES)
    assert amounts(s) == [5225000] and s["rows"][0]["charge_head"] == "PLC+IFMS+CAM"


def test_pp2_puts_charges_on_the_balance_row():
    s = compute_schedule(PLANS["PP2"], **DOC_PRICES, booking_date="2026-11-01")
    assert amounts(s) == [500000, 4725000] and s["rows"][1]["due_date"] == "2026-12-31"


def test_rounding_remainder_goes_to_the_last_row():
    s = compute_schedule(PLANS["PP3"], 1000001)
    assert [r["basic_amount"] for r in s["rows"]] == [500001, 250000, 150000, 100000]
    assert sum(r["basic_amount"] for r in s["rows"]) == 1000001


def test_charge_without_a_carrier_row_lands_on_the_last_row():
    plan = {"code": "X", "instalments": [
        {"seq": 1, "label": "On Booking", "percent": 40, "due_trigger": "booking", "due_days": 0},
        {"seq": 2, "label": "Later", "percent": 60, "due_trigger": "booking", "due_days": 30}]}
    s = compute_schedule(plan, 1000000, plc=5000, ifms=0, cam=7000)
    assert amounts(s) == [400000, 612000] and s["total"] == 1012000


def test_zero_charges_show_no_charge_head():
    s = compute_schedule(PLANS["PP1"], 1000000)
    assert all(r["charge_head"] is None for r in s["rows"])


@pytest.mark.parametrize("kwargs, code", [
    ({"sale_price": 0}, "invalid_sale_price"), ({"sale_price": -5}, "invalid_sale_price"),
    ({"sale_price": "abc"}, "invalid_sale_price"), ({"sale_price": None}, "invalid_sale_price"),
    ({"sale_price": True}, "invalid_sale_price"),
    ({"sale_price": 100, "plc": -1}, "invalid_plc_amount"), ({"sale_price": 100, "ifms": "x"}, "invalid_ifms_amount"),
    ({"sale_price": 100, "cam": float("nan")}, "invalid_cam_amount"),
    ({"sale_price": 600000000}, "total_amount_too_large"),
    ({"sale_price": 100, "booking_date": "09/11/2026"}, "invalid_booking_date"),
])
def test_invalid_inputs_raise_coded_errors(kwargs, code):
    with pytest.raises(ValueError, match=code):
        compute_schedule(PLANS["PP1"], **kwargs)


def test_amounts_accept_indian_formatting():
    s = compute_schedule(PLANS["PP4"], "Rs. 25,00,000", plc="1,00,000")
    assert s["total"] == 2600000


@pytest.mark.parametrize("bad", [
    {"instalments": []},
    {"instalments": [{"seq": 1, "label": "a", "percent": 90, "due_days": 0}]},                       # 90 %
    {"instalments": [{"seq": 1, "label": "a", "percent": 100, "due_days": 30}]},                     # not on booking
    {"instalments": [{"seq": 1, "label": "a", "percent": 100, "due_trigger": "possession"}]},
    {"instalments": [{"seq": 1, "label": "a", "percent": 100, "due_days": 0, "charge_head": "GST"}]},
    {"instalments": [{"seq": 1, "label": "a", "percent": 0, "due_days": 0},
                     {"seq": 2, "label": "b", "percent": 100, "due_days": 9}]},
])
def test_invalid_plans_are_rejected(bad):
    with pytest.raises(ValueError, match="invalid_payment_plan"):
        validate_plan(bad)


def test_public_plan_includes_rules_and_the_document_example():
    view = public_plan(PLANS["PP1"])
    assert view["booking_percent"] == 10 and len(view["instalments"]) == 6
    assert view["instalments"][5]["due_text"] == "On Offer of Possession"
    assert view["example"]["total"] == 5225000
    assert [r["amount"] for r in view["example"]["rows"]] == [500000, 750000, 1350000, 1300000, 825000, 500000]


# --------------------------------------------------------------------------
# Catalogue (DB)
# --------------------------------------------------------------------------

def test_seed_loads_four_plans_in_order(plan_service):
    plans = plan_service.list_plans("suraksha-enclave")
    assert [p["short_code"] for p in plans] == ["PP1", "PP2", "PP3", "PP4"]
    assert plan_service.list_plans("ops-divine-greens") == []      # Suraksha plans only


def test_sync_is_idempotent(plan_persistence):
    assert sync_plans(plan_persistence) == {"created": 0, "updated": 4}
    assert len(plan_persistence.list_active("suraksha-enclave")) == 4


def test_new_version_replaces_old_but_old_version_stays_readable(plan_persistence):
    v2 = json.loads(json.dumps(PLANS["PP2"]))
    v2["version"] = 2
    v2["instalments"][1]["due_days"] = 90
    assert plan_persistence.upsert_plan(v2) == "created"
    active = plan_persistence.get_by_code("PP2/FPP/10-90")
    assert active["version"] == 2 and active["instalments"][1]["due_days"] == 90
    old = plan_persistence.get_by_code("PP2/FPP/10-90", version=1)
    assert old["instalments"][1]["due_days"] == 60 and not old["is_active"]


def test_unknown_plan_and_wrong_project(plan_service):
    with pytest.raises(ValueError, match="payment_plan_not_found"):
        plan_service.get_plan("PP9/NOPE")
    with pytest.raises(ValueError, match="payment_plan_not_for_project"):
        plan_service.preview("PP1/FPP/10-15-25-25-15-10", 100000, project_id="ops-divine-greens")


def test_preview_uses_today_ist_and_project_possession_date(plan_service, factory):
    plan_service.set_possession_date("suraksha-enclave", "2028-01-15", "admin:A1")
    s = plan_service.preview("PP1/FPP/10-15-25-25-15-10", **DOC_PRICES)
    assert s["booking_date"] == pp.ist_today().isoformat()
    assert s["rows"][5]["due_date"] == "2028-01-15"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, plan_service):
    import DivineAPI.payment_plan_api as module
    monkeypatch.setattr(module, "_plan_service", plan_service)
    from fastapi.testclient import TestClient
    from DivineAPI.main import app
    return TestClient(app)


def test_api_list_detail_and_preview(api):
    r = api.get("/payment-plans", params={"project_id": "suraksha-enclave"})
    assert r.status_code == 200 and r.json()["count"] == 4
    d = api.get("/payment-plans/PP1/FPP/10-15-25-25-15-10")
    assert d.status_code == 200 and d.json()["short_code"] == "PP1"
    p = api.post("/payment-plans/preview", json={"plan_code": "PP1/FPP/10-15-25-25-15-10", "sale_price": 5000000,
                                                "plc_amount": 100000, "ifms_amount": 50000, "cam_amount": 75000,
                                                "booking_date": "2026-11-01"})
    assert p.status_code == 200
    body = p.json()
    assert body["total"] == 5225000 and body["booking_amount"] == 500000
    assert body["rows"][2]["due_date"] == "2027-01-30"


def test_api_errors(api):
    assert api.get("/payment-plans/NOPE").status_code == 404
    assert api.post("/payment-plans/preview", json={"plan_code": "NOPE", "sale_price": 1}).status_code == 404
    assert api.post("/payment-plans/preview", json={"plan_code": "PP4/FPP/100", "sale_price": 0}).status_code == 422
    assert api.post("/payment-plans/preview", json={"plan_code": "PP4/FPP/100", "sale_price": 1,
                                                    "plc_amount": -1}).status_code == 422


def test_possession_endpoint_requires_admin(api):
    r = api.put("/admin/projects/suraksha-enclave/possession-date", json={"possession_offer_date": "2028-01-01"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Booking payment must equal the plan's first instalment
# --------------------------------------------------------------------------

def _payment_service(plan_service, created=None):
    from DivineService.service_payment import servicePayment
    payments, inventory = MagicMock(), MagicMock()
    inventory.get_by_id.return_value = SimpleNamespace(id="INV-1", status="available",
                                                       project_name="Suraksha Enclave", area_sqyd=119.43)
    payments.create_payment.side_effect = lambda **kw: SimpleNamespace(**kw)
    svc = servicePayment(payments, inventory, MagicMock())
    svc._payment_plan_service = plan_service
    svc._key_id, svc._key_secret = "rzp_test", "secret"
    return svc, payments


PLAN = {"payment_plan_code": "PP3/FPP/50-25-15-10", "sale_price": 2000000, "plc_amount": 0,
        "ifms_amount": 0, "cam_amount": 0}


def test_order_with_wrong_booking_amount_is_refused(plan_service):
    svc, payments = _payment_service(plan_service)
    with pytest.raises(ValueError, match="booking_amount_mismatch"):
        svc.create_order(200000, owner_id="C1", owner_role="customer", purpose="plot_booking",
                         inventory_id="INV-1", booking_plan=PLAN)          # 10% but PP3 books 50%
    payments.create_payment.assert_not_called()


def test_order_with_plan_stores_the_terms(plan_service, monkeypatch):
    svc, payments = _payment_service(plan_service)
    client = MagicMock()
    client.order.create.return_value = {"id": "order_1", "status": "created"}
    monkeypatch.setattr(svc, "_client", lambda: client)
    record, _ = svc.create_order(1000000, owner_id="C1", owner_role="customer", purpose="plot_booking",
                                 inventory_id="INV-1", booking_plan=PLAN)
    terms = record.notes["payment_plan"]
    assert terms["payment_plan_code"] == "PP3/FPP/50-25-15-10" and terms["booking_amount"] == 1000000
    assert terms["total_amount"] == 2000000 and terms["payment_plan_version"] == 1


def test_cash_booking_with_plan_checks_and_stores_terms(plan_service):
    svc, payments = _payment_service(plan_service)
    with pytest.raises(ValueError, match="booking_amount_mismatch"):
        svc.record_cash_payment(5, owner_id="C1", owner_role="customer", purpose="plot_booking",
                                inventory_id="INV-1", booking_plan=PLAN)
    svc._settle_by_purpose = lambda record: record
    record = svc.record_cash_payment(1000000, owner_id="C1", owner_role="customer", purpose="plot_booking",
                                     inventory_id="INV-1", booking_plan=PLAN, note="paid at office")
    assert record.notes["payment_plan"]["booking_amount"] == 1000000 and record.notes["note"] == "paid at office"


def test_plan_only_allowed_on_booking_payments(plan_service):
    svc, _ = _payment_service(plan_service)
    with pytest.raises(ValueError, match="payment_plan_only_for_booking"):
        svc._booking_plan_terms("installment", 100, PLAN, None)


def test_without_a_plan_the_old_flow_is_unchanged(plan_service):
    svc, _ = _payment_service(plan_service)
    assert svc._booking_plan_terms("plot_booking", 12345, None, "INV-1") is None


# --------------------------------------------------------------------------
# Upload: schedule from the payment's stored terms
# --------------------------------------------------------------------------

def _terms(plan_service, code="PP1/FPP/10-15-25-25-15-10", **prices):
    p = {**DOC_PRICES, **prices}
    return plan_service.booking_terms(code, p["sale_price"], p["plc"], p["ifms"], p["cam"])


@pytest.fixture
def doc_service(monkeypatch, plan_service):
    from DivineService.service_document import serviceDocument
    monkeypatch.setattr(pp, "servicePaymentPlans", lambda *a, **k: plan_service)
    return serviceDocument(MagicMock(), MagicMock())


def _payment(terms, amount=None, created=None):
    return SimpleNamespace(id="pay-1", amount=amount if amount is not None else terms["booking_amount"],
                           notes=json.dumps({"payment_plan": terms}), purpose="plot_booking",
                           created_date=created or datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc))


def test_upload_schedule_comes_from_the_payment_terms(doc_service, plan_service):
    plan = doc_service._plan_schedule_from_payment(_payment(_terms(plan_service)), {"applicantName": "A"})
    assert plan["booking_date"] == "2026-11-01"           # 31 Oct 20:00 UTC = 1 Nov in IST
    assert plan["total_receivable"] == 5225000 and plan["total_received"] == 500000
    assert [r["status"] for r in plan["rows"]] == ["paid", "due", "due", "due", "due", "due"]
    assert plan["payment_plan"]["code"] == "PP1/FPP/10-15-25-25-15-10" and plan["payment_plan"]["plc"] == 100000


def test_upload_rejects_a_tampered_total_or_plan(doc_service, plan_service):
    payment = _payment(_terms(plan_service))
    with pytest.raises(ValueError, match="plan_mismatch"):
        doc_service._plan_schedule_from_payment(payment, {"total_amount": 100})
    with pytest.raises(ValueError, match="plan_mismatch"):
        doc_service._plan_schedule_from_payment(payment, {"payment_plan_code": "PP4/FPP/100"})
    assert doc_service._plan_schedule_from_payment(payment, {"total_amount": "52,25,000"})


def test_upload_without_plan_terms_uses_legacy_flow(doc_service):
    assert doc_service._plan_schedule_from_payment(SimpleNamespace(notes={}), {}) is None
    assert doc_service._plan_schedule_from_payment(SimpleNamespace(notes="not json"), {}) is None


def _upload(doc_service, payment):
    doc_service._payment_persistence.get_by_id.return_value = SimpleNamespace(
        owner_id="C1", status="paid", razorpay_order_id=None, razorpay_payment_id=None, **vars(payment))
    return doc_service.upload_booking_application(
        b"%PDF-1.4 x", "application/pdf", "project_booking_application", "suraksha-enclave", "pay-1",
        None, None, json.dumps({"applicantName": "A"}), owner_id="C1", owner_role="customer")


def test_second_upload_for_the_same_payment_is_refused(doc_service, plan_service):
    doc_service._persistence.get_latest_by_payment_id.return_value = SimpleNamespace(
        document_type="project_booking_application")
    with pytest.raises(ValueError, match="booking_application_exists"):
        _upload(doc_service, _payment(_terms(plan_service)))


def test_instalment_payment_cannot_open_a_booking(doc_service, plan_service):
    payment = _payment(_terms(plan_service))
    payment.purpose = "installment"
    with pytest.raises(ValueError, match="payment_not_for_booking"):
        _upload(doc_service, payment)


# --------------------------------------------------------------------------
# Milestones
# --------------------------------------------------------------------------

def _milestones(factory):
    from DivineService.service_milestones import serviceMilestones
    svc = serviceMilestones(milestone_persistence=persistenceMilestone(session_factory=factory),
                            profile_persistence=MagicMock(), payment_persistence=MagicMock())
    return svc


def test_milestones_from_a_plan_schedule(factory, plan_service):
    plan = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date="2026-11-01")
    plan["rows"] = [{**r, "status": "paid" if i == 0 else "due"} for i, r in enumerate(plan["rows"])]
    svc = _milestones(factory)
    rows = svc._rows_from_plan(plan, "doc-1", "C1", "suraksha-enclave", "INV-1", "pay-1", "2026-11-01")
    assert [r["status"] for r in rows] == ["paid"] + ["upcoming"] * 5
    assert rows[5]["due_date"] is None and rows[5]["due_trigger"] == "possession"
    assert (rows[2]["charge_head"], rows[2]["charge_amount"], rows[2]["basic_amount"]) == ("PLC", 100000, 1250000)
    assert rows[0]["paid_on"] == date(2026, 11, 1)
    svc._persistence.create_milestones(rows)
    stored = svc._persistence.list_for_booking("doc-1")
    assert [int(r.amount) for r in stored] == [500000, 750000, 1350000, 1300000, 825000, 500000]


def test_booking_payment_short_of_first_instalment_leaves_it_unpaid(factory):
    plan = compute_schedule(PLANS["PP3"], 1000000)
    plan["rows"] = [{**r, "status": "due"} for r in plan["rows"]]
    plan["payment_plan"] = plan["plan"]                     # a payment-plan booking
    rows = _milestones(factory)._rows_from_plan(plan, "doc-2", "C1", "p", "i", "pay", "2026-11-01")
    assert rows[0]["status"] == "upcoming"


def test_backfill_rebuilds_from_the_frozen_plan_not_the_default(factory):
    plan = compute_schedule(PLANS["PP3"], 2000000, booking_date="2026-11-01")
    form = {"total_consideration": 2000000, "booking_date": "2026-11-01", "payment_plan": plan["plan"],
            "payment_schedule": [{**r, "status": "paid" if i == 0 else "due"} for i, r in enumerate(plan["rows"])]}
    rows = _milestones(factory)._plan_rows_for_record({"id": "doc-3", "form_data": form}, "C1", "doc-3")
    assert [r["amount"] for r in rows] == [1000000, 500000, 300000, 200000]


def test_possession_date_dates_unpaid_possession_rows(factory, plan_service):
    plan = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date="2026-11-01")
    svc = _milestones(factory)
    svc._persistence.create_milestones(
        svc._rows_from_plan(plan, "doc-4", "C1", "suraksha-enclave", "INV-1", "pay-1", "2026-11-01"))
    out = plan_service.set_possession_date("suraksha-enclave", date(2028, 2, 1), "admin:A1")
    assert out["instalments_dated"] == 1
    last = svc._persistence.list_for_booking("doc-4")[-1]
    assert str(last.due_date)[:10] == "2028-02-01"
    with pytest.raises(ValueError, match="invalid_possession_date"):
        plan_service.set_possession_date("suraksha-enclave", None, "admin:A1")


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

def _reminders(factory, form_source=None, email_ok=True):
    from DivineService.service_payment_reminders import serviceReminders
    milestones = persistenceMilestone(session_factory=factory)
    plan = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date=(date.today() - timedelta(days=50)).isoformat())
    from DivineService.service_milestones import serviceMilestones
    msvc = serviceMilestones(milestone_persistence=milestones, profile_persistence=MagicMock(),
                             payment_persistence=MagicMock())
    msvc._offline_submissions = MagicMock()
    msvc._offline_submissions.pending_milestones.return_value = set()
    milestones.create_milestones(msvc._rows_from_plan(plan, "doc-r", "C1", "suraksha-enclave", "INV", "pay",
                                                      plan["booking_date"]))
    profile = MagicMock()
    form = {"unitNo": "C61", "project_name": "Suraksha Enclave"}
    if form_source:
        form["source"] = form_source
    profile.list_booking_applications.return_value = [{"id": "doc-r", "form_data": form}]
    customers = MagicMock()
    customers.get_by_id.return_value = SimpleNamespace(email="buyer@gmail.com", first_name="A")
    sent = []
    import DivineService.service_payment_reminders as module
    svc = serviceReminders(milestone_persistence=milestones, customer_persistence=customers,
                           profile_persistence=profile, milestone_service=msvc, email_service=MagicMock())
    return svc, module, sent, milestones


def test_overdue_instalment_gets_a_reminder_with_its_charge(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory)
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: sent.append(k) or True)
    summary = svc.run()
    assert summary["sent"] == {"OVERDUE": 1}             # 45-day row is 5 days late
    assert sent[0]["unit_number"] == "C61" and sent[0]["amount"] == 750000
    assert svc.run()["sent"] == {}                        # once per week


def test_failed_send_is_retried_next_run(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory)
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: False)
    assert svc.run()["errors"] == 1
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: sent.append(k) or True)
    assert svc.run()["sent"] == {"OVERDUE": 1}


def test_offline_imported_buyers_get_no_reminders(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory, form_source="offline_import")
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: sent.append(k) or True)
    assert svc.run()["sent"] == {} and sent == []


def test_global_switch_disables_reminders(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory)
    monkeypatch.setenv("PAYMENT_REMINDERS_ENABLED", "false")
    assert svc.run()["disabled"] is True


def test_run_is_bounded(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory)
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: True)
    assert svc.run(max_seconds=-1)["truncated"] is True


def test_undated_possession_row_never_triggers_a_reminder():
    from DivineService.service_payment_reminders import pick_kind
    assert pick_kind(None, date.today()) == (None, None)


def test_job_token_header_and_query(monkeypatch):
    from DivineAPI.jobs_api import _require_job_token
    monkeypatch.setenv("DIVINE_JOBS_TOKEN", "s3cret-token")
    _require_job_token(None, "s3cret-token")
    _require_job_token("s3cret-token", None)
    for key, header in (("wrong", None), (None, "wrong"), (None, None)):
        with pytest.raises(HTTPException) as e:
            _require_job_token(key, header)
        assert e.value.status_code == 401


# --------------------------------------------------------------------------
# Demand letter
# --------------------------------------------------------------------------

def _plan_form(paid_rows=1, possession_date=None):
    plan = compute_schedule(PLANS["PP1"], **DOC_PRICES, booking_date="2026-01-01", possession_date=possession_date)
    rows = [{**r, "status": "paid" if i < paid_rows else "due"} for i, r in enumerate(plan["rows"])]
    paid = sum(r["amount"] for r in rows if r["status"] == "paid")
    return {"projectId": "suraksha-enclave", "applicantName": "Dheeraj Nirwal", "unitNo": "C61",
            "plotAreaSqYd": "119.43", "total_consideration": plan["total"], "amount_received": paid,
            "payment_schedule": rows, "payment_plan": {**plan["plan"], "total": plan["total"]}}


def _letter_text(form, issued=date(2026, 11, 9)):
    from DivineService.service_demand_letter import generate_demand_letter_pdf
    pdf = generate_demand_letter_pdf(form, "C1", issued_on=issued)
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def test_letter_shows_heads_basic_and_total_instalment_and_plan():
    text = _letter_text(_plan_form())
    assert "PP1/FPP/10-15-25-25-15-10" in text
    assert "BSP + PLC" in text and "BSP + IFMS" in text and "BSP + CAM" in text
    assert "12,50,000" in text and "13,50,000" in text        # basic and total of the PLC row
    assert "On Possession" in text
    assert "52,25,000" in text                                 # total plot amount incl. charges
    assert "On Booking" not in text                            # paid row hidden


def test_letter_overdue_excludes_undated_possession_row():
    from DivineService.service_demand_letter import demand_letter_figures
    fig = demand_letter_figures(_plan_form(), date(2026, 11, 9))
    # due by 9 Nov 2026: 45d (15 Feb) + 90d (1 Apr) + 180d (30 Jun) + 270d (28 Sep)
    assert fig["overdue"] == 750000 + 1350000 + 1300000 + 825000
    assert not fig["awaiting_possession"]


def test_only_possession_left_and_undated_means_nothing_due_yet():
    from DivineService.service_demand_letter import demand_letter_figures
    form = _plan_form(paid_rows=5)
    fig = demand_letter_figures(form, date(2026, 11, 9))
    assert fig["demand"] == 0 and fig["awaiting_possession"] and fig["outstanding"] == 500000
    assert "payable on Offer of Possession" in " ".join(_letter_text(form).split())


def test_possession_row_is_demanded_once_dated_and_past():
    from DivineService.service_demand_letter import demand_letter_figures
    fig = demand_letter_figures(_plan_form(paid_rows=5, possession_date="2026-10-01"), date(2026, 11, 9))
    assert fig["overdue"] == 500000 and fig["demand"] == 500000


def test_demand_letter_pdf_is_not_cacheable(monkeypatch):
    import DivineAPI.documents_api as api
    svc = MagicMock()
    svc.get_demand_letter.return_value = (b"%PDF-1.4", "demand-letter-d1.pdf")
    monkeypatch.setattr(api, "_doc_service", svc)
    resp = api.get_demand_letter("d1", current_user={"sub": "C1", "role": "customer"})
    assert resp.headers["cache-control"] == "no-store, private"
    assert resp.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def test_profile_exposes_plan_and_charge_columns():
    from DivineService.service_customer_profile import serviceCustomerProfile
    svc = serviceCustomerProfile(persistence=MagicMock())
    svc._resolve_amount_received = lambda *a, **k: None
    svc._apply_milestone_schedule = lambda *a, **k: None
    booking = svc._booking_from_record({"id": "d1", "form_data": _plan_form(), "created_date": None}, "C1",
                                       multi_booking=False, all_rows=[])
    assert booking.payment_plan["code"] == "PP1/FPP/10-15-25-25-15-10"
    row = booking.payment_schedule[2]
    assert (row.charge_head, row.charge_amount, row.basic_amount, row.amount) == ("PLC", 100000, 1250000, 1350000)
    assert booking.payment_schedule[5].due_trigger == "possession" and booking.payment_schedule[5].due_date is None



# --------------------------------------------------------------------------
# Corner cases (code review round 2)
# --------------------------------------------------------------------------

def test_legacy_token_payment_still_settles_row_one(factory):
    """Legacy booking (no plan): a token below 10% still marks row 1 paid, as before."""
    from DivineService.service_payment_schedule import build_payment_schedule
    plan = build_payment_schedule(2500000, 200000, "2026-01-01")
    assert plan["rows"][0]["status"] == "due"               # the generic builder says 'due'...
    rows = _milestones(factory)._rows_from_plan(plan, "doc-l", "C1", "p", "i", "pay-1", "2026-01-01")
    assert rows[0]["status"] == "paid" and rows[0]["paid_payment_id"] == "pay-1"   # ...but the payment is credited


def test_booking_terms_need_a_real_plot(plan_service):
    svc, _ = _payment_service(plan_service)
    with pytest.raises(ValueError, match="inventory_id_required"):
        svc._booking_plan_terms("plot_booking", 1000000, PLAN, None)
    svc._inventory_persistence.get_by_id.return_value = None
    with pytest.raises(ValueError, match="unit_not_found"):
        svc._booking_plan_terms("plot_booking", 1000000, PLAN, "INV-X")
    svc._inventory_persistence.get_by_id.side_effect = RuntimeError("db down")
    with pytest.raises(ValueError, match="plot_lookup_failed"):
        svc._booking_plan_terms("plot_booking", 1000000, PLAN, "INV-1")


def test_plan_of_another_project_is_refused_for_the_plot(plan_service):
    svc, _ = _payment_service(plan_service)
    svc._inventory_persistence.get_by_id.return_value = SimpleNamespace(
        id="INV-2", status="available", project_name="OPS Divine Greens", area_sqyd=131.43)
    with pytest.raises(ValueError, match="payment_plan_not_for_project"):
        svc._booking_plan_terms("plot_booking", 1000000, PLAN, "INV-2")


@pytest.mark.parametrize("sale_price", [100000, 99_000_000])      # Rs 837 / Rs 8.3 lakh per sq yd
def test_absurd_declared_price_is_refused(plan_service, sale_price):
    svc, _ = _payment_service(plan_service)
    plan = {**PLAN, "sale_price": sale_price}
    terms_amount = sale_price * 0.5
    with pytest.raises(ValueError, match="sale_price_out_of_range"):
        svc._booking_plan_terms("plot_booking", terms_amount, plan, "INV-1")


def test_price_range_is_configurable(plan_service, monkeypatch):
    svc, _ = _payment_service(plan_service)
    monkeypatch.setenv("PLAN_MIN_RATE_PER_SQYD", "30000")             # 20 lakh / 119.43 = 16,746 per sq yd
    with pytest.raises(ValueError, match="sale_price_out_of_range"):
        svc._booking_plan_terms("plot_booking", 1000000, PLAN, "INV-1")


def test_terms_record_the_plot_and_project(plan_service):
    svc, _ = _payment_service(plan_service)
    terms = svc._booking_plan_terms("plot_booking", 1000000, PLAN, "INV-1")
    assert (terms["project_id"], terms["inventory_id"], terms["area_sqyd"]) == ("suraksha-enclave", "INV-1", 119.43)


def test_upload_uses_the_plots_project_possession_date(doc_service, plan_service):
    plan_service.set_possession_date("suraksha-enclave", date(2028, 5, 1), "admin:A1")
    terms = {**_terms(plan_service), "project_id": "suraksha-enclave"}
    plan = doc_service._plan_schedule_from_payment(_payment(terms), {})
    assert plan["rows"][5]["due_date"] == "2028-05-01"


def test_booking_amount_tolerates_paise_rounding(plan_service):
    svc, _ = _payment_service(plan_service)
    odd = {**PLAN, "sale_price": 2000001.40}                 # rounds to 20,00,001 -> PP3 booking 10,00,001 (half-up)
    assert svc._booking_plan_terms("plot_booking", 1000000.6, odd, "INV-1")["booking_amount"] == 1000001
    with pytest.raises(ValueError, match="booking_amount_mismatch"):
        svc._booking_plan_terms("plot_booking", 999998, odd, "INV-1")


def test_short_code_is_accepted_case_insensitive(plan_service):
    assert plan_service.get_plan("pp3")["code"] == "PP3/FPP/50-25-15-10"
    assert plan_service.preview(" PP1 ".strip(), **DOC_PRICES)["plan"]["code"] == "PP1/FPP/10-15-25-25-15-10"


def test_plan_retired_after_payment_still_builds_the_paid_version(doc_service, plan_service, plan_persistence):
    terms = _terms(plan_service, code="PP2/FPP/10-90", plc=0, ifms=0, cam=0)
    v2 = json.loads(json.dumps(PLANS["PP2"]))
    v2["version"] = 2
    v2["instalments"][1]["due_days"] = 120
    plan_persistence.upsert_plan(v2)
    plan_service.clear_cache()
    plan = doc_service._plan_schedule_from_payment(_payment(terms), {})
    assert plan["payment_plan"]["version"] == 1 and plan["rows"][1]["due_days"] == 60


def test_tiny_sale_price_that_rounds_a_row_to_zero_is_refused():
    with pytest.raises(ValueError, match="sale_price_too_small"):
        compute_schedule(PLANS["PP1"], 3)


@pytest.mark.parametrize("bad_rows", [
    [{"seq": 1, "label": "a", "percent": 50, "due_days": 0}, {"seq": 1, "label": "b", "percent": 50, "due_days": 9}],
    [{"seq": 1, "label": "", "percent": 100, "due_days": 0}],
    [{"seq": "x", "label": "a", "percent": 100, "due_days": 0}],
    [{"seq": 1, "label": "a", "percent": 100, "due_days": "soon"}],
    [{"seq": 0, "label": "a", "percent": 100, "due_days": 0}],
    ["not a dict"],
])
def test_malformed_plan_definitions_are_rejected(bad_rows):
    with pytest.raises(ValueError, match="invalid_payment_plan"):
        validate_plan({"instalments": bad_rows})


def test_fractional_percent_plan_sums_exactly():
    plan = {"code": "T", "instalments": [
        {"seq": 1, "label": "a", "percent": 33.333, "due_days": 0},
        {"seq": 2, "label": "b", "percent": 33.333, "due_days": 30},
        {"seq": 3, "label": "c", "percent": 33.334, "due_days": 60}]}
    s = compute_schedule(plan, 1000000)
    assert sum(r["amount"] for r in s["rows"]) == 1000000


def test_leap_day_and_year_end_due_dates():
    s = compute_schedule(PLANS["PP2"], 1000000, booking_date="2027-12-31")
    assert s["rows"][1]["due_date"] == "2028-02-29"


def test_rollback_never_erases_a_booking_made_after_the_sync(tmp_path):
    from DivineService.service_offline_import import InventorySheetSync
    eng = create_engine(f"sqlite:///{tmp_path / 'rb.sqlite'}")
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO divine_project_inventory(id, project_name, city, unit_number, unit_type, "
                       "area_sqmt, area_sqyd, status) VALUES ('u1', 'Suraksha Enclave', 'Sonipat', 'B12', 'plot', "
                       "95, 113.69, 'held')"))
    header = ["Plot No.", "Width(mtr.)", "Length(mtr.)", "Area in sq mtrs", "Area in sq yard", "Status", "Project "]
    sync = InventorySheetSync(eng)
    report = sync.run([header, ["B12", "", "", "95", "113.69", "Unsold", "Suraksha Enclave"]],
                      "Suraksha Enclave", "Sonipat", apply=True)
    with eng.begin() as c:   # a customer books the now-available plot
        c.execute(text("UPDATE divine_project_inventory SET status='booked', booked_by='C12345', "
                       "booked_payment_id='P1' WHERE id='u1'"))
    rb = sync.rollback(report.batch_id)
    with eng.connect() as c:
        row = c.execute(text("SELECT status, booked_by, booked_payment_id FROM divine_project_inventory")).one()
    assert tuple(row) == ("booked", "C12345", "P1")
    assert any(k == "KEPT" for k, _, _ in rb.actions)


def test_admin_document_views_never_sign_imported_records():
    from DivineService.service_document import serviceDocument
    persistence = MagicMock()
    imported = SimpleNamespace(document_type="project_booking_application", status="imported",
                               storage_path="imported://b/s/C61", storage_bucket=None)
    real = SimpleNamespace(document_type="aadhaar_front", status="uploaded", storage_path="C1/a.jpg",
                           storage_bucket="documents")
    persistence.get_latest_by_owner_and_type.return_value = imported
    persistence.get_latest_by_owner_and_types.return_value = [imported, real]
    svc = serviceDocument(persistence, MagicMock())
    svc._sign_url = MagicMock(side_effect=AssertionError("must not sign"))
    signed_paths = []
    svc._sign_urls_batch = lambda paths, bucket=None: signed_paths.extend(paths) or {p: "https://x" for p in paths}
    assert svc.admin_get_latest("project_booking_application", "C1") == (imported, None, None)
    out = svc.admin_get_latest_batch(["project_booking_application", "aadhaar_front"], "C1")
    assert signed_paths == ["C1/a.jpg"]
    assert out["aadhaar_front"][1] == "https://x" and out["project_booking_application"][1] is None


def test_reminder_dedupe_runs_before_loading_booking_labels(factory, monkeypatch):
    svc, module, sent, _ = _reminders(factory)
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: True)
    svc.run()                                             # sends + records
    svc._profile.list_booking_applications.reset_mock()
    svc.run()                                             # already sent -> no label lookup at all
    svc._profile.list_booking_applications.assert_not_called()


def test_admin_detail_shows_the_booked_plan(monkeypatch):
    from DivineService.service_booking_kyc import serviceBookingKyc
    svc = serviceBookingKyc.__new__(serviceBookingKyc)
    svc._document_service = MagicMock()
    svc._document_service.get_booking_application_document.return_value = SimpleNamespace(
        form_data=json.dumps({"payment_plan": {"code": "PP1/FPP/10-15-25-25-15-10", "sale_price": 5000000}}))
    assert svc._booking_plan_snapshot("pay-1")["sale_price"] == 5000000
    svc._document_service.get_booking_application_document.side_effect = RuntimeError("x")
    assert svc._booking_plan_snapshot("pay-1") is None


def test_pdf_header_constant_is_shared():
    import DivineAPI.booking_api as a, DivineAPI.documents_api as b, DivineAPI.payment_api as c
    import DivineAPI.admin_booking_kyc_api as d
    from DivineAPI.pdf_headers import PRIVATE_PDF_HEADERS
    assert a._PRIVATE_PDF_HEADERS is b._PRIVATE_PDF_HEADERS is c._PRIVATE_PDF_HEADERS is d._PRIVATE_PDF_HEADERS \
        is PRIVATE_PDF_HEADERS



# --------------------------------------------------------------------------
# Admin: add any plan / new version / edit / deactivate
# --------------------------------------------------------------------------

PP5 = {
    "code": "PP5/FPP/20-30-50", "short_code": "PP5", "name": "Flexible Payment Plan 20-30-50",
    "description": "20% on booking, 30% in 60 days, 50% in 120 days.", "project_id": "suraksha-enclave",
    "sort_order": 5,
    "instalments": [
        {"label": "On Booking", "percent": 20, "due_trigger": "booking", "due_days": 0},
        {"label": "Within 60 days of booking", "percent": 30, "due_days": 60, "charge_head": "PLC"},
        {"label": "Within 120 days of booking", "percent": 50, "due_days": 120, "charge_head": "IFMS"},
    ],
}


@pytest.fixture
def admin_api(api, monkeypatch):
    import jwt as _jwt
    monkeypatch.setenv("ADMIN_JWT_SECRET_KEY", "pp-admin-secret")
    token = _jwt.encode({"sub": "DV0001", "role": "admin",
                         "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "pp-admin-secret", algorithm="HS256")
    api.headers.update({"Authorization": f"Bearer {token}"})
    return api


def test_admin_adds_a_plan_and_customers_see_it(admin_api):
    r = admin_api.post("/admin/payment-plans", json=PP5)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1 and body["is_active"] and body["booking_percent"] == 20
    # CAM has no row -> it lands on the last row; the example shows it
    assert [x["amount"] for x in body["example"]["rows"]] == [1000000, 1600000, 2625000]
    public = admin_api.get("/payment-plans", params={"project_id": "suraksha-enclave"}).json()
    assert [p["short_code"] for p in public["plans"]] == ["PP1", "PP2", "PP3", "PP4", "PP5"]
    preview = admin_api.post("/payment-plans/preview", json={"plan_code": "PP5", "sale_price": 1000000}).json()
    assert preview["booking_amount"] == 200000


def test_admin_validate_is_a_dry_run(admin_api):
    r = admin_api.post("/admin/payment-plans/validate", json=PP5)
    assert r.status_code == 200 and r.json()["example"]["total"] == 5225000
    assert admin_api.get("/payment-plans/PP5").status_code == 404           # nothing saved


def test_duplicate_code_and_short_code_conflict(admin_api):
    assert admin_api.post("/admin/payment-plans", json=PP5).status_code == 201
    assert admin_api.post("/admin/payment-plans", json=PP5).json()["detail"] == "payment_plan_exists"
    clash = {**PP5, "code": "PP6/FPP/X"}                                     # same short code PP5
    r = admin_api.post("/admin/payment-plans", json=clash)
    assert r.status_code == 409 and r.json()["detail"] == "short_code_in_use"


@pytest.mark.parametrize("change, detail", [
    ({"code": "bad code!"}, "invalid_plan_code"),
    ({"code": "preview"}, "invalid_plan_code"),
    ({"short_code": "P P"}, "invalid_short_code"),
    ({"project_id": "Suraksha Enclave"}, "invalid_project_id"),
    ({"instalments": [{"label": "On Booking", "percent": 50, "due_days": 0},
                      {"label": "Later", "percent": 40, "due_days": 30}]}, "invalid_payment_plan"),
    ({"instalments": [{"label": "Later", "percent": 100, "due_days": 30}]}, "invalid_payment_plan"),
    ({"instalments": [{"label": "On Booking", "percent": 100, "due_trigger": "possession"}]}, "invalid_payment_plan"),
    ({"instalments": [{"label": "On Booking", "percent": 50, "due_days": 0, "charge_head": "PLC"},
                      {"label": "Later", "percent": 50, "due_days": 30, "charge_head": "PLC"}]}, "invalid_payment_plan"),
    ({"instalments": [{"label": "On Booking", "percent": 50, "due_days": 0, "charge_head": "ALL"},
                      {"label": "Later", "percent": 50, "due_days": 30, "charge_head": "CAM"}]}, "invalid_payment_plan"),
    ({"instalments": [{"label": "On Booking", "percent": 33.3333, "due_days": 0},
                      {"label": "Later", "percent": 66.6667, "due_days": 30}]}, "invalid_instalments"),
])
def test_admin_plan_validation_errors(admin_api, change, detail):
    r = admin_api.post("/admin/payment-plans", json={**PP5, **change})
    assert r.status_code == 400 and r.json()["detail"] == detail, r.text


@pytest.mark.parametrize("bad", [
    {"instalments": []},
    {"instalments": [{"label": "x", "percent": 100, "due_days": 0}] * 25},
    {"instalments": [{"label": "On Booking", "percent": 0, "due_days": 0}]},
    {"instalments": [{"label": "On Booking", "percent": 100, "due_days": -1}]},
    {"instalments": [{"label": "On Booking", "percent": 100, "due_days": 0, "charge_head": "GST"}]},
    {"name": "X"},
    {"sort_order": 5000},
])
def test_admin_plan_schema_errors(admin_api, bad):
    assert admin_api.post("/admin/payment-plans", json={**PP5, **bad}).status_code == 422


def test_new_version_changes_instalments_and_keeps_old_bookings(admin_api, plan_service, doc_service):
    admin_api.post("/admin/payment-plans", json=PP5)
    terms = plan_service.booking_terms("PP5", 1000000, 0, 0, 0)            # a customer paid against v1
    v2 = {"code": "PP5/FPP/20-30-50", "instalments": [
        {"label": "On Booking", "percent": 20, "due_days": 0},
        {"label": "Within 90 days of booking", "percent": 80, "due_days": 90, "charge_head": "ALL"}]}
    r = admin_api.post("/admin/payment-plans/versions", json=v2)
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["name"] == PP5["name"]
    assert len(admin_api.get("/payment-plans/PP5").json()["instalments"]) == 2
    booked = doc_service._plan_schedule_from_payment(_payment(terms), {})
    assert booked["payment_plan"]["version"] == 1 and len(booked["rows"]) == 3      # v1 terms kept
    all_versions = [p for p in admin_api.get("/admin/payment-plans").json()["plans"] if p["short_code"] == "PP5"]
    assert [(p["version"], p["is_active"]) for p in all_versions] == [(2, True), (1, False)]
    assert admin_api.post("/admin/payment-plans/versions",
                          json={**v2, "code": "NOPE/1"}).status_code == 404


def test_deactivate_hides_from_customers_and_reactivate(admin_api):
    admin_api.post("/admin/payment-plans", json=PP5)
    r = admin_api.patch("/admin/payment-plans", json={"code": "PP5/FPP/20-30-50", "is_active": False,
                                                      "name": "Old 20-30-50"})
    assert r.status_code == 200 and r.json()["is_active"] is False and r.json()["name"] == "Old 20-30-50"
    assert "PP5" not in [p["short_code"] for p in admin_api.get("/payment-plans").json()["plans"]]
    assert admin_api.post("/payment-plans/preview", json={"plan_code": "PP5", "sale_price": 100000}).status_code == 404
    r = admin_api.patch("/admin/payment-plans", json={"code": "PP5/FPP/20-30-50", "is_active": True})
    assert r.status_code == 200 and r.json()["is_active"] is True
    assert admin_api.patch("/admin/payment-plans", json={"code": "NOPE/1", "name": "x y"}).status_code == 404


def test_plan_management_requires_an_admin(api):
    assert api.get("/admin/payment-plans").status_code == 401
    assert api.post("/admin/payment-plans", json=PP5).status_code == 401
    assert api.patch("/admin/payment-plans", json={"code": "PP1/FPP/10-15-25-25-15-10"}).status_code == 401


def test_normalize_definition_numbers_rows_in_order():
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(PP5)
    assert [r["seq"] for r in plan["instalments"]] == [1, 2, 3]
    assert plan["instalments"][0]["charge_head"] is None and plan["instalments"][1]["charge_head"] == "PLC"

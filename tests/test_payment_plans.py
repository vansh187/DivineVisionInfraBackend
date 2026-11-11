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
    {"instalments": [{"seq": 1, "label": "a", "percent": 100, "due_trigger": "custom"}]},          # custom without event
    {"instalments": [{"seq": 1, "label": "a", "percent": 100, "due_trigger": "slab"}]},            # unknown trigger
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
    ({"instalments": [{"label": "On Booking", "percent": 100, "due_trigger": "custom"}]}, "invalid_instalments"),
    ({"instalments": [{"label": "On Booking", "percent": 100, "due_trigger": "custom", "due_from": "   "}]},
     "invalid_instalments"),
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



# --------------------------------------------------------------------------
# Custom "Counted from" events + column-length safety
# --------------------------------------------------------------------------

PP6 = {
    "code": "PP6/CLP/10-40-50", "short_code": "PP6", "name": "Construction Linked Plan 10:40:50",
    "description": None, "project_id": "suraksha-enclave", "sort_order": 6,
    "instalments": [
        {"label": "Booking Amount", "percent": 10, "due_trigger": "booking", "due_days": 0, "charge_head": None},
        {"label": "On completion of plinth", "percent": 40, "due_trigger": "custom", "due_days": 30,
         "due_from": "Completion of plinth", "charge_head": None},
        {"label": "On Offer of Possession", "percent": 50, "due_trigger": "possession", "due_days": 0, "charge_head": "ALL"},
    ],
}


def test_custom_event_plan_matches_the_frontend_sample(admin_api):
    r = admin_api.post("/admin/payment-plans/validate", json=PP6)
    assert r.status_code == 200, r.text
    body = r.json()
    inst = body["instalments"]
    assert [i["due_from"] for i in inst] == [None, "Completion of plinth", None]
    assert [i["due_text"] for i in inst] == ["On booking", "30 days after Completion of plinth", "On Offer of Possession"]
    assert [x["amount"] for x in body["example"]["rows"]] == [500000, 2000000, 2725000]
    assert body["example"]["rows"][2]["charge_head"] == "PLC+IFMS+CAM" and body["example"]["total"] == 5225000
    assert admin_api.post("/admin/payment-plans", json=PP6).status_code == 201
    public = admin_api.get("/payment-plans/PP6").json()
    assert public["instalments"][1]["due_from"] == "Completion of plinth"


def test_custom_event_without_days_reads_on_event(admin_api):
    plan = {**PP6, "instalments": [PP6["instalments"][0],
                                   {**PP6["instalments"][1], "due_days": 0}, PP6["instalments"][2]]}
    body = admin_api.post("/admin/payment-plans/validate", json=plan).json()
    assert body["instalments"][1]["due_text"] == "On Completion of plinth"


def test_custom_rows_have_no_due_date():
    from DivineService.service_payment_plans import normalize_definition
    s = compute_schedule(normalize_definition(PP6), **DOC_PRICES, booking_date="2026-11-01",
                         possession_date="2028-01-01")
    assert [r["due_date"] for r in s["rows"]] == ["2026-11-01", None, "2028-01-01"]
    assert s["rows"][1]["due_trigger"] == "custom" and s["rows"][1]["due_from"] == "Completion of plinth"


def test_due_from_is_dropped_for_standard_rows():
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition({**PP6, "instalments": [
        {"label": "On Booking", "percent": 100, "due_trigger": "booking", "due_from": "ignored"}]})
    assert plan["instalments"][0]["due_from"] is None


def test_due_from_length_limits(admin_api):
    long_event = {**PP6, "instalments": [PP6["instalments"][0],
                                         {**PP6["instalments"][1], "due_from": "x" * 101}, PP6["instalments"][2]]}
    assert admin_api.post("/admin/payment-plans/validate", json=long_event).status_code == 422
    unknown = {**PP6, "instalments": [{**PP6["instalments"][0], "due_trigger": "slab"}]}
    assert admin_api.post("/admin/payment-plans/validate", json=unknown).status_code == 422


@pytest.mark.parametrize("first", [
    {"label": "Booking Amount", "percent": 25, "due_trigger": "booking", "due_days": 7},
    {"label": "Booking Amount", "percent": 25, "due_trigger": "possession", "due_days": 0},
    {"label": "Booking Amount", "percent": 25, "due_trigger": "custom", "due_days": 5, "due_from": "Agreement signing"},
])
def test_first_instalment_may_have_any_due_and_is_still_paid_at_booking(admin_api, factory, first):
    plan = {**PP6, "code": "PPX/T/25-75", "short_code": "PPX", "instalments": [
        first, {"label": "Balance", "percent": 75, "due_trigger": "booking", "due_days": 60, "charge_head": "ALL"}]}
    r = admin_api.post("/admin/payment-plans/validate", json=plan)
    assert r.status_code == 200, r.text
    from DivineService.service_payment_plans import normalize_definition
    sched = compute_schedule(normalize_definition(plan), 1000000, booking_date="2026-11-01")
    sched["rows"] = [{**x, "status": "paid" if i == 0 else "due"} for i, x in enumerate(sched["rows"])]
    sched["payment_plan"] = sched["plan"]
    rows = _milestones(factory)._rows_from_plan(sched, "doc-f", "C1", "p", "i", "pay-1", "2026-11-01")
    assert rows[0]["status"] == "paid" and rows[0]["paid_payment_id"] == "pay-1"
    assert sched["booking_amount"] == 250000


def _custom_milestones(factory, booking_date):
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition({**PP6, "instalments": [
        {"label": "Booking Amount", "percent": 10, "due_days": 0},
        {"label": "On completion of plinth", "percent": 40, "due_trigger": "custom", "due_days": 30,
         "due_from": "Completion of plinth"},
        {"label": "Within 180 days of booking", "percent": 50, "due_days": 180}]})
    sched = compute_schedule(plan, 1000000, booking_date=booking_date)
    sched["rows"] = [{**x, "status": "paid" if i == 0 else "due"} for i, x in enumerate(sched["rows"])]
    sched["payment_plan"] = sched["plan"]
    svc = _milestones(factory)
    svc._offline_submissions = MagicMock()
    svc._offline_submissions.pending_milestones.return_value = set()
    svc._persistence.create_milestones(
        svc._rows_from_plan(sched, "doc-c", "C1", "suraksha-enclave", "INV-1", "pay-1", booking_date))
    return svc


def test_custom_milestone_stores_event_and_shows_wording(factory):
    svc = _custom_milestones(factory, "2026-11-01")
    stored = svc._persistence.list_for_booking("doc-c")
    assert stored[1].due_from == "Completion of plinth" and stored[1].due_date is None
    enriched = svc.enriched_schedule("C1", "doc-c", rows=stored)
    row = enriched["rows"][1]
    assert row["due_text"] == "30 days after Completion of plinth" and row["status"] == "upcoming"


def test_dated_row_after_an_undated_event_still_gets_next_due_and_can_be_paid(factory):
    booked = (date.today() - timedelta(days=200)).isoformat()      # 180-day row now overdue
    svc = _custom_milestones(factory, booked)
    stored = svc._persistence.list_for_booking("doc-c")
    nd = svc.enriched_schedule("C1", "doc-c", rows=stored)["next_due"]
    assert nd["label"] == "Within 180 days of booking" and nd["days_until_due"] < 0
    svc.ensure_for_customer = lambda *a, **k: stored
    target, code = svc.validate_installment("C1", 3, 500000, inventory_id="INV-1")
    assert code is None and target.milestone_no == 3              # not blocked by the plinth row
    target, code = svc.validate_installment("C1", 2, 400000, inventory_id="INV-1")
    assert code is None                                            # the event row can be paid early too


def test_reminder_goes_to_the_dated_row_not_the_undated_event(factory, monkeypatch):
    svc_m = _custom_milestones(factory, (date.today() - timedelta(days=200)).isoformat())
    from DivineService.service_payment_reminders import serviceReminders
    import DivineService.service_payment_reminders as module
    profile = MagicMock()
    profile.list_booking_applications.return_value = [{"id": "doc-c", "form_data": {"unitNo": "C61"}}]
    customers = MagicMock()
    customers.get_by_id.return_value = SimpleNamespace(email="buyer@gmail.com", first_name="A")
    sent = []
    monkeypatch.setattr(module, "dispatch_payment_reminder_email", lambda *a, **k: sent.append(k) or True)
    rem = serviceReminders(milestone_persistence=svc_m._persistence, customer_persistence=customers,
                           profile_persistence=profile, milestone_service=svc_m, email_service=MagicMock())
    assert rem.run()["sent"] == {"OVERDUE": 1}
    assert sent[0]["milestone_label"] == "Within 180 days of booking" and sent[0]["amount"] == 500000


def test_demand_letter_never_demands_an_undated_event_and_wraps_its_wording():
    from DivineService.service_demand_letter import demand_letter_figures
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(PP6)
    sched = compute_schedule(plan, **DOC_PRICES, booking_date="2026-01-01")
    rows = [{**x, "status": "paid" if i == 0 else "due"} for i, x in enumerate(sched["rows"])]
    form = {"projectId": "suraksha-enclave", "applicantName": "A", "unitNo": "C61", "total_consideration": sched["total"],
            "amount_received": rows[0]["amount"], "payment_schedule": rows, "payment_plan": sched["plan"]}
    fig = demand_letter_figures(form, date(2026, 11, 9))
    assert fig["overdue"] == 0 and fig["demand"] == 0 and fig["awaiting_possession"]
    text_ = " ".join(_letter_text(form).split())
    assert "30 days after Completion of plinth" in text_ and "On Possession" in text_


@pytest.mark.parametrize("code", ["PP1", "PP2", "PP3", "PP4"])
def test_standard_plans_unchanged(code):
    """Regression: booking/possession-only plans give the same numbers and wording as before."""
    s = compute_schedule(PLANS[code], **DOC_PRICES, booking_date="2026-11-01")
    assert all(r["due_from"] is None for r in s["rows"])
    assert all(r["due_text"].startswith(("On booking", "On Offer", "45", "60", "90", "180", "270", "30"))
               for r in s["rows"])
    assert sum(r["amount"] for r in s["rows"]) == 5225000


def _string_limits(model):
    return {c.name: c.type.length for c in model.__table__.columns if getattr(c.type, "length", None)}


@pytest.mark.parametrize("code", ["PP1", "PP2", "PP3", "PP4", "PP6"])
def test_every_value_fits_its_database_column(factory, code):
    """Postgres rejects over-long strings (SQLite doesn't) - check every string
    a plan can produce against the declared column widths."""
    from Divinepersistence.persistence_milestone import PaymentMilestoneModel
    from Divinepersistence.persistence_payment_plan import PaymentPlanInstalmentModel, PaymentPlanModel
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(PP6) if code == "PP6" else PLANS[code]
    plan = {**plan, "instalments": [{**i, "label": (i["label"] + " ") .ljust(150, "x")[:150]} for i in plan["instalments"]]}
    sched = compute_schedule(plan, **DOC_PRICES, booking_date="2026-11-01")
    sched["payment_plan"] = sched["plan"]
    rows = _milestones(factory)._rows_from_plan(sched, "d" * 36, "C12345", "suraksha-enclave", "i" * 36, "p" * 36,
                                                "2026-11-01")
    limits = _string_limits(PaymentMilestoneModel)
    for row in rows:
        for col, limit in limits.items():
            value = row.get(col)
            if isinstance(value, str):
                assert len(value) <= limit, (col, value, limit)
    inst_limits = _string_limits(PaymentPlanInstalmentModel)
    for i in plan["instalments"]:
        for col, limit in inst_limits.items():
            if isinstance(i.get(col), str):
                assert len(i[col]) <= limit, (col, i[col])
    plan_limits = _string_limits(PaymentPlanModel)
    for col, limit in plan_limits.items():
        if isinstance(plan.get(col), str):
            assert len(plan[col]) <= limit, (col, plan[col])


def test_milestone_columns_are_wide_enough_for_combined_charges():
    from Divinepersistence.persistence_milestone import PaymentMilestoneModel
    limits = _string_limits(PaymentMilestoneModel)
    assert limits["charge_head"] >= len("PLC+IFMS+CAM") and limits["label"] >= 150 and limits["due_from"] >= 100


def test_postgres_widening_statements(monkeypatch):
    """The startup migration widens too-narrow Postgres columns and adds due_from."""
    from Divinepersistence import persistence_db as pdb
    assert pdb.MIN_VARCHAR_WIDTHS[("divine_payment_milestones", "charge_head")] == 20
    assert ("due_from", "varchar(100)") in pdb.OFFLINE_IMPORT_COLUMNS["divine_payment_plan_instalments"]
    assert ("due_from", "varchar(100)") in pdb.OFFLINE_IMPORT_COLUMNS["divine_payment_milestones"]


def test_admin_bookings_list_has_an_exact_customer_filter():
    from Divinepersistence.persistence_booking import persistenceBooking
    q = persistenceBooking()._q("list_queue")
    assert ":customer_id IS NULL OR customer_id = :customer_id" in q



# --------------------------------------------------------------------------
# Two-charge combinations in "Add charges" (PLC+IFMS, PLC+CAM, IFMS+CAM)
# --------------------------------------------------------------------------

def _pair_plan(code, row2_head, row3_head, pcts=(10, 40, 50)):
    return {
        "code": code, "short_code": code.split("/")[0], "name": f"Flexible Payment Plan {code}",
        "description": None, "project_id": "suraksha-enclave", "sort_order": 7,
        "instalments": [
            {"label": "Booking Amount", "percent": pcts[0], "due_trigger": "booking", "due_days": 0, "charge_head": None},
            {"label": "Within 45 days of booking", "percent": pcts[1], "due_trigger": "booking", "due_days": 45,
             "charge_head": row2_head},
            {"label": "On Offer of Possession", "percent": pcts[2], "due_trigger": "possession", "due_days": 0,
             "charge_head": row3_head},
        ],
    }


def _example(body):
    return [(r["basic_amount"], r["charge_head"], r["charge_amount"], r["amount"]) for r in body["example"]["rows"]]


def test_charges_in_unit():
    from DivineService.service_payment_plans import charges_in
    assert charges_in(None) == [] and charges_in("") == []
    assert charges_in("PLC") == ["PLC"] and charges_in("CAM") == ["CAM"]
    assert charges_in("PLC+IFMS") == ["PLC", "IFMS"]
    assert charges_in("PLC+CAM") == ["PLC", "CAM"]
    assert charges_in("IFMS+CAM") == ["IFMS", "CAM"]
    assert charges_in("ALL") == ["PLC", "IFMS", "CAM"]
    assert charges_in("CAM+PLC") == [] and charges_in("GST") == []          # not a valid head


def test_sample_e_plc_ifms_created_and_stored(admin_api, factory):
    body = _pair_plan("PP7/FPP/10-40-50", "PLC+IFMS", "CAM")
    body["description"] = "10% on booking, 40% (+PLC, IFMS) within 45 days, 50% (+CAM) on Offer of Possession."
    r = admin_api.post("/admin/payment-plans", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert [i["charge_head"] for i in out["instalments"]] == [None, "PLC+IFMS", "CAM"]
    assert _example(out) == [(500000, None, 0, 500000), (2000000, "PLC+IFMS", 150000, 2150000),
                             (2500000, "CAM", 75000, 2575000)]
    assert out["example"]["total"] == 5225000 and out["example"]["booking_amount"] == 500000
    assert out["valid"] is True and out["is_active"] is True
    with factory() as db:                                 # exactly what was written to the plan table
        stored = db.execute(text(
            "SELECT i.seq, i.charge_head FROM divine_payment_plan_instalments i JOIN divine_payment_plans p "
            "ON p.id = i.plan_id WHERE p.code = 'PP7/FPP/10-40-50' ORDER BY i.seq")).all()
    assert [tuple(x) for x in stored] == [(1, None), (2, "PLC+IFMS"), (3, "CAM")]


def test_sample_f_plc_cam(admin_api):
    out = admin_api.post("/admin/payment-plans/validate", json=_pair_plan("PP8/FPP/F", "PLC+CAM", "IFMS")).json()
    assert _example(out) == [(500000, None, 0, 500000), (2000000, "PLC+CAM", 175000, 2175000),
                             (2500000, "IFMS", 50000, 2550000)]


def test_sample_g_ifms_cam(admin_api):
    out = admin_api.post("/admin/payment-plans/validate", json=_pair_plan("PP9/FPP/G", "IFMS+CAM", "PLC")).json()
    assert _example(out) == [(500000, None, 0, 500000), (2000000, "IFMS+CAM", 125000, 2125000),
                             (2500000, "PLC", 100000, 2600000)]


def test_sample_h_unassigned_third_charge_falls_to_last_row(admin_api):
    body = {**_pair_plan("PP10/FPP/H", None, None), "instalments": [
        {"label": "Booking Amount", "percent": 10, "due_trigger": "booking", "due_days": 0, "charge_head": "PLC+IFMS"},
        {"label": "Balance", "percent": 90, "due_trigger": "booking", "due_days": 60, "charge_head": None}]}
    out = admin_api.post("/admin/payment-plans/validate", json=body).json()
    assert _example(out) == [(500000, "PLC+IFMS", 150000, 650000), (4500000, "CAM", 75000, 4575000)]
    assert out["example"]["booking_amount"] == 650000
    assert [i["charge_head"] for i in out["instalments"]] == ["PLC+IFMS", None]   # stored heads unchanged


@pytest.mark.parametrize("a, b", [
    ("PLC+IFMS", "PLC"), ("PLC+IFMS", "PLC+CAM"), ("IFMS+CAM", "ALL"), ("PLC+CAM", "PLC+CAM"),
    ("PLC", "PLC"), ("ALL", "CAM"), ("IFMS", "PLC+IFMS"),
])
def test_overlapping_charges_are_rejected_and_nothing_is_saved(admin_api, a, b):
    body = _pair_plan("PPZ/FPP/I", a, b)
    r = admin_api.post("/admin/payment-plans", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "invalid_payment_plan", r.text
    assert admin_api.get("/payment-plans/PPZ/FPP/I").status_code == 404


@pytest.mark.parametrize("head", ["CAM+PLC", "IFMS+PLC", "PLC + IFMS", "plc+ifms", "PLC+IFMS+CAM", "PLC,IFMS", "GST"])
def test_wrong_order_spacing_case_or_triple_is_refused_by_the_api(admin_api, head):
    r = admin_api.post("/admin/payment-plans/validate", json=_pair_plan("PPY/FPP/W", head, None))
    assert r.status_code == 422, r.text


def test_service_normalises_case_but_never_order():
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(_pair_plan("PPN/FPP/N", "plc+ifms", "cam"))
    assert [i["charge_head"] for i in plan["instalments"]] == [None, "PLC+IFMS", "CAM"]
    with pytest.raises(ValueError, match="invalid_instalments"):
        normalize_definition(_pair_plan("PPN/FPP/N", "CAM+PLC", None))
    with pytest.raises(ValueError, match="invalid_instalments"):
        normalize_definition(_pair_plan("PPN/FPP/N", "PLC+IFMS+CAM", None))   # all three is stored as ALL


def test_all_three_split_as_a_pair_and_a_single_equals_all():
    """PLC+IFMS on one row + CAM on another = the same money as placing them individually."""
    from DivineService.service_payment_plans import normalize_definition
    pair = compute_schedule(normalize_definition(_pair_plan("PPA/1", "PLC+IFMS", "CAM")), **DOC_PRICES)
    single = {**_pair_plan("PPA/2", None, "CAM"), "instalments": [
        {"label": "Booking Amount", "percent": 10, "due_days": 0},
        {"label": "Within 45 days of booking", "percent": 20, "due_days": 45, "charge_head": "PLC"},
        {"label": "Within 45 days of booking ", "percent": 20, "due_days": 45, "charge_head": "IFMS"},
        {"label": "On Offer of Possession", "percent": 50, "due_trigger": "possession", "charge_head": "CAM"}]}
    split = compute_schedule(normalize_definition(single), **DOC_PRICES)
    assert pair["total"] == split["total"] == 5225000
    assert pair["rows"][1]["amount"] == split["rows"][1]["amount"] + split["rows"][2]["amount"]


@pytest.mark.parametrize("prices", [
    dict(sale_price=5000000, plc=100000, ifms=50000, cam=75000),
    dict(sale_price=2627461, plc=0, ifms=10000, cam=0),          # only one charge present
    dict(sale_price=1000001, plc=0, ifms=0, cam=0),              # no charges at all
    dict(sale_price="25,00,000.50", plc="1,00,000", ifms=0, cam="12,345.4"),
])
@pytest.mark.parametrize("heads", [("PLC+IFMS", "CAM"), ("PLC+CAM", "IFMS"), ("IFMS+CAM", "PLC"),
                                   ("PLC+IFMS", None), (None, "IFMS+CAM")])
def test_pairs_always_add_up_to_the_total(prices, heads):
    from DivineService.service_payment_plans import normalize_definition
    s = compute_schedule(normalize_definition(_pair_plan("PPS/1", *heads)), **prices)
    assert sum(r["amount"] for r in s["rows"]) == s["total"]
    assert sum(r["charge_amount"] for r in s["rows"]) == s["plc"] + s["ifms"] + s["cam"]
    for r in s["rows"]:                                   # a row's head names exactly the non-zero charges on it
        assert (r["charge_head"] is None) == (r["charge_amount"] == 0)


def test_zero_charges_in_a_pair_show_no_head():
    from DivineService.service_payment_plans import normalize_definition
    s = compute_schedule(normalize_definition(_pair_plan("PPS/2", "PLC+IFMS", "CAM")), 1000000, plc=0, ifms=5000, cam=0)
    assert [r["charge_head"] for r in s["rows"]] == [None, "IFMS", None]


def test_stored_plan_with_a_pair_validates_on_load(plan_persistence, plan_service):
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(_pair_plan("PP7/FPP/10-40-50", "PLC+IFMS", "CAM"))
    plan.update({"version": 1, "is_active": True})
    plan_persistence.upsert_plan(plan)
    plan_service.clear_cache()
    listed = {p["short_code"]: p for p in plan_service.list_plans("suraksha-enclave")}
    assert listed["PP7"]["instalments"][1]["charge_head"] == "PLC+IFMS"
    preview = plan_service.preview("PP7", **DOC_PRICES)
    assert [r["amount"] for r in preview["rows"]] == [500000, 2150000, 2575000]


def test_booking_with_a_pair_plan_end_to_end(plan_persistence, plan_service, doc_service, factory):
    """Payment terms -> upload schedule -> milestones in the DB -> demand letter."""
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(_pair_plan("PP7/FPP/10-40-50", "PLC+IFMS", "CAM"))
    plan.update({"version": 1, "is_active": True})
    plan_persistence.upsert_plan(plan)
    plan_service.clear_cache()
    terms = plan_service.booking_terms("PP7", **DOC_PRICES)
    assert terms["booking_amount"] == 500000
    sched = doc_service._plan_schedule_from_payment(_payment(terms, created=datetime(2026, 1, 1, 6, tzinfo=timezone.utc)), {})
    svc = _milestones(factory)
    rows = svc._rows_from_plan(sched, "doc-p", "C1", "suraksha-enclave", "INV-1", "pay-1", sched["booking_date"])
    svc._persistence.create_milestones(rows)
    stored = svc._persistence.list_for_booking("doc-p")
    assert [(r.charge_head, int(r.charge_amount), int(r.amount)) for r in stored] == [
        (None, 0, 500000), ("PLC+IFMS", 150000, 2150000), ("CAM", 75000, 2575000)]
    form = {"projectId": "suraksha-enclave", "applicantName": "A", "unitNo": "C61",
            "total_consideration": sched["total_receivable"], "amount_received": 500000,
            "payment_schedule": sched["rows"], "payment_plan": sched["payment_plan"]}
    letter = " ".join(_letter_text(form, issued=date(2026, 11, 11)).split())
    assert "BSP + PLC + IFMS" in letter and "21,50,000" in letter


@pytest.mark.parametrize("head, expected", [
    (None, "Basic Price"), ("PLC", "BSP + PLC"), ("PLC+IFMS", "BSP + PLC + IFMS"),
    ("PLC+CAM", "BSP + PLC + CAM"), ("IFMS+CAM", "BSP + IFMS + CAM"), ("PLC+IFMS+CAM", "BSP + Charges"),
    ("ALL", "BSP + Charges"),
])
def test_demand_letter_head_text(head, expected):
    from DivineService.service_demand_letter import _head_text
    assert _head_text({"charge_head": head, "charge_amount": 100 if head else 0}) == expected


def test_demand_letter_head_text_fits_its_cell():
    """The longest pair label must fit the 23 mm Head column at the smallest font."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 5.5)
    for label in ("BSP + PLC + IFMS", "BSP + IFMS + CAM", "BSP + PLC + CAM", "BSP + Charges"):
        assert pdf.get_string_width(label) <= 23.0 - 1.5, label


def test_reminder_label_names_the_pair():
    from DivineService.service_payment_reminders import serviceReminders
    enriched = {"rows": [{"id": "m2", "label": "Within 45 days of booking", "charge_head": "PLC+IFMS",
                          "charge_amount": 150000}]}
    assert serviceReminders._milestone_label(enriched, "m2", "x") == "Within 45 days of booking (incl. PLC+IFMS)"


@pytest.mark.parametrize("heads", [("PLC+IFMS", "CAM"), ("PLC+CAM", "IFMS"), ("IFMS+CAM", "PLC"), ("ALL", None)])
def test_pair_values_fit_their_database_columns(factory, heads):
    from Divinepersistence.persistence_milestone import PaymentMilestoneModel
    from Divinepersistence.persistence_payment_plan import PaymentPlanInstalmentModel
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition(_pair_plan("PPL/1", *heads))
    plan_limit = _string_limits(PaymentPlanInstalmentModel)["charge_head"]
    for i in plan["instalments"]:
        assert len(i["charge_head"] or "") <= plan_limit                 # stored plan value
    sched = compute_schedule(plan, **DOC_PRICES)
    sched["payment_plan"] = sched["plan"]
    rows = _milestones(factory)._rows_from_plan(sched, "d" * 36, "C1", "p", "i", "pay", "2026-11-01")
    ms_limit = _string_limits(PaymentMilestoneModel)["charge_head"]
    for r in rows:
        assert len(r["charge_head"] or "") <= ms_limit                   # computed instalment value


@pytest.mark.parametrize("code", ["PP1", "PP2", "PP3", "PP4"])
def test_existing_single_and_all_plans_unchanged_by_pairs(code):
    """Regression: the seeded plans give the same rows as before the pair change."""
    expected = {
        "PP1": [(500000, None), (750000, None), (1350000, "PLC"), (1300000, "IFMS"), (825000, "CAM"), (500000, None)],
        "PP2": [(500000, None), (4725000, "PLC+IFMS+CAM")],
        "PP3": [(2500000, None), (1350000, "PLC"), (800000, "IFMS"), (575000, "CAM")],
        "PP4": [(5225000, "PLC+IFMS+CAM")],
    }[code]
    s = compute_schedule(PLANS[code], **DOC_PRICES)
    assert [(r["amount"], r["charge_head"]) for r in s["rows"]] == expected


@pytest.mark.parametrize("form_code", ["PP1/FPP/10-15-25-25-15-10", "PP1", "pp1", " PP1 "])
def test_upload_accepts_the_full_or_short_plan_code(doc_service, plan_service, form_code):
    """Integration bug found end-to-end: the website may repeat the plan as its short code."""
    plan = doc_service._plan_schedule_from_payment(_payment(_terms(plan_service)), {"payment_plan_code": form_code})
    assert plan["payment_plan"]["code"] == "PP1/FPP/10-15-25-25-15-10"


@pytest.mark.parametrize("form_code", ["PP2", "PP1/FPP/OTHER", "PP"])
def test_upload_rejects_a_different_plan_code(doc_service, plan_service, form_code):
    with pytest.raises(ValueError, match="plan_mismatch"):
        doc_service._plan_schedule_from_payment(_payment(_terms(plan_service)), {"payment_plan_code": form_code})

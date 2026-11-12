"""Payment plans against a REAL Postgres - catches what SQLite can't (varchar
lengths, jsonb casts, ON CONFLICT ... DO UPDATE ... WHERE, IS DISTINCT FROM,
boolean/date types, unique-constraint errors, ALTER COLUMN migrations).

Opt-in and local-only: set PAYMENT_PLAN_PG_TEST_URL to an EMPTY throwaway
database on localhost, e.g.

    PAYMENT_PLAN_PG_TEST_URL=postgresql+psycopg2://postgres@localhost:55432/plan_it pytest tests/test_payment_plans_postgres.py

Skipped otherwise. Refuses any non-localhost host so it can never touch prod.
The schema is dropped and recreated at the start of the run.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

PG_URL = os.getenv("PAYMENT_PLAN_PG_TEST_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENT_PLAN_PG_TEST_URL to run against local Postgres")

if PG_URL:
    _host = (urlparse(PG_URL.replace("+psycopg2", "").replace("+psycopg", "")).hostname or "").lower()
    if _host not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(f"PAYMENT_PLAN_PG_TEST_URL must point at localhost, not {_host!r}")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import Divinepersistence  # noqa: E402,F401
from Divinepersistence.persistence_db import Base, ensure_offline_import_columns  # noqa: E402

DOC_PRICES = dict(sale_price=5000000, plc=100000, ifms=50000, cam=75000)


@pytest.fixture(scope="module")
def pg():
    eng = create_engine(PG_URL, future=True, pool_pre_ping=True)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        ensure_offline_import_columns(c)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def factory(pg):
    return sessionmaker(bind=pg, autoflush=False, autocommit=False)


@pytest.fixture(scope="module")
def plans(factory, pg):
    from Divinepersistence.persistence_milestone import persistenceMilestone
    from Divinepersistence.persistence_payment_plan import persistencePaymentPlan
    from DivineService.service_payment_plans import servicePaymentPlans, sync_plans
    persistence = persistencePaymentPlan(session_factory=factory)
    sync_plans(persistence)
    return servicePaymentPlans(persistence, persistenceMilestone(session_factory=factory))


def _cols(pg, table):
    with pg.connect() as c:
        return {r[0]: r[1] for r in c.execute(text(
            "SELECT column_name, character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"), {"t": table})}


# --------------------------------------------------------------------------
# Schema + migration path (what prod went through)
# --------------------------------------------------------------------------

def test_fresh_schema_has_the_right_widths(pg):
    ms = _cols(pg, "divine_payment_milestones")
    assert ms["charge_head"] == 20 and ms["label"] == 150 and ms["due_from"] == 100 and ms["due_trigger"] == 12
    inst = _cols(pg, "divine_payment_plan_instalments")
    assert inst["charge_head"] == 10 and inst["due_from"] == 100 and inst["label"] == 150


def test_migration_upgrades_an_old_prod_shaped_schema(pg):
    """Recreate the pre-patch prod shape in its own database and run the startup migration on it."""
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS plan_it_legacy"))
        c.execute(text("CREATE DATABASE plan_it_legacy"))
    admin.dispose()
    legacy_url = PG_URL.rsplit("/", 1)[0] + "/plan_it_legacy"
    eng = create_engine(legacy_url)
    try:
        with eng.begin() as c:
            c.execute(text(
                "CREATE TABLE divine_payment_milestones (id varchar(48) PRIMARY KEY, booking_id varchar(36) NOT NULL, "
                "customer_id varchar(6) NOT NULL, label varchar(120), amount numeric(14,2) NOT NULL DEFAULT 0, "
                "status varchar(12) NOT NULL DEFAULT 'upcoming', charge_head varchar(10), due_trigger varchar(12))"))
            c.execute(text(
                "CREATE TABLE divine_payment_plan_instalments (id varchar(36) PRIMARY KEY, plan_id varchar(36) NOT NULL, "
                "seq integer NOT NULL, label varchar(150) NOT NULL, percent numeric(6,3) NOT NULL, "
                "due_trigger varchar(12) NOT NULL DEFAULT 'booking', due_days integer, charge_head varchar(10))"))
            c.execute(text("INSERT INTO divine_payment_milestones (id, booking_id, customer_id, label, amount, charge_head) "
                           "VALUES ('m1', 'd1', 'C00001', 'On Booking', 1000, 'PLC')"))
        with eng.begin() as c:
            changed = ensure_offline_import_columns(c)
        assert "divine_payment_milestones.charge_head varchar(10->20)" in changed
        assert "divine_payment_milestones.label varchar(120->150)" in changed
        assert "divine_payment_milestones.due_from" in changed and "divine_payment_plan_instalments.due_from" in changed
        with eng.begin() as c:
            assert ensure_offline_import_columns(c) == []            # idempotent
            row = c.execute(text("SELECT label, charge_head FROM divine_payment_milestones WHERE id = 'm1'")).one()
            assert tuple(row) == ("On Booking", "PLC")              # existing data untouched
            c.execute(text("INSERT INTO divine_payment_milestones (id, booking_id, customer_id, label, amount, "
                           "charge_head, due_from) VALUES ('m2', 'd1', 'C00001', :l, 1, 'PLC+IFMS+CAM', 'Plinth')"),
                      {"l": "x" * 150})
    finally:
        eng.dispose()


def test_old_schema_would_have_rejected_the_combined_charge(pg):
    """Proves the bug the widening fixed: varchar(10) refuses 'PLC+IFMS+CAM' on Postgres."""
    from sqlalchemy.exc import DataError
    with pg.connect() as c:
        tx = c.begin()
        c.execute(text("CREATE TEMP TABLE narrow (charge_head varchar(10))"))
        with pytest.raises(DataError):
            c.execute(text("INSERT INTO narrow VALUES ('PLC+IFMS+CAM')"))
        tx.rollback()


# --------------------------------------------------------------------------
# Plans: seed, admin create (pairs, custom, ALL), versions, on/off
# --------------------------------------------------------------------------

def _plan(code, short, instalments, project="pg-test"):
    return {"code": code, "short_code": short, "name": f"Plan {short}", "description": "d" * 1000,
            "project_id": project, "sort_order": 1000, "instalments": instalments}


LONG = "L" * 150


def test_seeded_plans_load_on_postgres(plans):
    assert [p["short_code"] for p in plans.list_plans("suraksha-enclave")] == ["PP1", "PP2", "PP3", "PP4"]


def test_admin_plans_with_every_charge_shape_save_on_postgres(plans):
    shapes = [None, "PLC", "IFMS", "CAM", "PLC+IFMS", "PLC+CAM", "IFMS+CAM", "ALL"]
    for n, head in enumerate(shapes):
        rows = [{"label": LONG, "percent": 50, "due_trigger": "custom", "due_days": 3650, "due_from": "E" * 100,
                 "charge_head": head},
                {"label": "Balance", "percent": 50, "due_trigger": "possession", "due_days": 0}]
        out = plans.create_plan(_plan(f"PG/{n}/" + "x" * 70, f"PG{n}", rows), "DV0001")
        assert out["valid"] and out["instalments"][0]["charge_head"] == head
        assert out["instalments"][0]["due_text"] == "3650 days after " + "E" * 100
    assert len([p for p in plans.list_plans("pg-test")]) == len(shapes)


def test_duplicate_plan_and_new_version_on_postgres(plans):
    rows = [{"label": "On Booking", "percent": 100, "due_trigger": "booking", "due_days": 0}]
    plans.create_plan(_plan("PGV/1", "PGV", rows), "DV0001")
    with pytest.raises(ValueError, match="payment_plan_exists"):
        plans.create_plan(_plan("PGV/1", "PGV", rows), "DV0001")
    v2 = plans.new_version("PGV/1", {"instalments": [
        {"label": "On Booking", "percent": 40, "due_days": 0},
        {"label": "Later", "percent": 60, "due_days": 30, "charge_head": "PLC+CAM"}]}, "DV0001")
    assert v2["version"] == 2 and v2["is_active"]
    off = plans.update_plan("PGV/1", {"is_active": False, "name": "Retired"}, "DV0001")
    assert off["is_active"] is False and off["name"] == "Retired"
    plans.clear_cache()
    assert "PGV" not in [p["short_code"] for p in plans.list_plans("pg-test")]
    assert plans._persistence.get_by_code("PGV/1", version=1)["instalments"][0]["percent"] == 100


def test_possession_date_upsert_on_postgres(plans):
    out1 = plans.set_possession_date("pg-test", date(2028, 1, 1), "admin:DV0001")
    out2 = plans.set_possession_date("pg-test", "2028-02-01", "admin:DV0002")
    assert out1["possession_offer_date"] == "2028-01-01" and out2["possession_offer_date"] == "2028-02-01"
    assert plans.possession_date("pg-test") == date(2028, 2, 1)


# --------------------------------------------------------------------------
# Booking flow: payment (jsonb notes) -> upload schedule -> milestones ->
# instalments -> reminders -> possession -> demand letter
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def booked(factory, plans, pg):
    import DivineService.service_payment_plans as pp_module
    from Divinepersistence.persistence_payment import persistencePayment
    from DivineService.service_document import serviceDocument
    from DivineService.service_payment_plans import normalize_definition
    plan = normalize_definition({
        "code": "PGF/CLP/10-30-30-30", "short_code": "PGF", "name": "PG flow", "project_id": "pg-flow",
        "instalments": [
            {"label": "Booking Amount", "percent": 10, "due_days": 0, "charge_head": "PLC+IFMS"},
            {"label": LONG, "percent": 30, "due_trigger": "custom", "due_days": 30, "due_from": "Completion of plinth"},
            {"label": "Within 180 days of booking", "percent": 30, "due_days": 180},
            {"label": "On Offer of Possession", "percent": 30, "due_trigger": "possession", "charge_head": "CAM"}]})
    plan.update({"version": 1, "is_active": True})
    plans._persistence.upsert_plan(plan)
    plans.clear_cache()
    terms = plans.booking_terms("PGF", **{k: v for k, v in DOC_PRICES.items()}, project_id="pg-flow")
    payments = persistencePayment(session_factory=factory)
    created = datetime.now(timezone.utc) - timedelta(days=200)
    payment = payments.create_payment(
        id="pay-pg-1", owner_id="C77777", owner_role="customer", amount=terms["booking_amount"], currency="INR",
        status="paid", method="cash", notes={"payment_plan": {**terms, "project_id": "pg-flow"}},
        purpose="plot_booking", inventory_id="INV-PG")
    stored = payments.get_by_id(payment.id)
    stored.created_date = created                         # booked 200 days ago
    doc_service = serviceDocument(MagicMock(), payments)
    original = pp_module.servicePaymentPlans
    pp_module.servicePaymentPlans = lambda *a, **k: plans
    try:
        sched = doc_service._plan_schedule_from_payment(stored, {"total_amount": terms["total_amount"]})
    finally:
        pp_module.servicePaymentPlans = original
    from Divinepersistence.persistence_milestone import persistenceMilestone
    from DivineService.service_milestones import serviceMilestones
    svc = serviceMilestones(milestone_persistence=persistenceMilestone(session_factory=factory),
                            profile_persistence=MagicMock(), payment_persistence=payments)
    svc._offline_submissions = MagicMock()
    svc._offline_submissions.pending_milestones.return_value = set()
    rows = svc._rows_from_plan(sched, "doc-pg-1", "C77777", "pg-flow", "INV-PG", "pay-pg-1", sched["booking_date"])
    svc._persistence.create_milestones(rows)
    return SimpleNamespace(svc=svc, sched=sched, terms=terms, payments=payments)


def test_payment_notes_roundtrip_as_jsonb(booked):
    stored = booked.payments.get_by_id("pay-pg-1")
    notes = stored.notes if isinstance(stored.notes, dict) else json.loads(stored.notes)
    assert notes["payment_plan"]["payment_plan_code"] == "PGF/CLP/10-30-30-30"
    assert notes["payment_plan"]["booking_amount"] == booked.terms["booking_amount"] == 650000


def test_milestones_persist_every_new_column_on_postgres(booked):
    stored = booked.svc._persistence.list_for_booking("doc-pg-1")
    assert [(r.charge_head, int(r.charge_amount), r.due_trigger, r.due_from) for r in stored] == [
        ("PLC+IFMS", 150000, "booking", None),
        (None, 0, "custom", "Completion of plinth"),
        (None, 0, "booking", None),
        ("CAM", 75000, "possession", None)]
    assert stored[1].label == LONG and stored[1].due_date is None and stored[3].due_date is None
    assert stored[0].status == "paid" and stored[0].paid_payment_id == "pay-pg-1"
    assert sum(int(r.amount) for r in stored) == 5225000


def test_schedule_next_due_and_payment_order_on_postgres(booked):
    stored = booked.svc._persistence.list_for_booking("doc-pg-1")
    enriched = booked.svc.enriched_schedule("C77777", "doc-pg-1", rows=stored)
    assert enriched["rows"][1]["due_text"] == "30 days after Completion of plinth"
    assert enriched["next_due"]["label"] == "Within 180 days of booking"     # dated row, not the event
    assert enriched["next_due"]["days_until_due"] < 0
    booked.svc.ensure_for_customer = lambda *a, **k: booked.svc._persistence.list_for_booking("doc-pg-1")
    target, code = booked.svc.validate_installment("C77777", 3, 1500000, inventory_id="INV-PG")
    assert code is None
    assert booked.svc.mark_paid(target.id, "pay-pg-2") is not None
    assert booked.svc._persistence.list_for_booking("doc-pg-1")[2].status == "paid"


def test_reminder_dedupe_upsert_on_postgres(booked, factory):
    """ON CONFLICT ... DO UPDATE ... WHERE ... IS DISTINCT FROM 'sent' - Postgres syntax."""
    m = booked.svc._persistence
    args = dict(customer_id="C77777", booking_id="doc-pg-1", milestone_id="doc-pg-1-m3", kind="OVERDUE",
                week_key="2026W46", amount=1500000, due_date=date(2026, 5, 1), email_to="b@gmail.com")
    m.record_reminder(id="r1", delivery_status="failed", **args)
    assert m.reminder_sent("doc-pg-1-m3", "OVERDUE", "2026W46") is False      # failed -> retried
    m.record_reminder(id="r2", delivery_status="sent", **args)
    assert m.reminder_sent("doc-pg-1-m3", "OVERDUE", "2026W46") is True
    m.record_reminder(id="r3", delivery_status="failed", **args)              # never downgrades a sent row
    assert m.reminder_sent("doc-pg-1-m3", "OVERDUE", "2026W46") is True
    with factory() as db:
        assert db.execute(text("SELECT count(*) FROM divine_payment_reminders WHERE milestone_id='doc-pg-1-m3'")).scalar() == 1


def test_possession_date_dates_the_rows_on_postgres(booked, plans):
    out = plans.set_possession_date("pg-flow", date(2028, 3, 1), "admin:DV0001")
    assert out["instalments_dated"] == 1
    stored = booked.svc._persistence.list_for_booking("doc-pg-1")
    assert str(stored[3].due_date) == "2028-03-01" and stored[1].due_date is None   # custom stays undated


def test_demand_letter_from_postgres_rows(booked):
    import io
    from pypdf import PdfReader
    from DivineService.service_demand_letter import demand_letter_figures, generate_demand_letter_pdf
    stored = booked.svc._persistence.list_for_booking("doc-pg-1")
    rows = [{"label": r.label, "due_date": r.due_date.isoformat() if r.due_date else None, "amount": int(r.amount),
             "status": r.status, "basic_amount": int(r.basic_amount), "charge_head": r.charge_head,
             "charge_amount": int(r.charge_amount), "due_trigger": r.due_trigger, "due_from": r.due_from,
             "due_days": r.due_days} for r in stored]
    form = {"projectId": "suraksha-enclave", "applicantName": "A", "unitNo": "C61", "total_consideration": 5225000,
            "amount_received": 650000 + 1500000, "payment_schedule": rows,
            "payment_plan": booked.sched["payment_plan"]}
    fig = demand_letter_figures(form, date.today())
    assert fig["overdue"] == 0                     # plinth undated; 180-day row paid; possession in 2028
    text_ = " ".join("".join(p.extract_text() for p in PdfReader(io.BytesIO(
        generate_demand_letter_pdf(form, "C77777"))).pages).split())
    assert "30 days after Completion of plinth" in text_ and "BSP + CAM" in text_


# --------------------------------------------------------------------------
# Admin bookings list: exact customer filter
# --------------------------------------------------------------------------

def test_booking_queue_customer_filter_on_postgres(factory):
    from Divinepersistence.persistence_booking import persistenceBooking
    bookings = persistenceBooking(session_factory=factory)
    for pid, cid in (("p-a", "C10001"), ("p-b", "C10002"), ("p-c", "C100011"[:6])):
        bookings.create_booking(payment_id=pid, inventory_id=f"inv-{pid}", customer_id=cid,
                                project_name="Suraksha Enclave", unit_number="C1")
    rows = bookings.list_queue(customer_id="C10001")
    assert {r.customer_id for r in rows} == {"C10001"} and len(rows) == 2
    assert bookings.count_queue(customer_id="C10002") == 1
    assert bookings.count_queue(search="C1000") == 3


# --------------------------------------------------------------------------
# Offline import loaders (jsonb CAST path, history, rollback) on Postgres
# --------------------------------------------------------------------------

def test_offline_loaders_on_postgres(pg):
    from DivineService.service_offline_import import InventorySheetSync, OfflineBuyerImport
    header = ["Plot No.", "Width(mtr.)", "Length(mtr.)", "Area in sq mtrs", "Area in sq yard", "Status", "Project "]
    sheet = [header, ["Z1", "7.45", "13.404", "99.86", "119.43", "Sold", "PG Estate"],
             ["Z2", "7.45", "13.404", "99.86", "119.43", "Unsold", "PG Estate"]]
    sync = InventorySheetSync(pg)
    rep = sync.run(sheet, "PG Estate", "Sonipat", apply=True)
    assert not rep.errors and rep.counts_after == {"sold": 1, "available": 1}
    buyers = [["Plot No.", "Applicant", "1 Co_Applicant", "2 Co_applicant", "Contact No.", "Alternative Number",
               "Email Id", "Co_applicant Email", "Adders", "Address_1", "Plot_Address "],
              ["Z1", "Mr. " + "N" * 200, "", "", "9.876543210E9", "", "pg.buyer@gmail.com", "", "R/o-Somewhere", "",
               "Plot No -Z1"]]
    imp = OfflineBuyerImport(pg, password_hasher=lambda p: "h:" + p[:4])
    rep_b = imp.run(buyers, "PG Estate", apply=True)
    assert not rep_b.errors
    with pg.connect() as c:
        form = c.execute(text("SELECT form_data FROM divine_documents WHERE status = 'imported'")).scalar()
        first = c.execute(text("SELECT first_name FROM divine_customer_users WHERE email = 'pg.buyer@gmail.com'")).scalar()
    assert form["unitNo"] == "Z1" and form["finance_status"] == "pending"     # jsonb returned as dict
    assert len(first) == 150                                                  # long name cut to the column
    imp.rollback(rep_b.batch_id)
    sync.rollback(rep.batch_id)
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM divine_project_inventory WHERE project_name='PG Estate'")).scalar() == 0


def test_plots_count_reads_jsonb_notes_on_postgres(plans, factory, pg):
    """The Postgres JSON query behind plots_count: real jsonb notes written by the
    payment code, plus odd shapes (scalar, array, nested string) that must be
    skipped, not crash the query."""
    from Divinepersistence.persistence_payment import persistencePayment
    from DivineService.service_payment_plans import normalize_definition
    code = "PGC/CNT/100"
    plan = normalize_definition({"code": code, "short_code": "PGC", "name": "PG count", "project_id": "pg-count",
                                 "instalments": [{"label": "On Booking", "percent": 100, "due_days": 0}]})
    plan.update({"version": 1, "is_active": True})
    plans._persistence.upsert_plan(plan)
    payments = persistencePayment(session_factory=factory)

    def book(i, notes=None, raw=None, status="booked"):
        pid = f"pay-cnt-{i}"
        payments.create_payment(id=pid, owner_id="C66666", owner_role="customer", amount=1, currency="INR",
                                status="paid", method="cash", notes=notes, purpose="plot_booking",
                                inventory_id=f"INV-CNT-{i}")
        with pg.begin() as c:
            if raw is not None:
                c.execute(text("UPDATE divine_payments SET notes = CAST(:n AS jsonb) WHERE id = :id"),
                          {"n": raw, "id": pid})
            c.execute(text("INSERT INTO divine_bookings(id, payment_id, inventory_id, customer_id, status, kyc_status, "
                           "version) VALUES (:b, :p, :inv, 'C66666', :s, 'pending', 1)"),
                      {"b": f"bk-cnt-{i}", "p": pid, "inv": f"INV-CNT-{i}", "s": status})

    book(1, {"payment_plan": {"payment_plan_code": code, "payment_plan_version": 1}})
    book(2, {"payment_plan": {"payment_plan_code": code, "payment_plan_version": "1"}}, status="pending_kyc_review")
    book(3, {"payment_plan": {"payment_plan_code": code}})                     # no version -> active v1
    book(4, {"payment_plan": {"payment_plan_code": code, "payment_plan_version": 1}}, status="cancelled")
    book(5, raw='"just a string"')
    book(6, raw="[1, 2]")
    book(7, raw='{"payment_plan": "PGC"}')
    book(8, {})
    assert plans._persistence.count_plots_by_plan([code]) == {(code, 1): 3}
    listed = {(p["code"], p["version"]): p["plots_count"] for p in plans.admin_list("pg-count")}
    assert listed == {(code, 1): 3}

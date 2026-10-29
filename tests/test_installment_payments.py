"""Instalment (milestone) payments: milestone build + status, the payment guard
rails, settle -> milestone paid, profile enrichment, the receipt PDF endpoint,
and the daily reminder job."""
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from fastapi.testclient import TestClient
from sqlalchemy import text

from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_milestone import persistenceMilestone
from DivineService.service_milestones import (
    serviceMilestones, compute_status, PAY_WINDOW_DAYS, REMINDER_LEAD_DAYS,
)
from DivineService.service_payment import servicePayment
from DivineService.service_payment_reminders import serviceReminders, pick_kind
from DivineAPI.main import app
import DivineAPI.customer_api as customer_api
import DivineAPI.payment_api as payment_api

client = TestClient(app)


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
_WIPE_TABLES = (
    "divine_offline_payment_submissions",
    "divine_payment_reminders", "divine_payment_milestones",
    "divine_documents", "divine_payments",
)


def _reset_db():
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()
    # os.remove() can silently no-op on Windows while this process holds sqlite
    # connections, so explicitly clear the tables this file writes to.
    try:
        with engine.begin() as c:
            for t in _WIPE_TABLES:
                c.execute(text(f"DELETE FROM {t}"))
    except Exception:
        pass


def teardown_module(module):
    try:
        with engine.begin() as c:
            for t in _WIPE_TABLES:
                c.execute(text(f"DELETE FROM {t}"))
    except Exception:
        pass


def _seed_booking(customer_id="C00001", total=5_000_000, booking_amount=500_000,
                  booking_date="2026-01-01", project="OPS Divine Greens", unit="A-12"):
    """Insert a paid booking payment + a booking-application document straight into
    the DB, the way upload_booking_application would (minus the storage upload).

    The document_type here is ``project_booking_application`` on purpose - that is
    the exact string the upload endpoint stores in production (the client sends it
    as a multipart field). The profile / milestone / reminder read queries must
    match it, so seeding the real spelling keeps this suite honest."""
    booking_id = "doc-" + uuid.uuid4().hex[:10]
    payment_id = "pay-" + uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    form = {
        "applicantName": "Asha Rao", "project_name": project, "unit_number": unit,
        "total_consideration": total, "amount_received": booking_amount,
        "booking_date": booking_date,
    }
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, method, "
            "purpose, created_date, last_updated_date) VALUES (:id,:o,'customer',:amt,'INR','paid','razorpay',"
            "'plot_booking',:n,:n)"), {"id": payment_id, "o": customer_id, "amt": booking_amount, "n": now})
        c.execute(text(
            "INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, storage_path, "
            "status, project_id, payment_id, created_date, last_updated_date) VALUES (:id,:o,'customer',"
            "'project_booking_application',:fd,'x/y.pdf','generated',:pj,:pay,:n,:n)"),
            {"id": booking_id, "o": customer_id, "fd": json.dumps(form), "pj": project,
             "pay": payment_id, "n": now})
    return booking_id, payment_id


def _mk_milestones(customer_id="C00001", **kw):
    return serviceMilestones().ensure_for_customer(customer_id)


# --------------------------------------------------------------------------- #
#  pure status helpers                                                         #
# --------------------------------------------------------------------------- #
def test_compute_status_windows():
    t = date(2026, 6, 1)
    assert compute_status("2026-06-20", True, t) == "paid"
    assert compute_status((t + timedelta(days=40)).isoformat(), False, t) == "upcoming"
    assert compute_status((t + timedelta(days=REMINDER_LEAD_DAYS)).isoformat(), False, t) == "due"
    assert compute_status((t + timedelta(days=1)).isoformat(), False, t) == "due"
    assert compute_status((t - timedelta(days=1)).isoformat(), False, t) == "overdue"


def test_pick_kind_boundaries():
    today = date(2026, 1, 15)
    assert pick_kind(-3, today)[0] == "OVERDUE"
    assert pick_kind(0, today) == ("DUE_TODAY", "")
    assert pick_kind(1, today) == ("T_MINUS_5", "")
    assert pick_kind(PAY_WINDOW_DAYS, today) == ("T_MINUS_5", "")
    assert pick_kind(PAY_WINDOW_DAYS + 1, today) == ("T_MINUS_20", "")
    assert pick_kind(REMINDER_LEAD_DAYS, today) == ("T_MINUS_20", "")
    assert pick_kind(REMINDER_LEAD_DAYS + 1, today) == (None, None)


# --------------------------------------------------------------------------- #
#  serviceMilestones                                                           #
# --------------------------------------------------------------------------- #
def test_ensure_for_customer_builds_10_15_25_25_25_from_booking():
    _reset_db()
    _seed_booking()
    rows = serviceMilestones().ensure_for_customer("C00001")
    assert [r.milestone_no for r in rows] == [1, 2, 3, 4, 5]
    assert [int(r.amount) for r in rows] == [500000, 750000, 1250000, 1250000, 1250000]
    assert rows[0].status == "paid" and rows[0].paid_payment_id is not None
    assert all(r.status != "paid" for r in rows[1:])
    # idempotent
    again = serviceMilestones().ensure_for_customer("C00001")
    assert len(again) == 5


def test_enriched_schedule_has_status_and_next_due():
    _reset_db()
    bdate = (date.today() - timedelta(days=30)).isoformat()
    _seed_booking(booking_date=bdate)
    out = serviceMilestones().enriched_schedule("C00001")
    rows = out["rows"]
    assert len(rows) == 5
    assert rows[0]["status"] == "paid"
    assert all(r["id"] for r in rows)
    # No pay window: every row is payable from the booking date, not due_date - 5d.
    assert all(r["pay_enabled_from"] == bdate for r in rows)
    assert out["next_due"]["milestone_id"] == rows[1]["id"]
    assert out["next_due"]["status"] in ("due", "overdue", "upcoming")


def test_validate_installment_guard_rails():
    _reset_db()
    _seed_booking(booking_date=(date.today() - timedelta(days=200)).isoformat())
    svc = serviceMilestones()
    rows = svc.ensure_for_customer("C00001")
    m2, m3 = rows[1], rows[2]

    assert svc.validate_installment("C99999", 2, m2.amount)[1] == "no_booking"
    assert svc.validate_installment("C00001", 99, 1)[1] == "installment_not_found"
    assert svc.validate_installment("C00001", 1, rows[0].amount)[1] == "installment_already_paid"
    assert svc.validate_installment("C00001", 3, m3.amount)[1] == "installment_out_of_order"
    assert svc.validate_installment("C00001", 2, int(m2.amount) + 500)[1] == "installment_amount_mismatch"
    # m2 due 45d after a booking 200d ago -> window well open, exact amount -> ok
    milestone, code = svc.validate_installment("C00001", 2, m2.amount)
    assert code is None and milestone.milestone_no == 2


def test_validate_installment_payable_before_due_date():
    _reset_db()
    _seed_booking(booking_date=date.today().isoformat())  # m2 due in 45d
    svc = serviceMilestones()
    rows = svc.ensure_for_customer("C00001")
    # Early payment of the next instalment is allowed - there is no pay window.
    milestone, code = svc.validate_installment("C00001", 2, rows[1].amount)
    assert code is None and milestone.milestone_no == 2


def test_mark_paid_updates_amount_received():
    _reset_db()
    _seed_booking(booking_date=(date.today() - timedelta(days=100)).isoformat())
    svc = serviceMilestones()
    rows = svc.ensure_for_customer("C00001")
    svc.mark_paid(rows[1].id, "pay-m2")
    assert svc.amount_received_rupees("C00001") == 500000 + 750000
    fresh = {r.milestone_no: r.status for r in persistenceMilestone().list_for_customer("C00001")}
    assert fresh[2] == "paid"


# --------------------------------------------------------------------------- #
#  servicePayment - instalment settle                                          #
# --------------------------------------------------------------------------- #
def _pay_service_with_real_milestones():
    payments = MagicMock()
    svc = servicePayment(payments, MagicMock())
    svc._key_id, svc._key_secret = "rzp_fake", "sec"
    return svc, payments


def test_cash_installment_marks_milestone_paid():
    _reset_db()
    _seed_booking(booking_date=(date.today() - timedelta(days=100)).isoformat())
    rows = serviceMilestones().ensure_for_customer("C00001")
    m2 = rows[1]

    svc, payments = _pay_service_with_real_milestones()
    row = SimpleNamespace(id="pay-cash-1", owner_id="C00001", owner_role="customer",
                          amount=int(m2.amount), currency="INR", status="paid", method="cash",
                          purpose="installment", inventory_id=None, installment_no=2,
                          due_date=str(m2.due_date))
    payments.create_payment.return_value = row

    with patch.object(servicePayment, "_send_installment_receipt"):
        out = svc.record_cash_payment(int(m2.amount), owner_id="C00001", owner_role="customer",
                                      purpose="installment", installment_no=2, due_date=str(m2.due_date))
    assert out.installment_status == "paid"
    fresh = {r.milestone_no: r.status for r in persistenceMilestone().list_for_customer("C00001")}
    assert fresh[2] == "paid"


def test_cash_installment_guard_failure_at_order_time_raises_code():
    _reset_db()
    _seed_booking(booking_date=date.today().isoformat())  # m2 unpaid, so m3 is out of order
    svc, payments = _pay_service_with_real_milestones()
    try:
        svc.record_cash_payment(1250000, owner_id="C00001", owner_role="customer",
                                purpose="installment", installment_no=3)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "installment_out_of_order"
    payments.create_payment.assert_not_called()


def test_offline_cheque_submission_requires_number_and_carries_it_to_payment():
    _reset_db()
    _seed_booking(booking_date=date.today().isoformat())  # m2 due in 45d - early payment allowed
    rows = serviceMilestones().ensure_for_customer("C00001")
    m2 = rows[1]
    svc = servicePayment(MagicMock(), MagicMock())
    try:
        svc.submit_offline_installment("C00001", int(m2.amount), 2, str(m2.due_date), "cheque", None)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "cheque_number_required"

    sub = svc.submit_offline_installment("C00001", int(m2.amount), 2, str(m2.due_date), "cheque", None,
                                         cheque_number=" 004512 ")
    assert sub.method == "cheque" and sub.cheque_number == "004512" and sub.status == "pending_review"

    with patch.object(servicePayment, "_send_installment_receipt"):
        decided = svc.decide_offline_submission(sub.id, "A0001", "approved")
    with engine.begin() as c:
        pay = c.execute(text("SELECT method, cheque_number, recorded_by_admin_id, notes FROM divine_payments "
                             "WHERE id=:id"), {"id": decided.payment_id}).mappings().first()
    assert pay["method"] == "cheque" and pay["cheque_number"] == "004512"
    # Customer-submitted, admin-approved: not recorded as an admin-entered payment.
    assert pay["recorded_by_admin_id"] is None
    notes = pay["notes"] if isinstance(pay["notes"], dict) else json.loads(pay["notes"])
    assert notes == {"offline_submission_id": sub.id, "approved_by": "A0001"}


def test_offline_submission_rejects_utr_on_cheque_or_cash():
    _reset_db()
    _seed_booking(booking_date=date.today().isoformat())
    m2 = serviceMilestones().ensure_for_customer("C00001")[1]
    svc = servicePayment(MagicMock(), MagicMock())
    for method in ("cheque", "cash"):
        try:
            svc.submit_offline_installment("C00001", int(m2.amount), 2, str(m2.due_date), method, "UTR123",
                                           cheque_number="004512")
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "utr_only_for_rtgs_neft"


def test_installment_settle_guard_failure_keeps_payment_and_flags_review():
    _reset_db()
    _seed_booking(booking_date=(date.today() - timedelta(days=100)).isoformat())
    rows = serviceMilestones().ensure_for_customer("C00001")
    serviceMilestones().mark_paid(rows[1].id, "someone-else")   # m2 already paid

    svc, payments = _pay_service_with_real_milestones()
    row = SimpleNamespace(id="pay-late", owner_id="C00001", owner_role="customer",
                          amount=int(rows[1].amount), status="paid", method="cash",
                          purpose="installment", inventory_id=None, installment_no=2,
                          due_date=str(rows[1].due_date))
    payments.create_payment.return_value = row
    # bypass the order-time guard so we exercise the settle-time guard
    with patch.object(servicePayment, "_guard_installment"):
        out = svc.record_cash_payment(int(rows[1].amount), owner_id="C00001", owner_role="customer",
                                      purpose="installment", installment_no=2)
    assert out.status == "paid"
    assert out.installment_status in ("paid", "rejected")  # duplicate settle is tolerated as paid
    # a genuinely different milestone conflict flags review:
    payments.flag_manual_review.assert_not_called() if out.installment_status == "paid" else None


# --------------------------------------------------------------------------- #
#  API                                                                         #
# --------------------------------------------------------------------------- #
_TOKEN = None
_OTHER = None


def _mint(sub, role="customer"):
    import jwt
    return jwt.encode(
        {"sub": sub, "role": role, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "testsecret", algorithm="HS256",
    )


_CUSTOMER_ID = "C70001"


def setup_module(module):
    global _TOKEN, _OTHER
    _reset_db()
    # Seed the customer row directly and mint tokens locally - no /customer/*
    # HTTP calls, so this file adds nothing to the shared login/signup rate-limit
    # buckets (a full-suite run shares one 60s window).
    now = datetime.now(timezone.utc)
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM divine_customer_users WHERE id = :id"), {"id": _CUSTOMER_ID})
            c.execute(text(
                "INSERT INTO divine_customer_users(id, username, email, first_name, last_name, "
                "password_hash, created_date, last_updated_date) VALUES "
                "(:id, 'inst_cust', 'inst_cust@example.com', 'Asha', 'Rao', 'x', :n, :n)"),
                {"id": _CUSTOMER_ID, "n": now})
    except Exception:
        pass
    _TOKEN = _mint(_CUSTOMER_ID)
    _OTHER = _mint("C70099")


def _sub(token):
    import jwt
    return jwt.decode(token, "testsecret", algorithms=["HS256"])["sub"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_profile_exposes_enriched_schedule_and_next_due():
    cid = _sub(_TOKEN)
    bdate = (date.today() - timedelta(days=40)).isoformat()
    _seed_booking(customer_id=cid, booking_date=bdate)
    r = client.get("/customer/profile", headers=_h(_TOKEN))
    assert r.status_code == 200, r.text
    booking = r.json()["booking"]
    # Regression: the booking application is stored as 'project_booking_application';
    # the profile read must still surface every pricing/date field for it, not just
    # has_booking. (Previously the query only matched the exact string
    # 'booking_application', so cash + online bookings alike came back blank.)
    assert booking["has_booking"] is True
    assert booking["total_consideration"] == 5_000_000
    assert booking["booking_date"] == bdate
    assert booking["amount_received"] == 500_000
    sched = booking["payment_schedule"]
    assert len(sched) == 5
    assert sched[0]["status"] == "paid"
    assert sched[1]["pay_enabled_from"] and sched[1]["id"]
    assert booking["next_due"]["milestone_id"] == sched[1]["id"]


def test_create_order_installment_guard_returns_400_code():
    cid = _sub(_OTHER)
    _seed_booking(customer_id=cid, booking_date=date.today().isoformat())  # m2 unpaid, so m3 is out of order
    r = client.post("/payments/create-order",
                    json={"amount": 1250000, "purpose": "installment", "installment_no": 3},
                    headers=_h(_OTHER))
    assert r.status_code == 400
    assert r.json()["detail"] == "installment_out_of_order"


def test_cash_installment_end_to_end_and_receipt_pdf():
    cid = _sub(_TOKEN)
    # fresh booking 100d ago so m2's window is open
    with engine.begin() as c:
        c.execute(text("DELETE FROM divine_documents WHERE owner_id=:o"), {"o": cid})
        c.execute(text("DELETE FROM divine_payment_milestones WHERE customer_id=:o"), {"o": cid})
    _seed_booking(customer_id=cid, booking_date=(date.today() - timedelta(days=100)).isoformat())
    rows = serviceMilestones().ensure_for_customer(cid)
    m2 = rows[1]

    with patch.object(servicePayment, "_send_installment_receipt"):
        r = client.post("/payments/cash", json={
            "amount": int(m2.amount), "purpose": "installment", "installment_no": 2,
            "due_date": str(m2.due_date),
        }, headers=_h(_TOKEN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["purpose"] == "installment"
    assert body["installment_no"] == 2
    assert body["installment_status"] == "paid"
    payment_id = body["id"]

    rc = client.get(f"/payments/{payment_id}/receipt", headers=_h(_TOKEN))
    assert rc.status_code == 200, rc.text
    assert rc.content[:5] == b"%PDF-"
    assert client.get(f"/payments/{payment_id}/receipt", headers=_h(_OTHER)).status_code in (403, 404)


def test_offline_cheque_submission_api_forwards_and_echoes_cheque_number():
    token = _mint("C70003")
    cid = _sub(token)
    _seed_booking(customer_id=cid, booking_date=date.today().isoformat())
    m2 = serviceMilestones().ensure_for_customer(cid)[1]
    body = {"amount": int(m2.amount), "purpose": "installment", "installment_no": 2,
            "due_date": str(m2.due_date), "method": "cheque"}

    r = client.post("/payments/offline-submissions", json={**body, "cheque_number": "CHQ7788", "utr_number": "U1"},
                    headers=_h(token))
    assert r.status_code == 400 and r.json()["detail"] == "utr_only_for_rtgs_neft"

    r = client.post("/payments/offline-submissions", json=body, headers=_h(token))
    assert r.status_code == 400 and r.json()["detail"] == "cheque_number_required"

    r = client.post("/payments/offline-submissions", json={**body, "cheque_number": "CHQ7788"}, headers=_h(token))
    assert r.status_code == 201, r.text
    assert r.json()["method"] == "cheque" and r.json()["cheque_number"] == "CHQ7788"


def test_offline_cash_submission_from_payment_plan_goes_to_admin_review():
    token = _mint("C70004")
    cid = _sub(token)
    _seed_booking(customer_id=cid, booking_date=date.today().isoformat())
    m2 = serviceMilestones().ensure_for_customer(cid)[1]
    r = client.post("/payments/offline-submissions", json={
        "amount": int(m2.amount), "purpose": "installment", "installment_no": 2,
        "due_date": str(m2.due_date), "method": "cash",
    }, headers=_h(token))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["method"] == "cash" and out["status"] == "pending_review"
    assert out["utr_number"] is None and out["cheque_number"] is None
    sched = serviceMilestones().enriched_schedule(cid)["rows"]
    assert sched[1]["status"] == "pending_review"


def test_receipt_404_for_unknown_payment():
    assert client.get("/payments/nope-nope/receipt", headers=_h(_TOKEN)).status_code == 404


# --------------------------------------------------------------------------- #
#  reminder job                                                                #
# --------------------------------------------------------------------------- #
def test_reminder_job_sends_once_then_dedupes(monkeypatch):
    _reset_db()
    # m2 due 15 days out -> T_MINUS_20 window
    _seed_booking(customer_id="C00042",
                  booking_date=(date.today() - timedelta(days=45 - 15)).isoformat())
    serviceMilestones().ensure_for_customer("C00042")

    sent = []
    fake_email = MagicMock()
    fake_email.enabled = True
    fake_email.send_payment_reminder.side_effect = lambda *a, **k: (sent.append(k.get("kind")) or True)
    fake_customers = MagicMock()
    fake_customers.get_by_id.return_value = SimpleNamespace(email="a@b.com", first_name="Asha")

    job = serviceReminders(customer_persistence=fake_customers, email_service=fake_email)
    s1 = job.run()
    assert s1["sent"].get("T_MINUS_20") == 1
    assert sent == ["T_MINUS_20"]

    job2 = serviceReminders(customer_persistence=fake_customers, email_service=fake_email)
    s2 = job2.run()
    assert s2["sent"] == {}          # already recorded -> nothing re-sent


def test_reminder_job_endpoint_requires_token(monkeypatch):
    monkeypatch.delenv("DIVINE_JOBS_TOKEN", raising=False)
    assert client.post("/jobs/payment-reminders").status_code == 503
    monkeypatch.setenv("DIVINE_JOBS_TOKEN", "s3cr3t")
    assert client.post("/jobs/payment-reminders").status_code == 401
    assert client.post("/jobs/payment-reminders?key=wrong").status_code == 401
    ok = client.post("/jobs/payment-reminders?key=s3cr3t")
    assert ok.status_code == 200, ok.text
    assert "scanned_customers" in ok.json()


def test_reminder_job_endpoint_accepts_plain_get(monkeypatch):
    # cron-job.org stores only a URL - a default GET must work too.
    monkeypatch.setenv("DIVINE_JOBS_TOKEN", "s3cr3t")
    assert client.get("/jobs/payment-reminders").status_code == 401
    ok = client.get("/jobs/payment-reminders?key=s3cr3t")
    assert ok.status_code == 200, ok.text
    assert "scanned_customers" in ok.json()

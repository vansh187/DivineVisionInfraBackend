"""Multi-plot booking: a customer may hold more than one plot.

Covers the three server behaviours the frontend depends on:

1. Multiple bookings per customer - independent milestones / schedules /
   amount_received, keyed on (customer, plot), never on customer alone. Two
   plot-booking payments for different units both settle without blocking each
   other. (book_unit idempotency for the same unit+payment, and two customers
   racing one unit, are already covered end-to-end in test_inventory_booking.py.)
2. GET /documents/{id} re-signs ANY document the caller owns - identity photos
   included - with a fresh signed_url; 403 forbidden for another user's doc,
   404 document_not_found when absent.
3. GET /customer/profile exposes every plot in the additive bookings[] array
   while `booking` stays the single most-recent one (old clients unaffected).

Cross-cutting: an `installment` payment on /payments/verify marks the right
plot's milestone paid in the same request.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_milestone import persistenceMilestone
from DivineService.service_milestones import serviceMilestones
from DivineService.service_customer_profile import serviceCustomerProfile
from DivineService.service_payment import servicePayment
from DivineAPI.main import app

client = TestClient(app)

FAKE_SIGNED_URL = "https://fake.supabase.co/storage/v1/object/sign/documents/fresh.png?token=xyz"

_WIPE = (
    "divine_payment_reminders", "divine_payment_milestones",
    "divine_documents", "divine_payments",
)


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def _reset_db():
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()
    try:
        with engine.begin() as c:
            for t in _WIPE:
                c.execute(text(f"DELETE FROM {t}"))
    except Exception:
        pass


def teardown_module(module):
    try:
        with engine.begin() as c:
            for t in _WIPE:
                c.execute(text(f"DELETE FROM {t}"))
    except Exception:
        pass


def _mint(sub, role="customer"):
    return jwt.encode(
        {"sub": sub, "role": role, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "testsecret", algorithm="HS256",
    )


def _ensure_customer(cid, email=None):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text("DELETE FROM divine_customer_users WHERE id = :id"), {"id": cid})
        c.execute(text(
            "INSERT INTO divine_customer_users(id, username, email, first_name, last_name, "
            "password_hash, created_date, last_updated_date) VALUES "
            "(:id, :u, :e, 'Test', 'User', 'x', :n, :n)"),
            {"id": cid, "u": f"user_{cid}", "e": email or f"{cid.lower()}@example.com", "n": now})


def _seed_plot(cid, *, unit, total, inventory_id, days_ago=120, created_offset_min=0,
               project=None, payment_status="paid", with_total=True, with_form_amount=True):
    """One settled plot booking: a plot_booking payment + a
    project_booking_application document, exactly as the real upload flow stores
    them (minus the storage upload). Returns a handle with the ids.

    `payment_status` / `with_total` let a test model a booking whose money is not
    (yet) attributable - an unsettled or unlinked booking payment, or a form with
    no parseable total - to prove another plot's balance never leaks into it."""
    booking_amount = int(round(total * 0.10))
    booking_id = "doc-" + uuid.uuid4().hex[:12]
    payment_id = "pay-" + uuid.uuid4().hex[:12]
    bdate = (date.today() - timedelta(days=days_ago)).isoformat()
    created = datetime.now(timezone.utc) + timedelta(minutes=created_offset_min)
    project_id = project or f"proj-{unit}"
    form = {"unit_number": unit, "project_name": project_id, "booking_date": bdate}
    if with_total:
        form["total_consideration"] = total
    if with_form_amount:
        form["amount_received"] = booking_amount
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, "
            "method, purpose, inventory_id, razorpay_order_id, razorpay_payment_id, "
            "created_date, last_updated_date) VALUES "
            "(:id, :o, 'customer', :amt, 'INR', :st, 'razorpay', 'plot_booking', :inv, "
            ":order_id, :rzp_id, :n, :n)"),
            {"id": payment_id, "o": cid, "amt": booking_amount, "inv": inventory_id,
             "st": payment_status, "order_id": f"order-{payment_id}",
             "rzp_id": f"rzp-{payment_id}", "n": created})
        c.execute(text(
            "INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, "
            "storage_path, status, storage_bucket, project_id, payment_id, created_date, "
            "last_updated_date) VALUES (:id, :o, 'customer', 'project_booking_application', :fd, "
            "'x/y.pdf', 'generated', 'documents', :pj, :pay, :n, :n)"),
            {"id": booking_id, "o": cid, "fd": json.dumps(form),
             "pj": project_id, "pay": payment_id, "n": created})
    return SimpleNamespace(
        booking_id=booking_id, payment_id=payment_id, unit=unit, total=total,
        inventory_id=inventory_id, booking_amount=booking_amount,
    )


def _seed_document(cid, document_type, storage_path=None):
    doc_id = "docp-" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, "
            "storage_path, status, storage_bucket, created_date, last_updated_date) VALUES "
            "(:id, :o, 'customer', :dt, :fd, :sp, 'uploaded', 'documents', :n, :n)"),
            {"id": doc_id, "o": cid, "dt": document_type,
             "fd": json.dumps({"content_type": "image/png"}),
             "sp": storage_path or f"{cid}/{document_type}_{doc_id}.png", "n": now})
    return doc_id


def _pay_service():
    svc = servicePayment(MagicMock(), MagicMock())
    svc._key_id, svc._key_secret = "rzp_test_fake", "fake_secret"
    return svc


def _pay_row(**kw):
    base = {"id": "pay1", "owner_id": "C81000", "owner_role": "customer", "amount": 500000,
            "currency": "INR", "status": "paid", "method": "razorpay", "purpose": "other",
            "inventory_id": None, "installment_no": None, "due_date": None,
            "razorpay_order_id": None, "razorpay_payment_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
#  1. multiple bookings per customer                                           #
# --------------------------------------------------------------------------- #
def test_profile_lists_every_plot_and_scopes_amounts_per_plot():
    _reset_db()
    cid = "C81001"
    _ensure_customer(cid)
    a = _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A", created_offset_min=0)
    b = _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", created_offset_min=5)
    serviceMilestones().ensure_for_customer(cid)

    dto = serviceCustomerProfile().get_profile(cid, "customer")

    assert dto.bookings is not None and len(dto.bookings) == 2
    # newest first; `booking` mirrors bookings[0]
    assert [e.unit_number for e in dto.bookings] == ["B-2", "A-1"]
    assert dto.booking.unit_number == "B-2"
    assert dto.booking.has_booking is True

    by_unit = {e.unit_number: e for e in dto.bookings}
    assert by_unit["A-1"].id == a.booking_id
    assert by_unit["A-1"].document_id == a.booking_id
    assert by_unit["A-1"].inventory_id == "INV-A"
    assert by_unit["A-1"].payment_id == a.payment_id
    assert by_unit["A-1"].booking_payment_amount == 500_000
    assert by_unit["A-1"].payment_method == "razorpay"
    assert by_unit["A-1"].razorpay_order_id == f"order-{a.payment_id}"
    assert by_unit["A-1"].razorpay_payment_id == f"rzp-{a.payment_id}"
    assert by_unit["A-1"].payment_created_date is not None
    assert by_unit["A-1"].total_consideration == 5_000_000
    assert by_unit["B-2"].total_consideration == 8_000_000
    # amount_received is the paid (on-booking) milestone for THAT plot only - the
    # two figures never merge.
    assert by_unit["A-1"].amount_received == 500_000
    assert by_unit["B-2"].amount_received == 800_000
    assert dto.booking.amount_received == 800_000

    for e in dto.bookings:
        assert e.payment_schedule and len(e.payment_schedule) == 5
        assert e.payment_schedule[0].status == "paid"
        assert all(row.id and row.id.startswith(
            b.booking_id if e.unit_number == "B-2" else a.booking_id
        ) for row in e.payment_schedule)


def test_single_plot_profile_shape_is_unchanged():
    _reset_db()
    cid = "C81002"
    _ensure_customer(cid)
    _seed_plot(cid, unit="S-9", total=6_000_000, inventory_id="INV-S")
    serviceMilestones().ensure_for_customer(cid)

    dto = serviceCustomerProfile().get_profile(cid, "customer")

    assert dto.booking.has_booking is True
    assert dto.booking.unit_number == "S-9"
    assert dto.booking.payment_schedule and len(dto.booking.payment_schedule) == 5
    assert dto.bookings is not None and len(dto.bookings) == 1
    assert dto.bookings[0].unit_number == dto.booking.unit_number
    assert dto.bookings[0].amount_received == dto.booking.amount_received


def test_no_plot_profile_has_empty_bookings_and_empty_booking():
    _reset_db()
    cid = "C81003"
    _ensure_customer(cid)

    dto = serviceCustomerProfile().get_profile(cid, "customer")

    assert dto.booking.has_booking is False
    assert dto.bookings == []


def test_amount_received_never_leaks_across_plots_in_the_same_project():
    """Two plots in the SAME project. Plot A's booking payment is unsettled, its
    form carries no total and no amount_received, and nothing backs it with
    milestones - the exact conditions under which the per-payment scope yields 0.
    A's amount_received must stay empty, never widen to plot B's (or the combined)
    paid balance."""
    _reset_db()
    cid = "C81007"
    _ensure_customer(cid)
    _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A", project="shared-proj",
               payment_status="created", with_total=False, with_form_amount=False,
               created_offset_min=0)
    _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", project="shared-proj",
               created_offset_min=5)
    serviceMilestones().ensure_for_customer(cid, backfill_all=True)

    dto = serviceCustomerProfile().get_profile(cid, "customer")
    by_unit = {e.unit_number: e for e in dto.bookings}

    assert by_unit["B-2"].amount_received == 800_000
    assert by_unit["A-1"].amount_received in (None, 0)          # empty, not leaked
    assert by_unit["A-1"].amount_received != by_unit["B-2"].amount_received
    assert by_unit["A-1"].amount_received != 1_300_000          # not the combined total


def test_ensure_for_customer_default_keeps_the_fast_path_and_does_not_scan_all_bookings():
    """The hot path (backfill_all=False) must not fan out to every booking doc: a
    customer who already has milestone rows gets them back untouched, and a fresh
    customer materialises only their most-recent plan. Full coverage is opt-in via
    backfill_all=True."""
    _reset_db()
    cid = "C81008"
    _ensure_customer(cid)
    _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A", created_offset_min=0)
    _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", created_offset_min=5)

    svc = serviceMilestones()
    default_rows = svc.ensure_for_customer(cid)            # fresh -> most-recent plan only
    assert len(default_rows) == 5
    assert {r.booking_id for r in default_rows} == {
        r.booking_id for r in default_rows if r.milestone_no == 1
    }  # single booking id

    # already has rows -> returned as-is, still just the one plan
    assert len(svc.ensure_for_customer(cid)) == 5

    # opt in -> both plans now materialised
    assert len(svc.ensure_for_customer(cid, backfill_all=True)) == 10


def test_enriched_schedule_is_scoped_by_booking_id():
    _reset_db()
    cid = "C81004"
    _ensure_customer(cid)
    a = _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A")
    _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", created_offset_min=5)
    svc = serviceMilestones()
    svc.ensure_for_customer(cid, backfill_all=True)   # materialise every plot's plan

    everything = svc.enriched_schedule(cid)["rows"]
    assert len(everything) == 10  # both plots, unscoped legacy view

    just_a = svc.enriched_schedule(cid, a.booking_id)["rows"]
    assert len(just_a) == 5
    assert all(r["id"].startswith(a.booking_id) for r in just_a)


def test_two_plot_booking_payments_lock_their_own_units_independently():
    """A second plot_booking for a different unit must not be blocked by the first.
    Each call flips its own unit; neither races the other."""
    for inv in ("INV-A", "INV-B"):
        svc = _pay_service()
        svc._persistence.create_payment.return_value = _pay_row(
            method="cash", owner_role="broker", purpose="plot_booking", inventory_id=inv)
        svc._inventory_persistence.book_unit.return_value = _pay_row(id=inv, status="booked")

        record = svc.record_cash_payment(
            500000, owner_id="B90001", owner_role="broker",
            purpose="plot_booking", inventory_id=inv)

        svc._inventory_persistence.book_unit.assert_called_once_with(
            id=inv, payment_id="pay1", customer_id="C81000")
        assert record.inventory_status == "booked"
        assert record.inventory_conflict_reason is None


# --------------------------------------------------------------------------- #
#  cross-cutting: installment settles the right plot                           #
# --------------------------------------------------------------------------- #
def test_validate_installment_targets_the_plot_named_by_inventory_id():
    _reset_db()
    cid = "C81005"
    _ensure_customer(cid)
    a = _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A", days_ago=120)
    b = _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", days_ago=120,
                   created_offset_min=5)
    svc = serviceMilestones()
    svc.ensure_for_customer(cid)

    b_m2 = 8_000_000 * 15 // 100        # 1,200,000
    a_m2 = 5_000_000 * 15 // 100        # 750,000

    milestone, code = svc.validate_installment(cid, 2, b_m2, inventory_id="INV-B")
    assert code is None
    assert int(milestone.amount) == b_m2
    assert milestone.booking_id == b.booking_id

    # A's amount against B's plot is a mismatch, not a silent hit on A.
    assert svc.validate_installment(cid, 2, a_m2, inventory_id="INV-B")[1] == "installment_amount_mismatch"

    svc.mark_paid(milestone.id, "pay-inst-b")
    status = {(r.booking_id, r.milestone_no): r.status
              for r in persistenceMilestone().list_for_customer(cid)}
    assert status[(b.booking_id, 2)] == "paid"
    assert status[(a.booking_id, 2)] != "paid"      # the other plot is untouched


def test_verify_installment_settlement_marks_only_the_named_plots_milestone():
    _reset_db()
    cid = "C81006"
    _ensure_customer(cid)
    a = _seed_plot(cid, unit="A-1", total=5_000_000, inventory_id="INV-A", days_ago=120)
    b = _seed_plot(cid, unit="B-2", total=8_000_000, inventory_id="INV-B", days_ago=120,
                   created_offset_min=5)
    serviceMilestones().ensure_for_customer(cid)

    record = SimpleNamespace(
        id="pay-verify-b", owner_id=cid, owner_role="customer", amount=8_000_000 * 15 // 100,
        currency="INR", status="paid", method="razorpay", purpose="installment",
        inventory_id="INV-B", installment_no=2, due_date=None, installment_status=None,
    )
    svc = _pay_service()
    with patch.object(servicePayment, "_send_installment_receipt"):
        svc._apply_installment_settlement(record)

    assert record.installment_status == "paid"
    status = {(r.booking_id, r.milestone_no): r.status
              for r in persistenceMilestone().list_for_customer(cid)}
    assert status[(b.booking_id, 2)] == "paid"
    assert status[(a.booking_id, 2)] != "paid"


# --------------------------------------------------------------------------- #
#  2. GET /documents/{id} re-signs any owned document                          #
# --------------------------------------------------------------------------- #
def test_get_document_resigns_every_owned_type_and_uses_clear_error_codes():
    _reset_db()
    owner, stranger = "C82001", "C82002"
    _ensure_customer(owner)
    _ensure_customer(stranger)
    owner_tok, stranger_tok = _mint(owner), _mint(stranger)

    with patch("DivineService.service_document.serviceDocument._sign_url",
               return_value=FAKE_SIGNED_URL):
        for dtype in ("aadhaar_front", "pan_card", "applicant_photo", "co_applicant_photo"):
            doc_id = _seed_document(owner, dtype)
            r = client.get(f"/documents/{doc_id}", headers={"Authorization": f"Bearer {owner_tok}"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["signed_url"] == FAKE_SIGNED_URL
            assert body["signed_url_expires_in"] > 0
            assert body["document_type"] == dtype

        # another user's document -> 403 forbidden (ownership only)
        other_doc = _seed_document(owner, "applicant_photo")
        r = client.get(f"/documents/{other_doc}",
                       headers={"Authorization": f"Bearer {stranger_tok}"})
        assert r.status_code == 403
        assert r.json()["detail"] == "forbidden"

    # absent -> 404 document_not_found
    r = client.get("/documents/does-not-exist-xyz",
                   headers={"Authorization": f"Bearer {owner_tok}"})
    assert r.status_code == 404
    assert r.json()["detail"] == "document_not_found"

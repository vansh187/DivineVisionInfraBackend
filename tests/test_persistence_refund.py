"""Integration coverage for persistenceRefund against a real SQLite database."""
import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_payment import persistencePayment
from Divinepersistence.persistence_booking import persistenceBooking
from Divinepersistence.persistence_refund import persistenceRefund

_CUSTOMER_A = None
_CUSTOMER_B = None
_ZOHO_PENDING_ID = None
_ZOHO_COMPLETED_ID = None
_ZOHO_FAILED_ID = None
_CASH_PENDING_ID = None
_CASH_COLLECTED_ID = None
_BANK_PENDING_ID = None
_BANK_COMPLETED_ID = None
_LEGACY_RAZORPAY_ID = None


def setup_module(module):
    global _CUSTOMER_A, _CUSTOMER_B, _ZOHO_PENDING_ID, _ZOHO_COMPLETED_ID
    global _ZOHO_FAILED_ID, _CASH_PENDING_ID, _CASH_COLLECTED_ID
    global _BANK_PENDING_ID, _BANK_COMPLETED_ID, _LEGACY_RAZORPAY_ID

    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer_a = persistenceCustomer().create_user(
        username="refund_customer_a", password_hash="not-used",
        phone="9000000301", email="refund.a@example.com", first_name="Asha", last_name="Mehta",
    )
    customer_b = persistenceCustomer().create_user(
        username="refund_customer_b", password_hash="not-used",
        phone="9000000302", email="refund.b@example.com", first_name="Kabir", last_name="Rao",
    )
    _CUSTOMER_A = customer_a.id
    _CUSTOMER_B = customer_b.id

    unit_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit_id, project_name="Refund QA Orchard", city="Karnal",
        unit_number="R-01", area_sqmt=200, area_sqyd=239, status="available",
    )

    payments = persistencePayment()
    bookings = persistenceBooking()

    zoho_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=100000, currency="INR", status="created", method="zoho",
        purpose="plot_booking", inventory_id=unit_id,
    )
    payments.update_payment_status(zoho_pending.id, "paid", "pay_pending", "sig")
    bookings.create_booking(
        payment_id=zoho_pending.id, inventory_id=unit_id, customer_id=_CUSTOMER_A,
        project_name="Refund QA Orchard", unit_number="R-01", amount=100000,
    )
    payments.update_refund_status(zoho_pending.id, "pending", refund_amount=100000, refund_note="gateway timeout")
    _ZOHO_PENDING_ID = zoho_pending.id

    zoho_completed = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=200000, currency="INR", status="created", method="zoho", purpose="other",
    )
    payments.update_payment_status(zoho_completed.id, "paid", "pay_completed", "sig")
    payments.update_refund_status(
        zoho_completed.id, "completed", refund_amount=200000, zoho_refund_id="rfnd_ok",
    )
    _ZOHO_COMPLETED_ID = zoho_completed.id

    zoho_failed = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_B, owner_role="customer",
        amount=300000, currency="INR", status="created", method="zoho", purpose="other",
    )
    payments.update_payment_status(zoho_failed.id, "paid", "pay_failed", "sig")
    payments.update_refund_status(zoho_failed.id, "failed", refund_amount=300000)
    _ZOHO_FAILED_ID = zoho_failed.id

    cash_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_B, owner_role="customer",
        amount=400000, currency="INR", status="paid", method="cash", purpose="other",
    )
    payments.update_refund_status(cash_pending.id, "pending", refund_amount=400000)
    _CASH_PENDING_ID = cash_pending.id

    cash_collected = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_B, owner_role="customer",
        amount=500000, currency="INR", status="paid", method="cash", purpose="other",
    )
    payments.update_refund_status(cash_collected.id, "completed", refund_amount=500000)
    _CASH_COLLECTED_ID = cash_collected.id

    bank_pending = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=600000, currency="INR", status="paid", method="rtgs_neft", utr_number="UTRREFUND1",
        purpose="other",
    )
    payments.update_refund_status(bank_pending.id, "pending", refund_amount=600000)
    _BANK_PENDING_ID = bank_pending.id

    bank_completed = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=700000, currency="INR", status="paid", method="rtgs_neft", utr_number="UTRREFUND2",
        purpose="other",
    )
    payments.update_refund_status(bank_completed.id, "completed", refund_amount=700000)
    _BANK_COMPLETED_ID = bank_completed.id

    payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=800000, currency="INR", status="paid", method="zoho", purpose="other",
    )

    # A pre-cutover payment that went through the (now-retired) Razorpay gateway -
    # its refund must now behave like a manual method (bank_transfer_* bucket),
    # never the live 'zoho' processing/completed/failed bucket, since there is no
    # gateway left to call for it. Seeded with seed_legacy_razorpay_fields, the
    # only place in the app allowed to write a razorpay_* value post-cutover.
    legacy_razorpay = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=900000, currency="INR", status="created", method="razorpay", purpose="other",
    )
    payments.seed_legacy_razorpay_fields(
        legacy_razorpay.id, razorpay_order_id="order_legacy", razorpay_payment_id="pay_legacy",
    )
    payments.update_payment_status(legacy_razorpay.id, "paid", None, None)
    payments.update_refund_status(legacy_razorpay.id, "pending", refund_amount=900000)
    _LEGACY_RAZORPAY_ID = legacy_razorpay.id


def _persistence():
    return persistenceRefund()


def _rows_by_id(rows):
    return {r.id: r for r in rows}


def test_refunded_fixtures_are_returned_with_display_statuses():
    rows = _rows_by_id(_persistence().list_refunds(limit=1000, offset=0))

    assert rows[_ZOHO_PENDING_ID].display_status == "processing"
    assert rows[_ZOHO_COMPLETED_ID].display_status == "completed"
    assert rows[_ZOHO_FAILED_ID].display_status == "failed"
    assert rows[_CASH_PENDING_ID].display_status == "cash_refund_pending"
    assert rows[_CASH_COLLECTED_ID].display_status == "cash_collected"
    assert rows[_BANK_PENDING_ID].display_status == "bank_transfer_pending"
    assert rows[_BANK_COMPLETED_ID].display_status == "bank_transfer_completed"


def test_no_refund_rows_are_excluded():
    rows = _persistence().list_refunds(limit=1000, offset=0)
    assert all(r.refund_status != "none" for r in rows)


def test_filter_by_display_status():
    rows = _persistence().list_refunds(status="cash_refund_pending", limit=1000, offset=0)
    ids = {r.id for r in rows}
    assert _CASH_PENDING_ID in ids
    assert all(r.display_status == "cash_refund_pending" for r in rows)


def test_filter_by_method():
    rows = _persistence().list_refunds(method="rtgs_neft", limit=1000, offset=0)
    ids = {r.id for r in rows}
    assert _BANK_PENDING_ID in ids
    assert _BANK_COMPLETED_ID in ids
    assert all(r.method == "rtgs_neft" for r in rows)


def test_search_matches_project_and_customer():
    project_rows = _persistence().list_refunds(search="Refund QA Orchard", limit=1000, offset=0)
    assert {r.id for r in project_rows} == {_ZOHO_PENDING_ID}

    customer_rows = _persistence().list_refunds(search="Kabir Rao", limit=1000, offset=0)
    ids = {r.id for r in customer_rows}
    assert _ZOHO_FAILED_ID in ids
    assert _CASH_PENDING_ID in ids


def test_search_is_wildcard_safe_for_underscore():
    rows = _persistence().list_refunds(search="refund_qa_orchard_typo", limit=1000, offset=0)
    assert rows == []


def test_get_refund_by_id_returns_detail_fields():
    row = _persistence().get_refund(_ZOHO_PENDING_ID)
    assert row is not None
    assert row.display_status == "processing"
    assert row.project_name == "Refund QA Orchard"
    assert row.customer_first_name == "Asha"
    assert row.refund_note == "gateway timeout"


def test_get_refund_returns_none_for_unknown_or_non_refund_payment():
    payments = persistencePayment()
    no_refund = payments.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=1, currency="INR", status="paid", method="cash", purpose="other",
    )

    assert _persistence().get_refund("does-not-exist") is None
    assert _persistence().get_refund(no_refund.id) is None


def test_legacy_razorpay_refund_uses_manual_bucket_not_gateway_bucket():
    """A pre-cutover razorpay payment's refund must bucket like a manual
    (bank-transfer-style) refund, not the live 'zoho' processing/completed/
    failed bucket - there's no gateway left to call for it."""
    row = _persistence().get_refund(_LEGACY_RAZORPAY_ID)
    assert row is not None
    assert row.display_status == "bank_transfer_pending"
    assert row.method == "razorpay"


def test_pagination_has_total_count():
    page1 = _persistence().list_refunds(limit=2, offset=0)
    page2 = _persistence().list_refunds(limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.id for r in page1}.isdisjoint({r.id for r in page2})
    assert int(page1[0].total_count) >= 8

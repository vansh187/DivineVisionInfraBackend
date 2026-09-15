"""Integration coverage for persistenceRevenue against a real (SQLite) database -
verifies the divine_payments/divine_bookings/divine_customer_users join, the
revenue_status bucketing, and search/filter/pagination, independent of the
mocked unit tests in test_service_revenue.py and the HTTP-level coverage in
test_admin_revenue_api.py.

NOTE ON ISOLATION: this suite's own fixture rows are asserted for by exact id
(never by "the whole table has exactly N rows") because the shared
test_db.sqlite is written by many test files in the same pytest session, and
on Windows a prior file's still-open SQLAlchemy engine can make this file's
own setup_module unable to actually delete/recreate it - the same reason
test_admin_visits_api.py asserts `total_items >= 1` rather than `== 1`. Every
assertion below is written to hold whether or not other files' rows are
present alongside these fixtures."""
import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_payment import persistencePayment
from Divinepersistence.persistence_booking import persistenceBooking
from Divinepersistence.persistence_revenue import persistenceRevenue
from DivineService.service_payment import servicePayment

_CUSTOMER_A = None
_CUSTOMER_B = None
_CAPTURED_ID = None
_CASH_ID = None
_REFUNDED_ID = None
_REFUND_PENDING_ID = None


def setup_module(module):
    global _CUSTOMER_A, _CUSTOMER_B, _CAPTURED_ID, _CASH_ID, _REFUNDED_ID, _REFUND_PENDING_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer_a = persistenceCustomer().create_user(
        username="revenue_customer_a", password_hash="not-used-by-these-tests",
        phone="9000000101", email="revenue.a@example.com", first_name="Meera", last_name="Pillai",
    )
    _CUSTOMER_A = customer_a.id
    customer_b = persistenceCustomer().create_user(
        username="revenue_customer_b", password_hash="not-used-by-these-tests",
        phone="9000000102", email="revenue.b@example.com", first_name="Rehan", last_name="Sharma",
    )
    _CUSTOMER_B = customer_b.id

    inv = persistenceInventory()
    unit_captured = str(uuid.uuid4())
    inv.upsert_unit(id=unit_captured, project_name="Palm County", city="Karnal",
                     unit_number="P-01", area_sqmt=200, area_sqyd=239, status="available")
    unit_refund = str(uuid.uuid4())
    inv.upsert_unit(id=unit_refund, project_name="Emerald Hills", city="Karnal",
                     unit_number="E-09", area_sqmt=200, area_sqyd=239, status="available")

    payment_persistence = persistencePayment()
    booking_persistence = persistenceBooking()

    # Captured: razorpay, settled, no refund in flight. Inserted straight
    # through the persistence layer (not servicePayment.verify_payment) so a
    # matching divine_bookings row is created explicitly too - this is what
    # a real plot_booking settlement does as a side effect, and the revenue
    # join depends on it for booking_id/project_name/unit_number.
    captured = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=3120000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_captured,
    )
    payment_persistence.update_payment_status(
        captured.id, status="paid", razorpay_payment_id="pay_captured", razorpay_signature="sig_captured",
    )
    booking_persistence.create_booking(
        payment_id=captured.id, inventory_id=unit_captured, customer_id=_CUSTOMER_A,
        project_name="Palm County", unit_number="P-01", amount=3120000,
    )
    _CAPTURED_ID = captured.id

    # Cash recorded: settles as "paid" immediately via servicePayment.
    cash_record = servicePayment().record_cash_payment(
        amount=980000, owner_id=_CUSTOMER_B, owner_role="customer",
        purpose="other", method="cash",
    )
    _CASH_ID = cash_record.id

    # Refunded: a settled razorpay plot_booking payment whose refund has completed.
    refunded = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_B, owner_role="customer",
        amount=2100000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_refund,
    )
    payment_persistence.update_payment_status(
        refunded.id, status="paid", razorpay_payment_id="pay_refunded", razorpay_signature="sig_refunded",
    )
    booking_persistence.create_booking(
        payment_id=refunded.id, inventory_id=unit_refund, customer_id=_CUSTOMER_B,
        project_name="Emerald Hills", unit_number="E-09", amount=2100000,
    )
    payment_persistence.update_refund_status(refunded.id, refund_status="completed", refund_amount=2100000)
    _REFUNDED_ID = refunded.id

    # Refund pending: settled, refund kicked off but not yet completed.
    refund_pending = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=500000, currency="INR", status="created", method="rtgs_neft", utr_number="UTR900001",
        purpose="other",
    )
    payment_persistence.update_payment_status(
        refund_pending.id, status="paid", razorpay_payment_id=None, razorpay_signature=None,
    )
    payment_persistence.update_refund_status(refund_pending.id, refund_status="pending", refund_amount=500000)
    _REFUND_PENDING_ID = refund_pending.id

    # Never-settled payment - must NEVER show up in revenue at all.
    payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=100000, currency="INR", status="created", method="razorpay", purpose="other",
    )


def _persistence():
    return persistenceRevenue()


def _rows_by_id(rows):
    return {r.transaction_id: r for r in rows}


def test_settled_fixtures_are_all_returned():
    rows = _rows_by_id(_persistence().list_transactions(limit=1000, offset=0))
    assert _CAPTURED_ID in rows
    assert _CASH_ID in rows
    assert _REFUNDED_ID in rows
    assert _REFUND_PENDING_ID in rows


def test_never_settled_payment_is_excluded():
    rows = _persistence().list_transactions(search="", limit=1000, offset=0)
    # The 'created' fixture above was never given an id we track, but every
    # returned row must be a settled ('paid') payment - verified indirectly:
    # get_transaction (which shares the same status='paid' guard) must refuse
    # any payment id that was never settled.
    unsettled = persistencePayment().create_payment(
        id=str(uuid.uuid4()), owner_id=_CUSTOMER_A, owner_role="customer",
        amount=1, currency="INR", status="created", method="razorpay", purpose="other",
    )
    assert _persistence().get_transaction(unsettled.id) is None
    assert all(r.transaction_id != unsettled.id for r in rows)


def test_revenue_status_bucketing():
    rows = _rows_by_id(_persistence().list_transactions(limit=1000, offset=0))
    assert rows[_CAPTURED_ID].revenue_status == "captured"
    assert rows[_CASH_ID].revenue_status == "cash_recorded"
    assert rows[_REFUNDED_ID].revenue_status == "refunded"
    assert rows[_REFUND_PENDING_ID].revenue_status == "refund_pending"


def test_booking_join_populates_project_and_unit():
    rows = _rows_by_id(_persistence().list_transactions(limit=1000, offset=0))
    assert rows[_CAPTURED_ID].project_name == "Palm County"
    assert rows[_CAPTURED_ID].unit_number == "P-01"
    assert rows[_CAPTURED_ID].booking_id is not None


def test_filter_by_revenue_status_only_returns_matching_rows():
    rows = _persistence().list_transactions(revenue_status="refunded", limit=1000, offset=0)
    ids = {r.transaction_id for r in rows}
    assert _REFUNDED_ID in ids
    assert all(r.revenue_status == "refunded" for r in rows)


def test_filter_by_method_only_returns_matching_rows():
    rows = _persistence().list_transactions(method="cash", limit=1000, offset=0)
    ids = {r.transaction_id for r in rows}
    assert _CASH_ID in ids
    assert all(r.method == "cash" for r in rows)


def test_search_matches_project_name():
    rows = _persistence().list_transactions(search="Palm County", limit=1000, offset=0)
    ids = {r.transaction_id for r in rows}
    assert ids == {_CAPTURED_ID}  # "Palm County" is unique to this fixture set
    assert rows[0].project_name == "Palm County"


def test_search_matches_customer_name():
    rows = _persistence().list_transactions(search="Rehan Sharma", limit=1000, offset=0)
    ids = {r.transaction_id for r in rows}
    assert _CASH_ID in ids
    assert _REFUNDED_ID in ids


def test_search_is_wildcard_safe_for_underscore():
    """A literal underscore in :search must not act as a SQL LIKE wildcard -
    escaped per admin_revenue_queries.yaml, this must match nothing."""
    rows = _persistence().list_transactions(search="palm_county_typo_zzz", limit=1000, offset=0)
    assert rows == []


def test_pagination_has_no_overlap_or_gap_across_pages():
    page1 = _persistence().list_transactions(limit=2, offset=0)
    page2 = _persistence().list_transactions(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.transaction_id for r in page1}.isdisjoint({r.transaction_id for r in page2})
    assert int(page1[0].total_count) >= 4


def test_get_transaction_by_id():
    row = _persistence().get_transaction(_CAPTURED_ID)
    assert row is not None
    assert row.revenue_status == "captured"
    assert row.customer_first_name == "Meera"


def test_get_transaction_returns_none_for_unknown_id():
    assert _persistence().get_transaction("does-not-exist") is None


def test_rebooked_plot_never_duplicates_an_installment_payment():
    """Regression test for the join-duplication bug: if a customer cancels a
    booking and later rebooks the SAME plot, divine_bookings ends up with two
    rows sharing one inventory_id + customer_id. An installment payment on
    that plot (which has no bookings row of its own - only the ORIGINAL
    plot_booking payment does) must resolve to exactly ONE booking via the
    fallback match, never both - a naive OR-joined LEFT JOIN would emit this
    one payment TWICE, once per matching booking row, silently duplicating it
    in the revenue table."""
    customer = persistenceCustomer().create_user(
        username="revenue_rebook_customer", password_hash="not-used-by-these-tests",
        phone="9000000199", email="revenue.rebook@example.com", first_name="Asha", last_name="Mehta",
    )
    unit_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit_id, project_name="Rebooked Ridge", city="Karnal",
        unit_number="R-01", area_sqmt=200, area_sqyd=239, status="available",
    )

    payment_persistence = persistencePayment()
    booking_persistence = persistenceBooking()

    # First booking cycle: settled, then (conceptually) cancelled - the
    # booking row itself is never deleted, it just stops being the "current"
    # one for that plot.
    first_cycle_payment = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=customer.id, owner_role="customer",
        amount=1000000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_id,
    )
    payment_persistence.update_payment_status(
        first_cycle_payment.id, status="paid", razorpay_payment_id="pay_first", razorpay_signature="sig_first",
    )
    booking_persistence.create_booking(
        payment_id=first_cycle_payment.id, inventory_id=unit_id, customer_id=customer.id,
        project_name="Rebooked Ridge", unit_number="R-01", amount=1000000,
    )

    # Second booking cycle: the same customer rebooks the same plot - a
    # second divine_bookings row, same inventory_id + customer_id as the first.
    second_cycle_payment = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=customer.id, owner_role="customer",
        amount=1000000, currency="INR", status="created", method="razorpay",
        purpose="plot_booking", inventory_id=unit_id,
    )
    payment_persistence.update_payment_status(
        second_cycle_payment.id, status="paid", razorpay_payment_id="pay_second", razorpay_signature="sig_second",
    )
    booking_persistence.create_booking(
        payment_id=second_cycle_payment.id, inventory_id=unit_id, customer_id=customer.id,
        project_name="Rebooked Ridge", unit_number="R-01", amount=1000000,
    )

    # An installment payment on the (now current) second booking cycle - has
    # no divine_bookings row of its own, only matchable by inventory_id + customer_id.
    installment_payment = payment_persistence.create_payment(
        id=str(uuid.uuid4()), owner_id=customer.id, owner_role="customer",
        amount=200000, currency="INR", status="created", method="razorpay",
        purpose="installment", installment_no=1, inventory_id=unit_id,
    )
    payment_persistence.update_payment_status(
        installment_payment.id, status="paid", razorpay_payment_id="pay_installment", razorpay_signature="sig_inst",
    )

    rows = _persistence().list_transactions(search=None, limit=1000, offset=0)
    matches = [r for r in rows if r.transaction_id == installment_payment.id]
    assert len(matches) == 1, f"installment payment must appear exactly once, got {len(matches)}"

    detail = _persistence().get_transaction(installment_payment.id)
    assert detail is not None
    # Resolves to the second (most recent) booking cycle, not the first.
    assert detail.booking_id == booking_persistence.get_by_payment_id(second_cycle_payment.id).id

    assert _persistence().count_transactions(search=str(installment_payment.id)) == 1


def test_get_summary_reflects_fixture_amounts():
    summary = _persistence().get_summary()
    assert int(summary.total_transactions) >= 4
    assert float(summary.captured_amount) >= 3120000
    assert float(summary.cash_amount) >= 980000
    assert float(summary.refunded_amount) >= 2100000
    assert float(summary.refund_pending_amount) >= 500000

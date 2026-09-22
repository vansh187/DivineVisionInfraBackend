"""Plot-inventory 'booked' flow: a settled plot-booking payment locks the unit,
it drops out of search, and the broker repair endpoints can move it back."""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from fastapi.testclient import TestClient

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_payment import persistencePayment
from DivineService.service_payment import servicePayment
from DivineService.service_inventory import serviceInventory
from DivineAPI.inventory_api import _inventory_service as _api_inventory_service
from DivineAPI.main import app

client = TestClient(app)


# --------------------------------------------------------------------------- #
# servicePayment: booking flip on settle                                       #
# --------------------------------------------------------------------------- #
def _pay_service(gateway=None):
    payments = MagicMock()
    inventory = MagicMock()
    booking = MagicMock()
    # create_booking must return something truthy with a plain .id (not another
    # MagicMock, which getattr(..., "id", None) would happily return as an
    # auto-created attribute rather than a real string) for record.booking_id
    # assertions to mean anything.
    booking.create_booking.return_value = SimpleNamespace(id="BKG-2026-000001")
    svc = servicePayment(payments, inventory, booking, gateway=gateway or MagicMock())
    return svc, payments, inventory, booking


def _row(**kw):
    base = {"id": "pay1", "owner_id": "C00001", "owner_role": "customer", "amount": 500000,
            "currency": "INR", "status": "paid", "method": "zoho", "purpose": "other",
            "inventory_id": None, "zoho_payments_session_id": None, "zoho_payment_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_broker_recorded_cash_booking_holds_unit_for_kyc_review():
    svc, payments, inventory, booking = _pay_service()
    payments.create_payment.return_value = _row(method="cash", owner_role="broker",
                                                purpose="plot_booking", inventory_id="INV-9")
    inventory.hold_for_kyc_review.return_value = _row(id="INV-9", status="pending_kyc_review",
                                                       project_name="Green Meadows", unit_number="A-1")

    record = svc.record_cash_payment(500000, owner_id="B00001", owner_role="broker",
                                     purpose="plot_booking", inventory_id="INV-9")

    inventory.hold_for_kyc_review.assert_called_once_with(id="INV-9", payment_id="pay1", customer_id="C00001")
    assert record.inventory_status == "pending_kyc_review"
    assert record.inventory_conflict_reason is None
    assert record.booking_id == "BKG-2026-000001"
    booking.create_booking.assert_called_once_with(
        payment_id="pay1", inventory_id="INV-9", customer_id="C00001",
        project_name="Green Meadows", unit_number="A-1", amount=500000,
    )


def test_customer_self_reported_cash_booking_never_flips_inventory():
    """A customer's unverified cash entry must not move inventory - otherwise anyone
    locks arbitrary plots for a rupee. The payment row is still created."""
    svc, payments, inventory, booking = _pay_service()
    payments.create_payment.return_value = _row(method="cash", owner_role="customer",
                                                purpose="plot_booking", inventory_id="INV-9")

    record = svc.record_cash_payment(1, owner_id="C00001", owner_role="customer",
                                     purpose="plot_booking", inventory_id="INV-9")

    inventory.hold_for_kyc_review.assert_not_called()
    assert record.status == "paid"
    assert record.inventory_status is None


def test_cash_payment_without_booking_purpose_never_touches_inventory():
    svc, payments, inventory, booking = _pay_service()
    payments.create_payment.return_value = _row(method="cash", owner_role="broker",
                                                purpose="other", inventory_id=None)

    record = svc.record_cash_payment(1000, owner_id="B00001", owner_role="broker")

    inventory.hold_for_kyc_review.assert_not_called()
    assert record.inventory_status is None


def test_booking_flip_conflict_flags_manual_review_but_still_settles():
    svc, payments, inventory, booking = _pay_service()
    payments.create_payment.return_value = _row(method="cash", owner_role="broker",
                                                purpose="plot_booking", inventory_id="INV-TAKEN")
    inventory.hold_for_kyc_review.return_value = None  # already booked / sold / reserved

    record = svc.record_cash_payment(500000, owner_id="B00001", owner_role="broker",
                                     purpose="plot_booking", inventory_id="INV-TAKEN")

    assert record.status == "paid"                       # money is real - payment still settles
    assert record.inventory_status == "conflict"
    assert record.inventory_conflict_reason == "unit_not_available"
    payments.flag_manual_review.assert_called_once_with("pay1", "inventory_unavailable")
    booking.create_booking.assert_not_called()


def test_booking_flip_swallows_inventory_db_error():
    svc, payments, inventory, booking = _pay_service()
    payments.create_payment.return_value = _row(method="cash", owner_role="broker",
                                                purpose="plot_booking", inventory_id="INV-9")
    inventory.hold_for_kyc_review.side_effect = RuntimeError("db down")

    record = svc.record_cash_payment(500000, owner_id="B00001", owner_role="broker",
                                     purpose="plot_booking", inventory_id="INV-9")

    assert record.inventory_status == "conflict"
    assert record.inventory_conflict_reason == "inventory_update_failed"
    payments.flag_manual_review.assert_called_once_with("pay1", "inventory_update_failed")


def test_create_order_rejects_a_booking_for_an_already_taken_unit():
    svc, payments, inventory, booking = _pay_service()
    inventory.get_by_id.return_value = SimpleNamespace(id="INV-SOLD", status="sold")
    try:
        svc.create_order(500000, owner_id="C00001", owner_role="customer",
                         purpose="plot_booking", inventory_id="INV-SOLD")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "unit_not_available"
    payments.create_payment.assert_not_called()


def test_invalid_purpose_is_rejected():
    svc, _, _, _ = _pay_service()
    for fn in (
        lambda: svc.create_order(1000, owner_id="C00001", owner_role="customer", purpose="nope"),
        lambda: svc.record_cash_payment(1000, owner_id="C00001", owner_role="customer", purpose="nope"),
    ):
        try:
            fn()
            assert False, "expected ValueError"
        except ValueError as e:
            assert str(e) == "invalid_purpose"


def test_verify_payment_holds_unit_for_kyc_review_on_valid_signature():
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = True
    gw.retrieve_payment_session.return_value = {"status": "succeeded"}
    svc, payments, inventory, booking = _pay_service(gateway=gw)
    payments.get_by_zoho_session_id.return_value = _row(
        status="created", purpose="plot_booking", inventory_id="INV-9", zoho_payments_session_id="session_x")
    payments.update_payment_status.return_value = _row(
        status="paid", purpose="plot_booking", inventory_id="INV-9", zoho_payments_session_id="session_x")
    inventory.hold_for_kyc_review.return_value = _row(id="INV-9", status="pending_kyc_review")

    updated, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "5000.00", "sig_x", owner_id="C00001")

    assert verified is True
    inventory.hold_for_kyc_review.assert_called_once_with(id="INV-9", payment_id="pay1", customer_id="C00001")
    assert updated.inventory_status == "pending_kyc_review"
    assert updated.booking_id == "BKG-2026-000001"


def test_verify_payment_failed_signature_does_not_flip():
    gw = MagicMock()
    gw.verify_redirect_signature.return_value = False
    svc, payments, inventory, booking = _pay_service(gateway=gw)
    payments.get_by_zoho_session_id.return_value = _row(
        status="created", purpose="plot_booking", inventory_id="INV-9", zoho_payments_session_id="session_x")
    payments.update_payment_status.return_value = _row(
        status="failed", purpose="plot_booking", inventory_id="INV-9", zoho_payments_session_id="session_x")

    _, verified = svc.verify_payment("session_x", "pay_x", "succeeded", "5000.00", "sig_x", owner_id="C00001")

    assert verified is False
    inventory.hold_for_kyc_review.assert_not_called()


def test_webhook_retries_flip_for_a_booking_that_settled_without_locking():
    """verify_payment settled the payment but a transient error left the plot unheld.
    The captured webhook must still finish the hold, not bail at 'already settled'."""
    gw = MagicMock()
    gw.verify_webhook_signature.return_value = True
    svc, payments, inventory, booking = _pay_service(gateway=gw)
    payments.get_by_zoho_session_id.return_value = _row(
        id="pay1", status="paid", method="zoho", purpose="plot_booking",
        inventory_id="INV-9", zoho_payments_session_id="session_x")
    inventory.hold_for_kyc_review.return_value = _row(id="INV-9", status="pending_kyc_review")

    body = ('{"event":"payment.success","payload":{"payment":'
            '{"payment_id":"pay_x","payments_session_id":"session_x"}}}')
    result = svc.handle_webhook(body.encode(), "sig")

    assert result == "ignored_already_settled"
    inventory.hold_for_kyc_review.assert_called_once_with(id="INV-9", payment_id="pay1", customer_id="C00001")


# --------------------------------------------------------------------------- #
# document upload safety-net: only ever holds the payment's OWN unit           #
# --------------------------------------------------------------------------- #
def test_document_safety_net_holds_only_the_payments_own_unit():
    from DivineService.service_document import serviceDocument
    inv = MagicMock()
    inv.hold_for_kyc_review.return_value = SimpleNamespace(id="INV-PAID", status="pending_kyc_review")
    svc = serviceDocument(MagicMock(), inventory_persistence=inv)

    doc = SimpleNamespace()
    payment = SimpleNamespace(id="pay1", purpose="plot_booking", inventory_id="INV-PAID")
    # client tries to smuggle a different plot in the form field - must be ignored
    svc._confirm_inventory_booked(doc, payment=payment, client_inventory_id="INV-SOMEONE-ELSE", owner_id="C1")

    inv.hold_for_kyc_review.assert_called_once_with(id="INV-PAID", payment_id="pay1", customer_id="C1")
    assert doc.inventory_id == "INV-PAID"
    assert doc.inventory_status == "pending_kyc_review"


def test_document_safety_net_does_nothing_for_a_non_booking_payment():
    from DivineService.service_document import serviceDocument
    inv = MagicMock()
    svc = serviceDocument(MagicMock(), inventory_persistence=inv)

    doc = SimpleNamespace()
    payment = SimpleNamespace(id="pay1", purpose="other", inventory_id=None)
    svc._confirm_inventory_booked(doc, payment=payment, client_inventory_id="INV-X", owner_id="C1")

    inv.hold_for_kyc_review.assert_not_called()
    assert doc.inventory_status is None


# --------------------------------------------------------------------------- #
# persistenceInventory.book_unit / unbook_unit against real sqlite             #
# --------------------------------------------------------------------------- #
def _seed_unit(status="available"):
    PersistenceDB().create_tables()
    inv = persistenceInventory()
    uid = f"seed-{uuid.uuid4().hex[:8]}"
    inv.upsert_unit(id=uid, project_name="Suraksha Enclave", city="Sonipat",
                    unit_number=uid[-6:], area_sqmt=100, area_sqyd=120, status=status)
    return inv, uid


def test_book_unit_locks_available_and_is_idempotent_for_same_payment():
    inv, uid = _seed_unit()
    first = inv.book_unit(id=uid, payment_id="pay-A", customer_id="C00001")
    assert first is not None and first.status == "booked"
    assert first.booked_payment_id == "pay-A" and first.booked_by == "C00001"

    again = inv.book_unit(id=uid, payment_id="pay-A", customer_id="C00001")
    assert again is not None and again.status == "booked"          # no-op, still ours


def test_book_unit_refuses_a_unit_booked_by_a_different_payment():
    inv, uid = _seed_unit()
    assert inv.book_unit(id=uid, payment_id="pay-A", customer_id="C1") is not None
    assert inv.book_unit(id=uid, payment_id="pay-B", customer_id="C2") is None   # locked


def test_book_unit_returns_none_for_unknown_id():
    inv, _ = _seed_unit()
    assert inv.book_unit(id="does-not-exist", payment_id="pay-A", customer_id="C1") is None


def test_unbook_unit_returns_unit_to_available():
    inv, uid = _seed_unit()
    inv.book_unit(id=uid, payment_id="pay-A", customer_id="C1")
    released = inv.unbook_unit(id=uid)
    assert released is not None and released.status == "available"
    assert released.booked_payment_id is None
    assert inv.unbook_unit(id=uid) is None                         # not booked anymore


def test_booked_unit_drops_out_of_default_search():
    inv, uid = _seed_unit()
    ids_before = {u.id for u in inv.search(city="Sonipat", limit=200)}
    assert uid in ids_before
    inv.book_unit(id=uid, payment_id="pay-A", customer_id="C1")
    ids_after = {u.id for u in inv.search(city="Sonipat", limit=200)}
    assert uid not in ids_after


# --------------------------------------------------------------------------- #
# serviceInventory repair helpers                                              #
# --------------------------------------------------------------------------- #
def _inv_service():
    persistence = MagicMock()
    return serviceInventory(
        persistence=persistence, market_trend_persistence=MagicMock(),
        chatbot_persistence=MagicMock(), gemini=MagicMock(), groq=MagicMock(),
    ), persistence


def test_service_book_unit_repair_maps_miss_to_unit_not_available():
    svc, persistence = _inv_service()
    persistence.book_unit.return_value = None
    try:
        svc.book_unit_repair("INV-9", payment_id="pay-A")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "unit_not_available"


def test_service_unbook_unit_maps_miss_to_unit_not_booked():
    svc, persistence = _inv_service()
    persistence.unbook_unit.return_value = None
    try:
        svc.unbook_unit("INV-9")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "unit_not_booked"


def test_service_unbook_unit_formats_result():
    svc, persistence = _inv_service()
    persistence.unbook_unit.return_value = SimpleNamespace(
        id="INV-9", status="available", booked_at=None, booked_payment_id=None, booked_by=None)
    out = svc.unbook_unit("INV-9")
    assert out == {"id": "INV-9", "status": "available", "booked_at": None,
                   "booked_payment_id": None, "booked_by": None}


# --------------------------------------------------------------------------- #
# API: broker-gated repair endpoints                                           #
# --------------------------------------------------------------------------- #
_BROKER_TOKEN = None
_CUSTOMER_TOKEN = None


def setup_module(module):
    global _BROKER_TOKEN, _CUSTOMER_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    # test_inventory_api.py leaves the shared _inventory_service singleton with a
    # stub persistence (it drives every case through a fake service instead). Our
    # API cases hit the real service, so restore a working persistence here.
    _api_inventory_service._persistence = persistenceInventory()

    client.post("/broker/signup", json={"username": "bookbroker", "password": "strongpassword", "phone": "9876500016", "project": "suraksha-enclave"})
    _BROKER_TOKEN = client.post("/broker/login", json={"username": "bookbroker", "password": "strongpassword"}).json()["access_token"]
    client.post("/customer/signup", json={"username": "bookcust", "password": "strongpassword", "phone": "9876500017"})
    _CUSTOMER_TOKEN = client.post("/customer/login", json={"username": "bookcust", "password": "strongpassword"}).json()["access_token"]


def teardown_module(module):
    # This file seeds inventory / payment rows into the shared test_db.sqlite. Later
    # test files that assert on absolute row counts (e.g. distinct_plot_sizes) would
    # otherwise see them, and their setup_module os.remove() can be a no-op while
    # this process still holds sqlite connections. Clear what we added.
    from Divinepersistence.persistence_db import engine
    from sqlalchemy import text as _text
    try:
        with engine.begin() as conn:
            conn.execute(_text("DELETE FROM divine_project_inventory"))
            conn.execute(_text("DELETE FROM divine_payments"))
    except Exception:
        pass


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_api_unit(status="available"):
    inv = persistenceInventory()
    uid = f"api-{uuid.uuid4().hex[:8]}"
    inv.upsert_unit(id=uid, project_name="Suraksha Enclave", city="Sonipat",
                    unit_number=uid[-6:], area_sqmt=100, area_sqyd=120, status=status)
    return uid


def test_repair_book_and_unbook_roundtrip_as_broker():
    uid = _seed_api_unit()
    r = client.post(f"/inventory/{uid}/book", json={"payment_id": "pay-A", "reason": "manual"}, headers=_h(_BROKER_TOKEN))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "booked"

    r2 = client.post(f"/inventory/{uid}/book", json={"payment_id": "pay-B"}, headers=_h(_BROKER_TOKEN))
    assert r2.status_code == 409                                   # locked by pay-A

    r3 = client.post(f"/inventory/{uid}/unbook", json={"reason": "refunded"}, headers=_h(_BROKER_TOKEN))
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "available"

    r4 = client.post(f"/inventory/{uid}/unbook", json={}, headers=_h(_BROKER_TOKEN))
    assert r4.status_code == 409                                   # not booked


def test_repair_endpoints_reject_customers_and_anonymous():
    uid = _seed_api_unit()
    assert client.post(f"/inventory/{uid}/book", json={}, headers=_h(_CUSTOMER_TOKEN)).status_code == 403
    assert client.post(f"/inventory/{uid}/unbook", json={}).status_code == 401

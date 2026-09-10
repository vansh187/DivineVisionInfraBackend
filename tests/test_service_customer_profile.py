import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import pytest

from DivineService.service_customer_profile import serviceCustomerProfile


def _persistence(**overrides):
    p = MagicMock()
    p.get_customer.return_value = overrides.get(
        "customer",
        SimpleNamespace(
            id="C00001", username="rahul", email="rahul@example.com",
            phone="+91 98765 43210", first_name="Rahul", last_name="Sharma",
        ),
    )
    p.get_identity_extract.return_value = overrides.get("identity", {})
    booking = overrides.get("booking", None)
    p.get_booking_application.return_value = booking
    # Multi-plot: the profile assembler now lists every booking. Default to whatever
    # the single `booking` override supplies (so existing single-booking tests keep
    # exercising the real code path); an explicit `bookings` override wins.
    if "bookings" in overrides:
        p.list_booking_applications.return_value = overrides.get("bookings")
    else:
        p.list_booking_applications.return_value = [booking] if isinstance(booking, dict) else []
    p.get_amount_received.return_value = overrides.get("amount_received", 0)
    p.get_amount_received_for_payment.return_value = overrides.get(
        "amount_received_for_payment", 0
    )
    p.count_booking_projects.return_value = overrides.get("project_count", 1)
    p.get_amount_received_for_project.return_value = overrides.get(
        "amount_received_for_project", 0
    )
    return p


class _NoMilestones:
    """Keeps these tests focused on the form-data fallback path; the milestone-backed
    enrichment is covered by tests/test_installment_payments.py."""
    def enriched_schedule(self, *a, **k):
        return {"rows": [], "next_due": None}

    def amount_received_rupees(self, *a, **k):
        return None


def _service(**overrides):
    p = _persistence(**overrides)
    return serviceCustomerProfile(p, milestone_service=_NoMilestones()), p


def test_non_customer_role_is_rejected():
    svc, _ = _service()
    with pytest.raises(PermissionError):
        svc.get_profile("B00001", "broker")


def test_missing_customer_raises_lookup_error():
    svc, p = _service()
    p.get_customer.return_value = None
    with pytest.raises(LookupError):
        svc.get_profile("C09999", "customer")


def test_base_identity_from_customer_row():
    svc, _ = _service()
    dto = svc.get_profile("C00001", "customer")
    assert dto.customer_id == "C00001"
    assert dto.first_name == "Rahul"
    assert dto.last_name == "Sharma"
    assert dto.full_name == "Rahul Sharma"
    assert dto.email == "rahul@example.com"
    assert dto.booking.has_booking is False


def test_identity_extract_fills_gender_dob_age_and_address():
    identity = {
        "name": "Rahul Sharma",
        "gender": "M",
        "dob": "12-04-1990",
        "house": "Kothi No. 11",
        "street": "Ganeshwar Dham Road",
        "landmark": "Karol Bagh",
        "vtc": "New Delhi",
        "state": "Delhi",
        "pincode": "110005",
    }
    svc, _ = _service(customer=SimpleNamespace(
        id="C00001", username="rahul", email=None, phone=None,
        first_name=None, last_name=None,
    ), identity=identity)
    dto = svc.get_profile("C00001", "customer")
    assert dto.gender == "male"
    assert dto.date_of_birth == "1990-04-12"
    assert dto.age == datetime.now(timezone.utc).year - 1990 - (
        (datetime.now(timezone.utc).month, datetime.now(timezone.utc).day) < (4, 12)
    )
    assert dto.first_name == "Rahul"
    assert dto.last_name == "Sharma"
    assert dto.address.city == "New Delhi"
    assert dto.address.pincode == "110005"
    assert "Kothi No. 11" in dto.address_text
    assert dto.address_text.endswith("110005")


def test_booking_derived_from_document_form_data_and_payments():
    form = {
        "projectId": "ops-divine-greens",
        "projectName": "OPS Divine Greens",
        "unitNumber": "204",
        "plot_area_sq_yd": "131.43",
        "unitType": "Residential Plot",
        "bookingDate": "2025-01-10",
        "totalConsideration": "17,74,305",
        "payment_schedule": [
            {"label": "On Booking", "percent": 10, "due_days": 0,
             "due_date": "2025-01-10", "amount": 177431, "status": "paid"},
        ],
    }
    svc, _ = _service(
        booking={
            "id": "doc-1", "project_id": "ops-divine-greens",
            "document_id": "doc-1",
            "inventory_id": "inv-204",
            "payment_id": "pay-204",
            "booking_payment_amount": 177431,
            "payment_method": "razorpay",
            "razorpay_order_id": "order_204",
            "razorpay_payment_id": "rzp_204",
            "payment_created_date": datetime(2025, 1, 10, 12, 30, tzinfo=timezone.utc),
            "form_data": form, "created_date": datetime(2025, 1, 10, tzinfo=timezone.utc),
        },
        amount_received=700000,
    )
    dto = svc.get_profile("C00001", "customer")
    b = dto.booking
    assert b.has_booking is True
    assert b.id == "doc-1"
    assert b.document_id == "doc-1"
    assert b.inventory_id == "inv-204"
    assert b.project_id == "ops-divine-greens"
    assert b.project_name == "OPS Divine Greens"
    assert b.unit_number == "204"
    assert b.plot_area_sq_yd == "131.43"
    assert b.unit_type == "Residential Plot"
    assert b.booking_date == "2025-01-10"
    assert b.total_consideration == 1774305
    assert b.amount_received == 700000
    assert b.payment_id == "pay-204"
    assert b.booking_payment_amount == 177431
    assert b.payment_method == "razorpay"
    assert b.razorpay_order_id == "order_204"
    assert b.razorpay_payment_id == "rzp_204"
    assert b.payment_created_date == "2025-01-10"
    assert b.payment_schedule[0].label == "On Booking"
    assert b.payment_schedule[0].amount == 177431


def test_booking_date_falls_back_to_document_created_date():
    svc, _ = _service(booking={
        "id": "doc-1", "project_id": None, "form_data": {},
        "created_date": datetime(2025, 3, 4, tzinfo=timezone.utc),
    })
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.booking_date == "2025-03-04"


def test_amount_received_without_booking_document():
    svc, _ = _service(amount_received=250000)
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.has_booking is False
    assert dto.booking.amount_received == 250000
    assert dto.bookings == []


def test_malformed_identity_blob_does_not_break_profile():
    svc, p = _service()
    p.get_identity_extract.side_effect = RuntimeError("boom")
    dto = svc.get_profile("C00001", "customer")
    assert dto.customer_id == "C00001"
    assert dto.gender is None
    assert dto.address is None


def test_bare_year_dob_yields_age_only():
    svc, _ = _service(identity={"dob": "1990"})
    dto = svc.get_profile("C00001", "customer")
    assert dto.date_of_birth is None
    assert dto.age == datetime.now(timezone.utc).year - 1990


def test_payment_schedule_keeps_zero_due_days_and_percent():
    form = {
        "payment_schedule": [
            {"label": "On Booking", "percent": 0, "due_days": 0, "amount": 0},
        ],
    }
    svc, _ = _service(booking={
        "id": "doc-1", "project_id": "p1", "form_data": form,
        "created_date": datetime(2025, 1, 10, tzinfo=timezone.utc),
    })
    row = svc.get_profile("C00001", "customer").booking.payment_schedule[0]
    assert row.due_days == 0
    assert row.percent == 0
    assert row.amount == 0


def test_booking_date_from_string_created_date_is_reduced_to_iso_date():
    # SQLite returns timestamptz columns as strings, not datetime objects.
    svc, _ = _service(booking={
        "id": "doc-1", "project_id": None, "form_data": {},
        "created_date": "2025-03-04 00:00:00+00:00",
    })
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.booking_date == "2025-03-04"


def test_lakh_suffix_amount_in_form_data_is_normalised():
    svc, _ = _service(booking={
        "id": "doc-1", "project_id": "p1",
        "form_data": {"total_consideration": "17.74 lakh"},
        "created_date": datetime(2025, 1, 10, tzinfo=timezone.utc),
    })
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.total_consideration == 1774000


def test_amount_received_is_scoped_to_project_when_customer_has_multiple_bookings():
    svc, p = _service(
        booking={
            "id": "doc-2", "project_id": "project-b", "form_data": {},
            "created_date": datetime(2025, 6, 1, tzinfo=timezone.utc),
        },
        amount_received=1_000_000,          # all-time paid across every booking
        project_count=2,                    # more than one booking project -> ambiguous
        amount_received_for_project=400_000,  # only what is linked to project-b
    )
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.amount_received == 400_000
    p.get_amount_received_for_project.assert_called_once_with("C00001", "project-b")


def test_amount_received_uses_full_balance_when_only_one_booking_project():
    svc, _ = _service(
        booking={
            "id": "doc-1", "project_id": "project-a", "form_data": {},
            "created_date": datetime(2025, 1, 10, tzinfo=timezone.utc),
        },
        amount_received=700_000,
        project_count=1,
        amount_received_for_project=0,
    )
    dto = svc.get_profile("C00001", "customer")
    assert dto.booking.amount_received == 700_000

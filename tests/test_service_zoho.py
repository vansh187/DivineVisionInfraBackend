from unittest.mock import MagicMock

from DivineService.service_zoho import serviceZoho


def test_push_booking_contact_payload_includes_approved_booking_context():
    zoho = serviceZoho()
    zoho._upsert = MagicMock(return_value=True)

    assert zoho.push_booking_contact(
        customer_id="C00001",
        first_name="Rehan",
        last_name="Sharma",
        email="rehan@example.com",
        phone="9999999998",
        inventory_id="INV-1",
        payment_id="pay1",
        purpose="plot_booking",
        payment_method="razorpay",
        booking_id="BKG-2026-000001",
        project_name="Divine Greens",
        unit_number="A-112",
        booking_amount=2500000,
        booking_status="booked",
        kyc_status="verified",
    ) is True

    zoho._upsert.assert_called_once()
    module, record, duplicate_fields = zoho._upsert.call_args.args
    assert module == "Contacts"
    assert duplicate_fields == ["Email", "Phone"]
    assert record["First_Name"] == "Rehan"
    assert record["Last_Name"] == "Sharma"
    assert record["Email"] == "rehan@example.com"
    assert record["Phone"] == "9999999998"
    assert record["Lead_Source"] == "Website Plot Booking"
    assert "booking_id=BKG-2026-000001" in record["Description"]
    assert "project_name=Divine Greens" in record["Description"]
    assert "unit_number=A-112" in record["Description"]
    assert "booking_status=booked" in record["Description"]
    assert "kyc_status=verified" in record["Description"]

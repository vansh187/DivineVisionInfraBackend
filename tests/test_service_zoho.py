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
    assert zoho._upsert.call_args.kwargs == {"return_id": True}
    assert module == "Contacts"
    assert duplicate_fields == ["Email", "Phone"]
    assert record["First_Name"] == "Rehan"
    assert record["Last_Name"] == "Sharma"
    assert record["Email"] == "rehan@example.com"
    assert record["Phone"] == "9999999998"
    assert record["Mobile"] == "9999999998"
    assert record["Lead_Source"] == "Website Plot Booking"
    assert "booking_id=BKG-2026-000001" in record["Description"]
    assert "project_name=Divine Greens" in record["Description"]
    assert "unit_number=A-112" in record["Description"]
    assert "booking_status=booked" in record["Description"]
    assert "kyc_status=verified" in record["Description"]


def test_push_booking_contact_attaches_application_pdf_after_successful_upsert():
    zoho = serviceZoho()
    zoho._upsert = MagicMock(return_value="zcrm_12345")
    zoho._attach_booking_application = MagicMock()

    result = zoho.push_booking_contact(
        customer_id="C00001",
        email="rehan@example.com",
        phone="9999999998",
        payment_id="pay1",
    )

    assert result is True
    zoho._attach_booking_application.assert_called_once_with("zcrm_12345", "pay1", "C00001")


def test_push_booking_contact_skips_attachment_when_upsert_fails():
    zoho = serviceZoho()
    zoho._upsert = MagicMock(return_value=None)
    zoho._attach_booking_application = MagicMock()

    result = zoho.push_booking_contact(
        customer_id="C00001",
        email="rehan@example.com",
        phone="9999999998",
        payment_id="pay1",
    )

    assert result is False
    zoho._attach_booking_application.assert_not_called()


def test_push_booking_contact_skips_attachment_without_payment_id():
    zoho = serviceZoho()
    zoho._upsert = MagicMock(return_value="zcrm_12345")
    zoho._attach_booking_application = MagicMock()

    result = zoho.push_booking_contact(
        customer_id="C00001",
        email="rehan@example.com",
        phone="9999999998",
    )

    assert result is True
    zoho._attach_booking_application.assert_not_called()


def test_attach_booking_application_uploads_pdf_bytes_when_found(monkeypatch):
    zoho = serviceZoho()
    zoho.upload_attachment = MagicMock(return_value=True)

    fake_document_service = MagicMock()
    fake_document_service.get_booking_application_bytes.return_value = (
        b"%PDF-1.4 ...", "booking-application-C00001.pdf",
    )
    import DivineService.service_document as service_document_module
    monkeypatch.setattr(service_document_module, "serviceDocument", lambda: fake_document_service)

    zoho._attach_booking_application("zcrm_12345", "pay1", "C00001")

    fake_document_service.get_booking_application_bytes.assert_called_once_with("pay1")
    zoho.upload_attachment.assert_called_once_with(
        "Contacts", "zcrm_12345", b"%PDF-1.4 ...", "booking-application-C00001.pdf",
    )


def test_attach_booking_application_skips_when_no_pdf_found(monkeypatch):
    zoho = serviceZoho()
    zoho.upload_attachment = MagicMock()

    fake_document_service = MagicMock()
    fake_document_service.get_booking_application_bytes.return_value = (None, None)
    import DivineService.service_document as service_document_module
    monkeypatch.setattr(service_document_module, "serviceDocument", lambda: fake_document_service)

    zoho._attach_booking_application("zcrm_12345", "pay1", "C00001")

    zoho.upload_attachment.assert_not_called()


def test_upload_attachment_skipped_without_record_id_or_bytes():
    zoho = serviceZoho()
    assert zoho.upload_attachment("Contacts", None, b"bytes", "f.pdf") is False
    assert zoho.upload_attachment("Contacts", "zcrm_1", b"", "f.pdf") is False

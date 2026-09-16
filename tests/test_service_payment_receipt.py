from DivineService.service_payment_receipt import (
    generate_receipt_pdf, receipt_filename, _amount_words_line, _inr, _invoice_number,
)


def test_amount_words_line_includes_paise_when_present():
    assert _amount_words_line(3763.58) == (
        "Indian Rupee Three Thousand Seven Hundred Sixty-Three and Fifty-Eight Paise Only"
    )


def test_amount_words_line_omits_paise_when_whole():
    assert _amount_words_line(2500000) == "Indian Rupee Twenty-Five Lakh Only"


def test_inr_formats_paise_only_when_nonzero():
    assert _inr(3763.58) == "Rs.3,763.58"
    assert _inr(2500000) == "Rs.25,00,000"


def test_invoice_number_derived_from_payment_id():
    assert _invoice_number("550e8400-e29b-41d4-a716-446655440000") == "INV-440000"
    assert _invoice_number(None) == "INV-000000"


def test_receipt_filename_sanitizes_payment_id():
    assert receipt_filename("pay/1") == "payment-receipt-pay_1.pdf"


def test_generate_receipt_pdf_embeds_booking_and_form_data_fields():
    payment = {
        "id": "pay1", "amount": 3763.58, "method": "razorpay",
        "purpose": "plot_booking", "created_date": "2026-07-14", "owner_id": "C00001",
    }
    customer = {"first_name": "Vinay", "last_name": "Tyagi", "phone": "6006753400"}
    booking = {"unit_number": "A81", "project_name": "OPS Divine Greens"}
    form_data = {"plot_area_sq_yd": "75.94", "applicant_address": "R/O- Barana, Panipat"}

    pdf_bytes = generate_receipt_pdf(payment, customer=customer, booking=booking, form_data=form_data)

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000


def test_generate_receipt_pdf_never_raises_on_missing_context():
    payment = {"id": "pay1", "amount": 1000, "owner_id": "C00001"}
    pdf_bytes = generate_receipt_pdf(payment)
    assert pdf_bytes.startswith(b"%PDF-")

import base64
import os
import requests
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_document import serviceDocument, _safe_path_segment, _pdf_safe_text

# The smallest possible valid PNG (1x1, transparent) - a real, decodable image so
# fpdf2's embedding path (which uses Pillow) is exercised for real, not just mocked.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _service(configured=True):
    persistence = MagicMock()
    svc = serviceDocument(persistence)
    if configured:
        svc._supabase_url = "https://fake.supabase.co"
        svc._service_key = "fake-service-key"
    else:
        svc._supabase_url = None
        svc._service_key = None
    svc._bucket = "documents"
    return svc, persistence


# ---------- pure helpers ----------

def test_safe_path_segment_strips_unsafe_chars():
    assert _safe_path_segment("address proof/v2?") == "address_proof_v2"


def test_safe_path_segment_empty_input_falls_back():
    assert _safe_path_segment("???") == "document"
    assert _safe_path_segment("") == "document"


def test_safe_path_segment_truncates_long_input():
    assert len(_safe_path_segment("a" * 500)) <= 50


def test_pdf_safe_text_replaces_non_latin1_chars():
    # FPDF's core font can't render this - must not raise, must return something renderable
    result = _pdf_safe_text("Amount: ₹500 — café")
    result.encode("latin-1")  # would raise if any non-latin1 char slipped through


# ---------- _render_pdf ----------

def test_render_pdf_handles_unicode_form_data_without_raising():
    svc, _ = _service()
    pdf_bytes = svc._render_pdf("kyc", {"name": "José 你好", "note": "₹1,000"})
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")


def test_render_pdf_handles_empty_form_data():
    svc, _ = _service()
    pdf_bytes = svc._render_pdf("kyc", {})
    assert pdf_bytes.startswith(b"%PDF")


# ---------- _upload_to_storage ----------

def test_upload_to_storage_raises_when_not_configured():
    svc, _ = _service(configured=False)
    try:
        svc._upload_to_storage("C00001/kyc_1.pdf", b"data")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_not_configured"


@patch("DivineService.service_document.requests.post")
def test_upload_to_storage_raises_on_non_2xx(mock_post):
    mock_post.return_value = MagicMock(status_code=400)
    svc, _ = _service()
    try:
        svc._upload_to_storage("C00001/kyc_1.pdf", b"data")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_upload_failed:400"


@patch("DivineService.service_document.requests.post")
def test_upload_to_storage_raises_on_network_error(mock_post):
    mock_post.side_effect = requests.exceptions.ConnectionError("boom")
    svc, _ = _service()
    try:
        svc._upload_to_storage("C00001/kyc_1.pdf", b"data")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_unreachable"


@patch("DivineService.service_document.requests.post")
def test_upload_to_storage_succeeds_on_201(mock_post):
    mock_post.return_value = MagicMock(status_code=201)
    svc, _ = _service()
    svc._upload_to_storage("C00001/kyc_1.pdf", b"data")  # must not raise


# ---------- _sign_url ----------

def test_sign_url_raises_when_not_configured():
    svc, _ = _service(configured=False)
    try:
        svc._sign_url("C00001/kyc_1.pdf")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_not_configured"


@patch("DivineService.service_document.requests.post")
def test_sign_url_raises_on_non_200(mock_post):
    mock_post.return_value = MagicMock(status_code=500)
    svc, _ = _service()
    try:
        svc._sign_url("C00001/kyc_1.pdf")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_sign_failed:500"


@patch("DivineService.service_document.requests.post")
def test_sign_url_raises_on_invalid_json(mock_post):
    resp = MagicMock(status_code=200)
    resp.json.side_effect = ValueError("not json")
    mock_post.return_value = resp
    svc, _ = _service()
    try:
        svc._sign_url("C00001/kyc_1.pdf")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_sign_failed:invalid_response"


@patch("DivineService.service_document.requests.post")
def test_sign_url_raises_when_response_missing_signed_url(mock_post):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"unexpected": "shape"}
    mock_post.return_value = resp
    svc, _ = _service()
    try:
        svc._sign_url("C00001/kyc_1.pdf")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_sign_failed:no_url"


@patch("DivineService.service_document.requests.post")
def test_sign_url_returns_absolute_url_on_success(mock_post):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"signedURL": "/object/sign/documents/C00001/kyc_1.pdf?token=abc"}
    mock_post.return_value = resp
    svc, _ = _service()
    url = svc._sign_url("C00001/kyc_1.pdf")
    assert url == "https://fake.supabase.co/storage/v1/object/sign/documents/C00001/kyc_1.pdf?token=abc"


# ---------- generate() cleanup-on-failure behavior ----------

@patch.object(serviceDocument, "_sign_url")
@patch.object(serviceDocument, "_delete_from_storage")
@patch.object(serviceDocument, "_upload_to_storage")
def test_generate_cleans_up_storage_object_when_persistence_fails(mock_upload, mock_delete, mock_sign):
    from DivineDTO.models import DocumentGenerateRequestDTO

    persistence = MagicMock()
    persistence.create_document.side_effect = RuntimeError("db exploded")
    svc = serviceDocument(persistence)
    svc._supabase_url = "https://fake.supabase.co"
    svc._service_key = "fake-key"

    dto = DocumentGenerateRequestDTO(document_type="kyc", form_data={"name": "A"})
    try:
        svc.generate(dto, owner_id="C00001", owner_role="customer")
        assert False, "expected the persistence error to propagate"
    except RuntimeError as e:
        assert str(e) == "db exploded"

    mock_delete.assert_called_once()
    mock_sign.assert_not_called()  # never got far enough to mint a signed URL


@patch("DivineService.service_document.requests.delete")
def test_delete_from_storage_never_raises_even_on_network_error(mock_delete):
    mock_delete.side_effect = requests.exceptions.ConnectionError("boom")
    svc, _ = _service()
    svc._delete_from_storage("C00001/kyc_1.pdf")  # must swallow the error, not raise


# ---------- _render_pdf with attachments ----------

def test_render_pdf_embeds_valid_image_attachment():
    svc, _ = _service()
    pdf_bytes = svc._render_pdf("booking_application", {"name": "A"}, {"Aadhaar Card - Front": _TINY_PNG})
    assert pdf_bytes.startswith(b"%PDF")


def test_render_pdf_falls_back_gracefully_on_corrupt_attachment():
    svc, _ = _service()
    # Must not raise - a bad attachment shouldn't fail a document that otherwise succeeded.
    pdf_bytes = svc._render_pdf("booking_application", {"name": "A"}, {"Bad": b"not-an-image"})
    assert pdf_bytes.startswith(b"%PDF")


def test_render_pdf_without_attachments_unchanged():
    svc, _ = _service()
    pdf_bytes = svc._render_pdf("kyc", {"name": "A"})
    assert pdf_bytes.startswith(b"%PDF")


# ---------- _download_from_storage ----------

def test_download_from_storage_raises_when_not_configured():
    svc, _ = _service(configured=False)
    try:
        svc._download_from_storage("C00001/x.jpg")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_not_configured"


@patch("DivineService.service_document.requests.get")
def test_download_from_storage_raises_on_non_200(mock_get):
    mock_get.return_value = MagicMock(status_code=404)
    svc, _ = _service()
    try:
        svc._download_from_storage("C00001/x.jpg")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_download_failed:404"


@patch("DivineService.service_document.requests.get")
def test_download_from_storage_raises_on_network_error(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("boom")
    svc, _ = _service()
    try:
        svc._download_from_storage("C00001/x.jpg")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "storage_unreachable"


@patch("DivineService.service_document.requests.get")
def test_download_from_storage_returns_bytes_on_success(mock_get):
    mock_get.return_value = MagicMock(status_code=200, content=b"filedata")
    svc, _ = _service()
    assert svc._download_from_storage("C00001/x.jpg") == b"filedata"


# ---------- generate() gating on booking_application ----------

def test_generate_booking_application_raises_when_documents_missing():
    from DivineDTO.models import DocumentGenerateRequestDTO

    persistence = MagicMock()
    persistence.get_latest_by_owner_and_type.return_value = None
    svc = serviceDocument(persistence)

    dto = DocumentGenerateRequestDTO(document_type="booking_application", form_data={"name": "A"})
    try:
        svc.generate(dto, owner_id="C00001", owner_role="customer")
        assert False, "expected ValueError"
    except ValueError as e:
        message = str(e)
        assert message.startswith("documents_incomplete:")
        assert "aadhaar_front" in message
        assert "aadhaar_back" in message
        assert "pan_card" in message


@patch.object(serviceDocument, "_sign_url", return_value="https://fake.supabase.co/signed")
@patch.object(serviceDocument, "_upload_to_storage")
@patch.object(serviceDocument, "_download_from_storage", return_value=_TINY_PNG)
def test_generate_booking_application_succeeds_and_embeds_when_all_present(mock_download, mock_upload, mock_sign):
    from DivineDTO.models import DocumentGenerateRequestDTO

    persistence = MagicMock()
    persistence.get_latest_by_owner_and_type.return_value = MagicMock(storage_path="C00001/aadhaar_front_x.jpg")
    persistence.create_document.return_value = MagicMock(
        id="doc1", owner_id="C00001", owner_role="customer",
        document_type="booking_application", status="generated", created_date=None,
    )
    svc = serviceDocument(persistence)

    dto = DocumentGenerateRequestDTO(document_type="booking_application", form_data={"name": "A"})
    doc, signed_url, expires_in = svc.generate(dto, owner_id="C00001", owner_role="customer")

    assert doc.id == "doc1"
    assert mock_download.call_count == 3  # aadhaar_front, aadhaar_back, pan_card
    mock_upload.assert_called_once()


def test_generate_non_gated_document_type_skips_document_lookup():
    from DivineDTO.models import DocumentGenerateRequestDTO

    persistence = MagicMock()
    persistence.create_document.return_value = MagicMock(
        id="doc1", owner_id="C00001", owner_role="customer", document_type="kyc", status="generated", created_date=None,
    )
    svc = serviceDocument(persistence)
    svc._supabase_url = "https://fake.supabase.co"
    svc._service_key = "fake-key"

    with patch.object(serviceDocument, "_upload_to_storage"), patch.object(serviceDocument, "_sign_url", return_value="url"):
        dto = DocumentGenerateRequestDTO(document_type="kyc", form_data={"name": "A"})
        svc.generate(dto, owner_id="C00001", owner_role="customer")

    persistence.get_latest_by_owner_and_type.assert_not_called()


# ---------- upload_pan_photo / upload_aadhaar_photo ----------

def _fake_photo_bytes() -> bytes:
    import cv2
    import numpy as np

    blank = np.zeros((20, 20, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", blank)
    return encoded.tobytes()


@patch.object(serviceDocument, "_sign_url", return_value="url")
@patch.object(serviceDocument, "_upload_to_storage")
def test_upload_pan_photo_uses_pan_card_document_type(mock_upload, mock_sign):
    persistence = MagicMock()
    persistence.create_document.return_value = MagicMock(id="doc1")
    svc = serviceDocument(persistence)
    svc._supabase_url = "https://fake.supabase.co"
    svc._service_key = "fake-key"

    svc.upload_pan_photo(_fake_photo_bytes(), "image/jpeg", owner_id="C00001", owner_role="customer")

    _, kwargs = persistence.create_document.call_args
    assert kwargs["document_type"] == "pan_card"


def test_upload_aadhaar_photo_rejects_invalid_side():
    svc, _ = _service()
    try:
        svc.upload_aadhaar_photo(b"data", "image/jpeg", "sideways", owner_id="C00001", owner_role="customer")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_side"


# ---------- get() ownership check ----------

def test_get_rejects_matching_owner_id_but_different_owner_role():
    # owner_id alone can't currently collide across roles (customer/broker IDs use
    # distinct prefixes), but the authorization check itself should not rely on that
    # incidentally - a document belonging to a broker must not be servable to a customer
    # whose id happens to match, in case the ID scheme is ever changed.
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(owner_id="X00001", owner_role="broker", storage_path="p")
    try:
        svc.get("doc1", requester_id="X00001", requester_role="customer")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_get_succeeds_when_owner_id_and_role_both_match():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(owner_id="C00001", owner_role="customer", storage_path="p")
    with patch.object(serviceDocument, "_sign_url", return_value="url"):
        doc, signed_url, expires_in = svc.get("doc1", requester_id="C00001", requester_role="customer")
    assert signed_url == "url"


def test_get_signs_against_the_bucket_recorded_on_the_row():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(
        owner_id="C00001", owner_role="customer", storage_path="p", storage_bucket="Booking_Forms"
    )
    with patch.object(serviceDocument, "_sign_url", return_value="url") as mock_sign:
        svc.get("doc1", requester_id="C00001", requester_role="customer")
    assert mock_sign.call_args.kwargs["bucket"] == "Booking_Forms"


def test_get_falls_back_to_default_bucket_when_row_has_no_storage_bucket():
    # Rows created before the storage_bucket column existed (or before a migration backfilled
    # it) have it NULL - those all predate Booking_Forms, so falling back to the default bucket
    # is correct, not an inference off some unrelated column.
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(
        owner_id="C00001", owner_role="customer", storage_path="p", storage_bucket=None
    )
    with patch.object(serviceDocument, "_sign_url", return_value="url") as mock_sign:
        svc.get("doc1", requester_id="C00001", requester_role="customer")
    assert mock_sign.call_args.kwargs["bucket"] == svc._bucket


# ---------- upload_booking_application() ----------

_PDF_BYTES = b"%PDF-1.4\n%fake pdf content\n%%EOF"


def _booking_service(payment=None):
    persistence = MagicMock()
    payment_persistence = MagicMock()
    payment_persistence.get_by_id.return_value = payment
    svc = serviceDocument(persistence, payment_persistence)
    svc._supabase_url = "https://fake.supabase.co"
    svc._service_key = "fake-service-key"
    svc._bucket = "documents"
    svc._booking_forms_bucket = "Booking_Forms"
    return svc, persistence, payment_persistence


def _paid_payment(**overrides):
    defaults = dict(
        id="pay1", owner_id="C00001", status="paid",
        razorpay_order_id="order_Rzp123", razorpay_payment_id="pay_Rzp456",
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


def test_upload_booking_application_rejects_empty_file():
    svc, _, _ = _booking_service(_paid_payment())
    try:
        svc.upload_booking_application(
            b"", "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "empty_file"


def test_upload_booking_application_rejects_non_pdf_bytes():
    svc, _, _ = _booking_service(_paid_payment())
    try:
        svc.upload_booking_application(
            b"not a pdf", "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "unsupported_file_type"


def test_upload_booking_application_rejects_oversized_file():
    svc, _, _ = _booking_service(_paid_payment())
    huge = _PDF_BYTES + b"0" * (16 * 1024 * 1024)
    try:
        svc.upload_booking_application(
            huge, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "file_too_large"


def test_upload_booking_application_rejects_invalid_form_data_json():
    svc, _, _ = _booking_service(_paid_payment())
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{not json",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_form_data"


def test_upload_booking_application_rejects_missing_payment():
    svc, _, _ = _booking_service(payment=None)
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "payment_not_found"


def test_upload_booking_application_rejects_payment_owned_by_someone_else():
    svc, _, _ = _booking_service(_paid_payment(owner_id="C99999"))
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_upload_booking_application_rejects_unpaid_payment():
    svc, _, _ = _booking_service(_paid_payment(status="created"))
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "payment_not_completed"


def test_upload_booking_application_rejects_mismatched_razorpay_order_id():
    svc, _, _ = _booking_service(_paid_payment())
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1",
            "order_totally_different", None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "payment_mismatch"


def test_upload_booking_application_rejects_client_supplied_razorpay_id_against_cash_payment():
    # A cash payment is legitimately status="paid" with both razorpay ids NULL (see
    # service_payment.record_cash_payment). A client sending a self-reported razorpay_order_id
    # for such a payment must be rejected, not silently accepted and persisted, since nothing
    # verifies the client's claim against anything real in that case.
    svc, _, _ = _booking_service(_paid_payment(razorpay_order_id=None, razorpay_payment_id=None))
    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1",
            "order_self_reported", None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "payment_mismatch"


@patch.object(serviceDocument, "_sign_url", return_value="url")
@patch.object(serviceDocument, "_upload_to_storage")
def test_upload_booking_application_succeeds_for_cash_payment_when_no_razorpay_ids_supplied(mock_upload, mock_sign):
    svc, persistence, _ = _booking_service(_paid_payment(razorpay_order_id=None, razorpay_payment_id=None))
    persistence.create_document.return_value = MagicMock(id="doc1")

    svc.upload_booking_application(
        _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
        owner_id="C00001", owner_role="customer",
    )

    _, kwargs = persistence.create_document.call_args
    assert kwargs["razorpay_order_id"] is None
    assert kwargs["razorpay_payment_id"] is None


@patch.object(serviceDocument, "_sign_url", return_value="https://fake.supabase.co/signed")
@patch.object(serviceDocument, "_upload_to_storage")
def test_upload_booking_application_happy_path_uses_booking_forms_bucket(mock_upload, mock_sign):
    svc, persistence, payment_persistence = _booking_service(_paid_payment())
    persistence.create_document.return_value = MagicMock(
        id="doc1", owner_id="C00001", owner_role="customer",
        document_type="project_booking_application", status="generated", created_date=None,
    )

    doc, signed_url, expires_in = svc.upload_booking_application(
        _PDF_BYTES, "application/pdf", "project_booking_application", "ops-divine-greens", "pay1",
        "order_Rzp123", "pay_Rzp456", '{"applicantName": "Jane"}',
        owner_id="C00001", owner_role="customer",
    )

    assert doc.id == "doc1"
    assert signed_url == "https://fake.supabase.co/signed"
    assert expires_in == 3600
    mock_upload.assert_called_once()
    assert mock_upload.call_args.kwargs["bucket"] == "Booking_Forms"
    mock_sign.assert_called_once()
    assert mock_sign.call_args.kwargs["bucket"] == "Booking_Forms"

    _, kwargs = persistence.create_document.call_args
    assert kwargs["project_id"] == "ops-divine-greens"
    assert kwargs["payment_id"] == "pay1"
    # Persisted from the payment record itself, not the client-supplied form fields (those are
    # only used to verify the caller's claim - see the mismatch tests below).
    assert kwargs["razorpay_order_id"] == "order_Rzp123"
    assert kwargs["razorpay_payment_id"] == "pay_Rzp456"
    assert kwargs["form_data"] == {"applicantName": "Jane"}
    assert kwargs["storage_bucket"] == "Booking_Forms"


@patch.object(serviceDocument, "_sign_url")
@patch.object(serviceDocument, "_delete_from_storage")
@patch.object(serviceDocument, "_upload_to_storage")
def test_upload_booking_application_cleans_up_storage_when_persistence_fails(mock_upload, mock_delete, mock_sign):
    svc, persistence, _ = _booking_service(_paid_payment())
    persistence.create_document.side_effect = RuntimeError("db exploded")

    try:
        svc.upload_booking_application(
            _PDF_BYTES, "application/pdf", "project_booking_application", "proj1", "pay1", None, None, "{}",
            owner_id="C00001", owner_role="customer",
        )
        assert False, "expected the persistence error to propagate"
    except RuntimeError as e:
        assert str(e) == "db exploded"

    mock_delete.assert_called_once()
    assert mock_delete.call_args.kwargs["bucket"] == "Booking_Forms"
    mock_sign.assert_not_called()

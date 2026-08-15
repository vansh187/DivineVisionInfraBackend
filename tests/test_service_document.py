import os
import requests
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_document import serviceDocument, _safe_path_segment, _pdf_safe_text


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

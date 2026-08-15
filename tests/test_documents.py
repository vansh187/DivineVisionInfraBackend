import os
import jwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Ensure test env before importing app/persistence
os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app


client = TestClient(app)

FAKE_SIGNED_URL = "https://fake.supabase.co/storage/v1/object/sign/documents/fake.pdf?token=abc"

_TOKEN = None  # set in setup_module - shared across most tests to stay within the rate limiter
_OTHER_TOKEN = None  # a second, distinct identity - only for the cross-owner test
_SHARED_DOCUMENT_ID = None  # one pre-generated document, reused by GET-focused tests


def setup_module(module):
    global _TOKEN, _OTHER_TOKEN, _SHARED_DOCUMENT_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    # Two real signups total for this whole file (plus test_auth.py's own one signup),
    # instead of one per test - the global rate limiter (10 req/60s per client+path)
    # applies across the whole test session, and blowing through it here previously
    # caused unrelated tests in other files to fail with spurious 429s.
    client.post("/customer/signup", json={"username": "dockust_shared", "password": "strongpassword"})
    lr = client.post("/customer/login", json={"username": "dockust_shared", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _TOKEN = lr.json()["access_token"]

    client.post("/customer/signup", json={"username": "dockust_other", "password": "strongpassword"})
    lr2 = client.post("/customer/login", json={"username": "dockust_other", "password": "strongpassword"})
    assert lr2.status_code == 200, lr2.text
    _OTHER_TOKEN = lr2.json()["access_token"]

    # /documents/generate is itself rate-limited (10 req/60s, path-wide) same as every other
    # route, and several tests below only need GET /documents/{id} against an existing
    # document - generating one here and reusing its id keeps the file's total POST count
    # for that path comfortably under the limit instead of one generate call per GET test.
    with patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL), \
         patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None):
        create_resp = client.post(
            "/documents/generate",
            json={"document_type": "kyc", "form_data": {"full_name": "Shared Doc Owner"}},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert create_resp.status_code == 200, create_resp.text
    _SHARED_DOCUMENT_ID = create_resp.json()["id"]


def _auth_headers():
    return {"Authorization": f"Bearer {_TOKEN}"}


def test_generate_document_requires_auth():
    r = client.post("/documents/generate", json={"document_type": "kyc", "form_data": {"name": "A"}})
    assert r.status_code == 401


def test_generate_document_rejects_garbage_token():
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert r.status_code == 401


def test_generate_document_rejects_expired_token():
    expired_payload = {
        "sub": "C00001",
        "username": "someone",
        "role": "customer",
        "exp": (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp(),
    }
    expired_token = jwt.encode(expired_payload, "testsecret", algorithm="HS256")
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r.status_code == 401


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None)
def test_generate_document_happy_path(mock_upload, mock_sign):
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"full_name": "Jane Doe", "pan": "ABCDE1234F"}},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_type"] == "kyc"
    assert data["owner_role"] == "customer"
    assert data["status"] == "generated"
    assert data["signed_url"] == FAKE_SIGNED_URL
    assert data["signed_url_expires_in"] == 3600
    mock_upload.assert_called_once()
    mock_sign.assert_called_once()


def test_get_document_forbidden_for_other_owner():
    r = client.get(f"/documents/{_SHARED_DOCUMENT_ID}", headers={"Authorization": f"Bearer {_OTHER_TOKEN}"})
    assert r.status_code == 403


def test_get_document_not_found():
    r = client.get("/documents/does-not-exist", headers=_auth_headers())
    assert r.status_code == 404


def test_generate_document_missing_document_type_is_422():
    r = client.post(
        "/documents/generate",
        json={"form_data": {"name": "A"}},
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_generate_document_missing_form_data_is_422():
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc"},
        headers=_auth_headers(),
    )
    assert r.status_code == 422


@patch("DivineService.service_document.serviceDocument._upload_to_storage", side_effect=RuntimeError("storage_not_configured"))
def test_generate_document_returns_502_when_storage_not_configured(mock_upload):
    # Reproduces the real production incident: SUPABASE_URL missing on the deployed
    # environment surfaces here as a clean 502, not a 500/crash.
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers=_auth_headers(),
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_not_configured"


@patch("DivineService.service_document.serviceDocument._upload_to_storage", side_effect=RuntimeError("storage_upload_failed:500"))
def test_generate_document_returns_502_on_upload_failure(mock_upload):
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers=_auth_headers(),
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_upload_failed:500"


@patch("DivineService.service_document.serviceDocument._sign_url", side_effect=RuntimeError("storage_sign_failed:500"))
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None)
def test_generate_document_returns_502_on_sign_failure(mock_upload, mock_sign):
    r = client.post(
        "/documents/generate",
        json={"document_type": "kyc", "form_data": {"name": "A"}},
        headers=_auth_headers(),
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_sign_failed:500"


@patch("DivineService.service_document.serviceDocument._sign_url", side_effect=RuntimeError("storage_sign_failed:503"))
def test_get_document_returns_502_when_refresh_sign_fails(mock_sign):
    r = client.get(f"/documents/{_SHARED_DOCUMENT_ID}", headers=_auth_headers())
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_sign_failed:503"


# ---------- POST /documents/aadhaar-photo ----------

def _fake_jpeg_bytes() -> bytes:
    # Genuinely decodable image bytes, not just a JPEG magic-number prefix - the upload
    # endpoint verifies uploads by actually decoding them (cv2.imdecode), not by trusting
    # the client-supplied Content-Type header alone, so a real (if tiny/blank) image is
    # needed for the happy-path tests using this helper to actually succeed.
    import cv2
    import numpy as np

    blank = np.zeros((20, 20, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", blank)
    return encoded.tobytes()


def test_upload_aadhaar_photo_requires_auth():
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("front.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        data={"side": "front"},
    )
    assert r.status_code == 401


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None)
def test_upload_aadhaar_photo_front_happy_path(mock_upload, mock_sign):
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("front.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        data={"side": "front"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_type"] == "aadhaar_front"
    assert data["status"] == "uploaded"
    assert data["signed_url"] == FAKE_SIGNED_URL
    mock_upload.assert_called_once()
    # content-type is forwarded through to storage, not hardcoded to application/pdf
    assert mock_upload.call_args.args[2] == "image/jpeg"


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None)
def test_upload_aadhaar_photo_back_happy_path(mock_upload, mock_sign):
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("back.png", _fake_jpeg_bytes(), "image/png")},
        data={"side": "back"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["document_type"] == "aadhaar_back"


def test_upload_aadhaar_photo_rejects_invalid_side():
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("x.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        data={"side": "sideways"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_side"


def test_upload_aadhaar_photo_rejects_unsupported_file_type():
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"side": "front"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_file_type"


def test_upload_aadhaar_photo_rejects_non_image_bytes_with_image_content_type():
    # Content-Type is client-supplied and not trustworthy on its own - bytes claiming to
    # be image/jpeg but that aren't actually decodable as an image must still be rejected.
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("x.jpg", b"not actually an image", "image/jpeg")},
        data={"side": "front"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_file_type"


def test_upload_aadhaar_photo_rejects_empty_file():
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("x.jpg", b"", "image/jpeg")},
        data={"side": "front"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_file"


@patch("DivineService.service_document.serviceDocument._upload_to_storage", side_effect=RuntimeError("storage_not_configured"))
def test_upload_aadhaar_photo_returns_502_when_storage_not_configured(mock_upload):
    r = client.post(
        "/documents/aadhaar-photo",
        files={"file": ("x.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        data={"side": "front"},
        headers=_auth_headers(),
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_not_configured"


# ---------- POST /documents/pan-photo ----------

def test_upload_pan_photo_requires_auth():
    r = client.post(
        "/documents/pan-photo",
        files={"file": ("pan.jpg", _fake_jpeg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 401


@patch("DivineService.service_document.serviceDocument._sign_url", return_value=FAKE_SIGNED_URL)
@patch("DivineService.service_document.serviceDocument._upload_to_storage", return_value=None)
def test_upload_pan_photo_happy_path(mock_upload, mock_sign):
    r = client.post(
        "/documents/pan-photo",
        files={"file": ("pan.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_type"] == "pan_card"
    assert data["status"] == "uploaded"
    assert data["signed_url"] == FAKE_SIGNED_URL


def test_upload_pan_photo_rejects_unsupported_file_type():
    r = client.post(
        "/documents/pan-photo",
        files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_file_type"


def test_upload_pan_photo_rejects_empty_file():
    r = client.post(
        "/documents/pan-photo",
        files={"file": ("x.jpg", b"", "image/jpeg")},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_file"


@patch("DivineService.service_document.serviceDocument._upload_to_storage", side_effect=RuntimeError("storage_not_configured"))
def test_upload_pan_photo_returns_502_when_storage_not_configured(mock_upload):
    r = client.post(
        "/documents/pan-photo",
        files={"file": ("x.jpg", _fake_jpeg_bytes(), "image/jpeg")},
        headers=_auth_headers(),
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "storage_not_configured"

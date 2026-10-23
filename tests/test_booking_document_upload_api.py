"""End-to-end coverage for POST /admin/bookings/{id}/documents/{type} - the
admin KYC document upload/replacement - over real HTTP against the SQLite test
database. Supabase Storage is stubbed; everything else (auth, approval check,
optimistic lock, idempotency, audit rows, rate limits) runs for real."""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import cv2
import jwt
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB, SessionLocal
from Divinepersistence.persistence_admin import persistenceAdmin
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_customer import persistenceCustomer
from DivineService.service_admin import serviceAdmin
from DivineService.service_payment import servicePayment
import DivineAPI.admin_booking_kyc_api as admin_booking_kyc_api
import DivineService.service_booking_document_upload as upload_module
from DivineAPI.main import app

client = TestClient(app)

_CUSTOMER_ID = None
_ADMIN_ID = None
_PENDING_ADMIN_ID = None
_REJECTED_ADMIN_ID = None


def setup_module(module):
    global _CUSTOMER_ID, _ADMIN_ID, _PENDING_ADMIN_ID, _REJECTED_ADMIN_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    customer = persistenceCustomer().create_user(
        username="doc_upload_customer", password_hash="not-used-by-these-tests",
        phone="9000000031", email="doc.upload@example.com", first_name="Aarav", last_name="Sharma",
    )
    _CUSTOMER_ID = customer.id

    _ADMIN_ID = _new_admin("DVT101", "divya.upload@divinevisioninfra.com", "approved")
    _PENDING_ADMIN_ID = _new_admin("DVT102", "pending.upload@divinevisioninfra.com", None)
    _REJECTED_ADMIN_ID = _new_admin("DVT104", "rejected.upload@divinevisioninfra.com", "rejected")


def _new_admin(employee_id, email, status):
    admins = persistenceAdmin()
    row = admins.create_user("Test Admin", employee_id, email, "x")
    if status:
        admins.set_approval_status(row.id, status, decided_by="test")
    return row.id


def _new_booking() -> str:
    unit_id = str(uuid.uuid4())
    persistenceInventory().upsert_unit(
        id=unit_id, project_name="Divine Meadows", city="Karnal", unit_number=f"A-{uuid.uuid4().hex[:4]}",
        area_sqmt=200, area_sqyd=239, status="available",
    )
    payment = servicePayment().record_cash_payment(
        amount=250000, owner_id=_CUSTOMER_ID, owner_role="customer",
        purpose="plot_booking", inventory_id=unit_id, method="rtgs_neft", utr_number=f"UTR{uuid.uuid4().hex[:9]}",
    )
    assert payment.booking_id
    return payment.booking_id


def _headers(admin_id=None, key=None):
    payload = {
        "sub": admin_id or _ADMIN_ID, "username": "divya.upload@divinevisioninfra.com",
        "role": "admin", "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return {
        "Authorization": f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}",
        "Idempotency-Key": key or str(uuid.uuid4()),
    }


def _png(color=0) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((8, 8, 3), color, dtype=np.uint8))
    assert ok
    return buf.tobytes()


_PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


class _StorageStub:
    def __init__(self):
        self.uploaded, self.deleted = [], []
        self.fail_upload, self.on_upload = False, None

    def store(self, object_path, file_bytes, content_type):
        if self.fail_upload:
            raise RuntimeError("storage_unreachable")
        if self.on_upload:
            self.on_upload()
        self.uploaded.append((object_path, content_type))

    def delete(self, object_path):
        self.deleted.append(object_path)

    @staticmethod
    def sign_batch(object_paths, expires_in=3600, bucket=None):
        return {p: f"https://storage.example.invalid/signed/{p}" for p in object_paths}


@pytest.fixture(autouse=True)
def _generous_rate_limits(monkeypatch):
    # Every test here uploads as the same admin - only the rate-limit tests
    # themselves should ever hit a real cap.
    monkeypatch.setattr(upload_module, "ADMIN_UPLOADS_PER_MINUTE", 1000)


@pytest.fixture
def storage(monkeypatch):
    stub = _StorageStub()
    docs = admin_booking_kyc_api._booking_service.document_service
    monkeypatch.setattr(docs, "store_object", stub.store)
    monkeypatch.setattr(docs, "delete_object", stub.delete)
    monkeypatch.setattr(docs, "_sign_urls_batch", stub.sign_batch)
    return stub


def _persistence():
    return admin_booking_kyc_api._booking_service.persistence


def _version(booking_id):
    return _persistence().get_by_id(booking_id).version


def _audit_rows(booking_id):
    with SessionLocal() as db:
        return db.execute(text(
            "SELECT * FROM divine_booking_document_uploads WHERE booking_id = :b ORDER BY created_date"
        ), {"b": booking_id}).mappings().all()


def _post(booking_id, doc_type="pan_card", file=None, filename="pan.png", content_type="image/png",
          version=None, note=None, headers=None):
    data = {"expected_version": str(version if version is not None else _version(booking_id))}
    if note is not None:
        data["note"] = note
    files = {"file": (filename, file if file is not None else _png(), content_type)}
    return client.post(f"/admin/bookings/{booking_id}/documents/{doc_type}",
                       data=data, files=files, headers=headers or _headers())


def _code(resp):
    return resp.json()["detail"]["code"]


# ---------- happy path ----------

def test_upload_stores_document_bumps_version_and_audits(storage):
    booking_id = _new_booking()
    before = _version(booking_id)
    resp = _post(booking_id, note="  Replacement PAN supplied after name mismatch  ",
                 filename="../../etc/pan.png")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == before + 1
    assert body["status"] == "pending_kyc_review"
    assert body["kyc_status"] == "pending"  # never auto-verified

    pan = next(d for d in body["documents"] if d["document_type"] == "pan_card")
    assert pan["uploaded"] is True and pan["preview_url"].startswith("https://storage.example.invalid/")
    assert len(body["documents"]) == 5  # full checklist, missing entries included

    event = body["decision_history"][-1]
    assert event["action"] == "kyc_document_uploaded"
    assert event["actor"] == _ADMIN_ID
    assert event["note"] == "Replacement PAN supplied after name mismatch"

    (object_path, content_type), = storage.uploaded
    assert object_path.startswith(f"{_CUSTOMER_ID}/pan_card_") and object_path.endswith(".png")
    assert ".." not in object_path
    assert content_type == "image/png"

    audit, = _audit_rows(booking_id)
    assert audit["admin_id"] == _ADMIN_ID and audit["document_type"] == "pan_card"
    assert audit["request_id"] == resp.headers["X-Request-ID"]
    assert audit["byte_size"] > 0 and len(audit["content_sha256"]) == 64
    doc = admin_booking_kyc_api._booking_service.document_service._persistence.get_by_id(audit["document_id"])
    assert doc.owner_id == _CUSTOMER_ID and doc.owner_role == "customer" and doc.storage_path == object_path
    form_data = json.loads(doc.form_data) if isinstance(doc.form_data, str) else doc.form_data
    assert form_data["original_filename"] == "pan.png"  # path components stripped
    assert form_data["source"] == "admin_upload" and form_data["uploaded_by_admin_id"] == _ADMIN_ID


def test_replacement_records_supersession_and_keeps_history(storage):
    booking_id = _new_booking()
    first = _post(booking_id, doc_type="cancelled_cheque", file=_PDF, filename="cheque.pdf",
                  content_type="application/pdf")
    assert first.status_code == 200, first.text
    ok, jpg = cv2.imencode(".jpg", np.zeros((8, 8, 3), dtype=np.uint8))
    second = _post(booking_id, doc_type="cancelled_cheque", file=jpg.tobytes(), filename="cheque.JPEG",
                   content_type="image/jpeg")
    assert second.status_code == 200, second.text
    assert storage.uploaded[-1][0].endswith(".jpg")
    actions = [d["action"] for d in second.json()["decision_history"]]
    assert actions.count("kyc_document_uploaded") == 2

    a, b = _audit_rows(booking_id)
    assert b["previous_document_id"] == a["document_id"]
    assert storage.deleted == []  # the prior object is retained, never deleted


def test_pdf_only_allowed_for_cancelled_cheque(storage):
    """Aadhaar/PAN/photo are embedded as images in the generated booking
    application - a PDF there would silently fail to render."""
    booking_id = _new_booking()
    for doc_type in ("aadhaar_front", "aadhaar_back", "pan_card", "applicant_photo"):
        resp = _post(booking_id, doc_type=doc_type, file=_PDF, filename="x.pdf", content_type="application/pdf")
        assert resp.status_code == 415, (doc_type, resp.text)
        assert "JPG/JPEG or PNG" in resp.json()["detail"]["message"]
    assert storage.uploaded == []


def test_request_id_is_server_generated_not_caller_chosen(storage):
    booking_id = _new_booking()
    headers = {**_headers(), "X-Request-ID": "abc"}
    resp = _post(booking_id, doc_type="aadhaar_front", headers=headers)
    assert resp.status_code == 200, resp.text
    rid = resp.headers["X-Request-ID"]
    assert rid != "abc" and uuid.UUID(rid)
    assert _audit_rows(booking_id)[0]["request_id"] == rid
    other = client.get("/admin/bookings/BKG-NOPE", headers={"X-Request-ID": "abc"})
    assert other.headers["X-Request-ID"] not in ("abc", rid)


# ---------- idempotency ----------

def test_same_key_same_request_replays_without_second_write(storage):
    booking_id = _new_booking()
    version = _version(booking_id)
    headers = _headers()
    first = _post(booking_id, version=version, headers=headers, note="n")
    second = _post(booking_id, version=version, headers=headers, note="n")
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert second.json()["version"] == version + 1
    assert len(storage.uploaded) == 1
    assert len(_audit_rows(booking_id)) == 1


def test_same_key_different_file_is_409(storage):
    booking_id = _new_booking()
    version = _version(booking_id)
    headers = _headers()
    assert _post(booking_id, version=version, headers=headers).status_code == 200
    resp = _post(booking_id, version=version, headers=headers, file=_png(255))
    assert resp.status_code == 409 and _code(resp) == "idempotency_conflict"


def test_duplicate_key_insert_is_rejected_by_the_database(storage):
    """The unique (admin_id, idempotency_key) constraint is the backstop for two
    same-key requests racing past the up-front lookup (service handling of the
    resulting IntegrityError is unit-tested in test_service_booking_document_upload)."""
    from sqlalchemy.exc import IntegrityError
    booking_id = _new_booking()
    key = str(uuid.uuid4())
    assert _post(booking_id, headers=_headers(key=key)).status_code == 200
    now = datetime.now(timezone.utc)
    with pytest.raises(IntegrityError):
        _persistence().record_admin_document_upload(
            booking_id=booking_id, expected_version=_version(booking_id), uploadable_status="pending_kyc_review",
            admin_id=_ADMIN_ID, idempotency_key=key, request_fingerprint="f" * 64, request_id=None, note=None,
            byte_size=1, content_sha256="0" * 64, admin_limit=1000, admin_since=now - timedelta(minutes=1),
            booking_limit=1000, booking_since=now - timedelta(hours=1),
            document={"id": str(uuid.uuid4()), "owner_id": _CUSTOMER_ID, "owner_role": "customer",
                      "document_type": "pan_card", "storage_path": "x/y.png", "status": "uploaded",
                      "storage_bucket": "documents", "form_data": {}},
        )
    assert len(_audit_rows(booking_id)) == 1  # rolled back - nothing half-written
    assert _version(booking_id) == 2


# ---------- auth ----------

def test_requires_auth(storage):
    booking_id = _new_booking()
    resp = client.post(f"/admin/bookings/{booking_id}/documents/pan_card",
                       data={"expected_version": "1"}, files={"file": ("p.png", _png(), "image/png")})
    assert resp.status_code == 401 and _code(resp) == "unauthorized"


def test_customer_token_is_unauthorized(storage):
    booking_id = _new_booking()
    token = jwt.encode({"sub": _CUSTOMER_ID, "role": "customer",
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "testsecret", algorithm="HS256")
    resp = _post(booking_id, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())})
    assert resp.status_code == 401 and _code(resp) == "unauthorized"


@pytest.mark.parametrize("which", ["pending", "rejected", "unknown"])
def test_non_approved_admins_are_forbidden(storage, which):
    admin_id = {"pending": _PENDING_ADMIN_ID, "rejected": _REJECTED_ADMIN_ID, "unknown": "ZZ9999"}[which]
    booking_id = _new_booking()
    resp = _post(booking_id, headers=_headers(admin_id=admin_id))
    assert resp.status_code == 403 and _code(resp) == "forbidden"
    assert storage.uploaded == []


def test_super_admin_still_pending_is_allowed_like_login(storage, monkeypatch):
    """Login lets a configured super admin in even if their row is still
    'pending' (auto-approve write failed); upload must agree."""
    admin_id = _new_admin("DVT199", "boss.upload@divinevisioninfra.com", None)
    monkeypatch.setattr(admin_booking_kyc_api, "_admin_service",
                        serviceAdmin(super_admins="boss.upload@divinevisioninfra.com:DVT199"))
    booking_id = _new_booking()
    resp = _post(booking_id, headers=_headers(admin_id=admin_id))
    assert resp.status_code == 200, resp.text


def test_admin_lookup_failure_is_sanitized_500(storage, monkeypatch):
    class Broken:
        def get_active_admin(self, admin_id):
            raise RuntimeError("db password=hunter2")

    monkeypatch.setattr(admin_booking_kyc_api, "_admin_service", Broken())
    resp = _post(_new_booking())
    assert resp.status_code == 500 and _code(resp) == "internal_error"
    assert "hunter2" not in resp.text


# ---------- booking state ----------

def test_stale_version_is_409_with_current_version(storage):
    booking_id = _new_booking()
    current = _version(booking_id)
    resp = _post(booking_id, version=current + 5)
    assert resp.status_code == 409 and _code(resp) == "version_conflict"
    assert resp.json()["detail"]["current_version"] == current
    assert storage.uploaded == []


def test_decided_booking_is_not_uploadable(storage):
    booking_id = _new_booking()
    _persistence().update_decision(id=booking_id, expected_version=_version(booking_id), status="rejected",
                                   kyc_status="rejected", admin_note=None)
    resp = _post(booking_id)
    assert resp.status_code == 409 and _code(resp) == "booking_not_uploadable"


def test_unknown_booking_is_404(storage):
    resp = client.post("/admin/bookings/BKG-DOES-NOT-EXIST/documents/pan_card",
                       data={"expected_version": "1"}, files={"file": ("p.png", _png(), "image/png")},
                       headers=_headers())
    assert resp.status_code == 404 and _code(resp) == "booking_not_found"


def test_version_race_at_commit_reports_current_version_and_drops_object(storage):
    booking_id = _new_booking()
    v = _version(booking_id)
    # Another admin's write lands between the pre-check and the commit.
    storage.on_upload = lambda: _persistence().update_decision(
        id=booking_id, expected_version=v, status="pending_kyc_review", kyc_status="pending", admin_note=None,
    )
    resp = _post(booking_id, version=v)
    assert resp.status_code == 409 and _code(resp) == "version_conflict"
    assert resp.json()["detail"]["current_version"] == v + 1
    assert storage.deleted == [storage.uploaded[0][0]]
    assert _audit_rows(booking_id) == []


def test_status_race_at_commit_is_not_uploadable(storage):
    booking_id = _new_booking()
    v = _version(booking_id)
    storage.on_upload = lambda: _persistence().update_decision(
        id=booking_id, expected_version=v, status="cancelled", kyc_status="rejected", admin_note=None,
    )
    resp = _post(booking_id, version=v)
    assert resp.status_code == 409 and _code(resp) == "booking_not_uploadable"
    assert storage.deleted == [storage.uploaded[0][0]]


# ---------- file validation ----------

@pytest.mark.parametrize("file_bytes,filename,content_type", [
    (b"hello world, not a document", "pan.png", "image/png"),        # no known signature
    (_png(), "pan.pdf", "application/pdf"),                            # extension/MIME vs PNG bytes
    (_png(), "pan.png", "image/jpeg"),                                 # client MIME mismatch
    (_png(), "pan.gif", "image/png"),                                  # extension not allowed
    (_png(), "pan", "image/png"),                                      # no extension
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "pan.png", "image/png"),     # signature but undecodable
])
def test_unsupported_files_are_415(storage, file_bytes, filename, content_type):
    booking_id = _new_booking()
    resp = _post(booking_id, file=file_bytes, filename=filename, content_type=content_type)
    assert resp.status_code == 415, resp.text
    assert _code(resp) == "unsupported_media_type"
    assert storage.uploaded == []


def test_encrypted_pdf_cheque_is_415(storage):
    booking_id = _new_booking()
    resp = _post(booking_id, doc_type="cancelled_cheque", filename="c.pdf", content_type="application/pdf",
                 file=_PDF.replace(b"<< /Type", b"<< /Encrypt 2 0 R /Type"))
    assert resp.status_code == 415 and "Encrypted" in resp.json()["detail"]["message"]


def test_generic_client_mime_is_accepted_when_bytes_match(storage):
    booking_id = _new_booking()
    resp = _post(booking_id, doc_type="applicant_photo", content_type="application/octet-stream")
    assert resp.status_code == 200, resp.text
    assert storage.uploaded[0][1] == "image/png"  # stored with the DETECTED type


def test_oversized_file_is_413(storage):
    booking_id = _new_booking()
    resp = _post(booking_id, doc_type="cancelled_cheque", file=b"%PDF-" + b"0" * (10 * 1024 * 1024),
                 filename="big.pdf", content_type="application/pdf")
    assert resp.status_code == 413 and _code(resp) == "file_too_large"


def test_declared_oversized_body_is_rejected_before_parsing(storage):
    booking_id = _new_booking()
    headers = {**_headers(), "Content-Type": "multipart/form-data; boundary=x",
               "Content-Length": str(12 * 1024 * 1024)}
    resp = client.post(f"/admin/bookings/{booking_id}/documents/pan_card", content=b"--x--", headers=headers)
    assert resp.status_code == 413 and _code(resp) == "file_too_large"


def test_chunked_body_without_content_length_is_rejected(storage):
    booking_id = _new_booking()

    def body():
        yield b"--x\r\nContent-Disposition: form-data; name=\"expected_version\"\r\n\r\n1\r\n--x--\r\n"

    headers = {**_headers(), "Content-Type": "multipart/form-data; boundary=x"}
    resp = client.post(f"/admin/bookings/{booking_id}/documents/pan_card", content=body(), headers=headers)
    assert resp.status_code == 400 and _code(resp) == "invalid_multipart"
    assert "Content-Length" in resp.json()["detail"]["message"]


@pytest.mark.parametrize("kwargs", [
    {"doc_type": "passport"},
    {"version": 0},
    {"note": "x" * 501},
    {"note": "bad\x00note"},
    {"file": b""},
])
def test_invalid_fields_are_422(storage, kwargs):
    booking_id = _new_booking()
    resp = _post(booking_id, **kwargs)
    assert resp.status_code == 422, resp.text
    assert _code(resp) == "validation_error"


def test_non_integer_version_is_422(storage):
    booking_id = _new_booking()
    resp = client.post(f"/admin/bookings/{booking_id}/documents/pan_card", data={"expected_version": "abc"},
                       files={"file": ("p.png", _png(), "image/png")}, headers=_headers())
    assert resp.status_code == 422 and _code(resp) == "validation_error"


def test_missing_file_is_422(storage):
    booking_id = _new_booking()
    resp = client.post(f"/admin/bookings/{booking_id}/documents/pan_card",
                       data={"expected_version": "1"}, files={"other": ("x", b"x")}, headers=_headers())
    assert resp.status_code == 422 and _code(resp) == "validation_error"


def test_idempotency_key_required_and_must_be_uuid(storage):
    booking_id = _new_booking()
    headers = _headers()
    headers["Idempotency-Key"] = "not-a-uuid"
    resp = _post(booking_id, headers=headers)
    assert resp.status_code == 400 and _code(resp) == "invalid_multipart"
    del headers["Idempotency-Key"]
    resp = _post(booking_id, headers=headers)
    assert resp.status_code == 422 and _code(resp) == "validation_error"


def test_non_multipart_body_is_400(storage):
    booking_id = _new_booking()
    resp = client.post(f"/admin/bookings/{booking_id}/documents/aadhaar_back",
                       json={"expected_version": 1}, headers=_headers())
    assert resp.status_code == 400 and _code(resp) == "invalid_multipart"


# ---------- dependencies / limits ----------

def test_storage_outage_is_503_and_nothing_persisted(storage):
    booking_id = _new_booking()
    storage.fail_upload = True
    before = _version(booking_id)
    resp = _post(booking_id, doc_type="applicant_photo")
    assert resp.status_code == 503 and _code(resp) == "document_storage_unavailable"
    assert _version(booking_id) == before
    assert _audit_rows(booking_id) == []


def test_db_commit_failure_is_sanitized_500_and_drops_object(storage, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("relation divine_x does not exist SELECT secret")

    monkeypatch.setattr(_persistence(), "record_admin_document_upload", boom)
    booking_id = _new_booking()
    resp = _post(booking_id)
    assert resp.status_code == 500 and _code(resp) == "internal_error"
    assert "secret" not in resp.text
    assert storage.deleted == [storage.uploaded[0][0]]


def test_per_admin_rate_limit_is_429_with_retry_after(storage, monkeypatch):
    monkeypatch.setattr(upload_module, "ADMIN_UPLOADS_PER_MINUTE", 1)
    admin_id = _new_admin("DVT103", "rate.upload@divinevisioninfra.com", "approved")
    booking_id = _new_booking()
    first = _post(booking_id, doc_type="aadhaar_front", headers=_headers(admin_id=admin_id))
    assert first.status_code == 200, first.text
    resp = _post(booking_id, doc_type="aadhaar_back", headers=_headers(admin_id=admin_id))
    assert resp.status_code == 429 and _code(resp) == "rate_limited"
    assert resp.headers.get("Retry-After") == "60"
    assert len(storage.uploaded) == 1  # the fast pre-check stopped it before storage


def test_per_booking_rate_limit_is_429(storage, monkeypatch):
    monkeypatch.setattr(upload_module, "BOOKING_UPLOADS_PER_HOUR", 1)
    booking_id = _new_booking()
    assert _post(booking_id, doc_type="aadhaar_front").status_code == 200
    resp = _post(booking_id, doc_type="aadhaar_back")
    assert resp.status_code == 429 and resp.headers.get("Retry-After") == "3600"


def test_rate_limit_is_enforced_inside_the_commit_transaction(storage, monkeypatch):
    """Concurrent requests can all pass the advisory pre-check; the in-transaction
    count under the admin row lock is what actually stops them."""
    monkeypatch.setattr(upload_module, "ADMIN_UPLOADS_PER_MINUTE", 1)
    admin_id = _new_admin("DVT105", "race.upload@divinevisioninfra.com", "approved")
    booking_id = _new_booking()
    assert _post(booking_id, doc_type="aadhaar_front", headers=_headers(admin_id=admin_id)).status_code == 200
    monkeypatch.setattr(_persistence(), "count_recent_document_uploads", lambda *a, **k: (0, 0))
    resp = _post(booking_id, doc_type="aadhaar_back", headers=_headers(admin_id=admin_id))
    assert resp.status_code == 429 and _code(resp) == "rate_limited"
    assert storage.deleted == [storage.uploaded[-1][0]]
    assert len(_audit_rows(booking_id)) == 1


def test_replay_is_not_rate_limited(storage, monkeypatch):
    monkeypatch.setattr(upload_module, "ADMIN_UPLOADS_PER_MINUTE", 1)
    admin_id = _new_admin("DVT106", "replay.upload@divinevisioninfra.com", "approved")
    booking_id = _new_booking()
    version = _version(booking_id)
    headers = _headers(admin_id=admin_id)
    assert _post(booking_id, version=version, headers=headers).status_code == 200
    resp = _post(booking_id, version=version, headers=headers)
    assert resp.status_code == 200 and resp.headers.get("Idempotent-Replayed") == "true"


# ---------- existing endpoints unaffected ----------

def test_approve_still_works_after_an_upload(storage):
    booking_id = _new_booking()
    upload = _post(booking_id, doc_type="aadhaar_front")
    assert upload.status_code == 200
    resp = client.post(f"/admin/bookings/{booking_id}/approve",
                       json={"note": "ok", "version": upload.json()["version"]}, headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "booked" and resp.json()["kyc_status"] == "verified"


# ---------- documents are shared across the owner's bookings ----------

def _fresh_customer_bookings(n):
    """n held bookings for a brand-new customer (not shared with other tests)."""
    customer = persistenceCustomer().create_user(
        username=f"sib_{uuid.uuid4().hex[:8]}", password_hash="x",
        phone=f"9{uuid.uuid4().int % 10 ** 9:09d}", email=f"{uuid.uuid4().hex[:8]}@example.com",
        first_name="Sib", last_name="Ling",
    )
    ids = []
    for _ in range(n):
        unit_id = str(uuid.uuid4())
        persistenceInventory().upsert_unit(
            id=unit_id, project_name="Divine Meadows", city="Karnal", unit_number=f"S-{uuid.uuid4().hex[:4]}",
            area_sqmt=200, area_sqyd=239, status="available",
        )
        ids.append(servicePayment().record_cash_payment(
            amount=250000, owner_id=customer.id, owner_role="customer", purpose="plot_booking",
            inventory_id=unit_id, method="rtgs_neft", utr_number=f"UTR{uuid.uuid4().hex[:9]}",
        ).booking_id)
    return customer.id, ids


def test_upload_bumps_the_owners_other_pending_bookings(storage):
    """The document is the customer's, so booking B's checklist changes too -
    an admin approving B from a page loaded before the upload must get a 409."""
    _, (a, b, decided) = _fresh_customer_bookings(3)
    _persistence().update_decision(id=decided, expected_version=_version(decided), status="rejected",
                                   kyc_status="rejected", admin_note=None)
    b_before, decided_before = _version(b), _version(decided)

    resp = _post(a, note="name fixed")
    assert resp.status_code == 200, resp.text
    assert _version(b) == b_before + 1
    assert _version(decided) == decided_before  # only bookings still under review
    b_history = client.get(f"/admin/bookings/{b}", headers=_headers()).json()["decision_history"]
    assert b_history[-1]["action"] == "kyc_document_uploaded"
    assert f"via booking {a}" in b_history[-1]["note"] and "name fixed" in b_history[-1]["note"]

    stale = client.post(f"/admin/bookings/{b}/approve", json={"note": "ok", "version": b_before},
                        headers=_headers())
    assert stale.status_code == 409 and stale.json()["detail"] == "version_conflict"


# ---------- document owner resolution ----------

def _booking_owned_by(owner_id):
    return _persistence().create_booking(
        payment_id=str(uuid.uuid4()), inventory_id=str(uuid.uuid4()), customer_id=owner_id,
        project_name="Divine Meadows", unit_number="B-1", amount=1000,
    ).id


def test_broker_owned_booking_gets_broker_role_and_lock(storage):
    from Divinepersistence.persistence_broker import persistenceBroker
    broker = persistenceBroker().create_user(username=f"brk_{uuid.uuid4().hex[:8]}", password_hash="x")
    booking_id = _booking_owned_by(broker.id)
    resp = _post(booking_id)
    assert resp.status_code == 200, resp.text
    audit, = _audit_rows(booking_id)
    doc = admin_booking_kyc_api._booking_service.document_service._persistence.get_by_id(audit["document_id"])
    assert doc.owner_id == broker.id and doc.owner_role == "broker"


def test_booking_with_unknown_owner_fails_cleanly(storage):
    booking_id = _booking_owned_by("Z99999")
    before = _version(booking_id)
    resp = _post(booking_id)
    assert resp.status_code == 500 and _code(resp) == "internal_error"
    assert storage.deleted == [storage.uploaded[0][0]]
    assert _version(booking_id) == before and _audit_rows(booking_id) == []


# ---------- saved upload whose detail reload fails ----------

def test_detail_reload_failure_after_save_is_reported_as_saved(storage, monkeypatch):
    booking_id = _new_booking()
    version = _version(booking_id)
    headers = _headers()
    svc = admin_booking_kyc_api._document_upload_service
    real_get_detail = svc._get_detail

    def flaky(_booking_id):
        raise RuntimeError("get_detail_failed")

    monkeypatch.setattr(svc, "_get_detail", flaky)
    resp = _post(booking_id, version=version, headers=headers)
    assert resp.status_code == 503 and _code(resp) == "document_saved_refresh_failed"
    assert resp.json()["detail"]["saved"] is True
    assert _version(booking_id) == version + 1  # the upload really is persisted

    # The documented recovery: retry with the SAME key -> replay, no second write.
    monkeypatch.setattr(svc, "_get_detail", real_get_detail)
    retry = _post(booking_id, version=version, headers=headers)
    assert retry.status_code == 200 and retry.headers.get("Idempotent-Replayed") == "true"
    assert len(_audit_rows(booking_id)) == 1 and len(storage.uploaded) == 1


# ---------- upload limit env vars ----------

def test_bad_upload_limit_env_values_do_not_crash_import(monkeypatch):
    """Loads an isolated copy of the module (the real one, and its exception
    class the API catches, stay untouched)."""
    import importlib.util
    monkeypatch.setenv("BOOKING_DOC_UPLOADS_PER_ADMIN_PER_MINUTE", "10/min")
    monkeypatch.setenv("BOOKING_DOC_UPLOADS_PER_BOOKING_PER_HOUR", "0")
    spec = importlib.util.spec_from_file_location("_isolated_upload_module", upload_module.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert isolated.ADMIN_UPLOADS_PER_MINUTE == 10
    assert isolated.BOOKING_UPLOADS_PER_HOUR == 20

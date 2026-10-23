"""Unit tests for serviceBookingDocumentUpload with mocked persistence/storage -
covers the branches the HTTP tests can't reach deterministically (commit-time
races, the duplicate-key IntegrityError path) plus the pure validators."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from sqlalchemy.exc import IntegrityError

from DivineService.service_booking_document_upload import (
    serviceBookingDocumentUpload, BookingDocumentUploadError, validate_document_file, clean_note,
    _sanitize_filename, MAX_UPLOAD_BYTES,
)


def _png() -> bytes:
    return cv2.imencode(".png", np.zeros((4, 4, 3), dtype=np.uint8))[1].tobytes()


def _jpg() -> bytes:
    return cv2.imencode(".jpg", np.zeros((4, 4, 3), dtype=np.uint8))[1].tobytes()


_PDF = b"%PDF-1.4\n%%EOF\n"
_BOOKING = SimpleNamespace(id="BKG-1", customer_id="C00001", status="pending_kyc_review", version=3)


def _service(**persistence_overrides):
    persistence = MagicMock()
    persistence.get_document_upload_by_key.return_value = None
    persistence.count_recent_document_uploads.return_value = (0, 0)
    persistence.get_by_id.return_value = _BOOKING
    persistence.record_admin_document_upload.return_value = ("ok", _BOOKING)
    for name, value in persistence_overrides.items():
        setattr(persistence, name, value)
    documents = MagicMock(bucket="documents")
    get_detail = MagicMock(return_value={"id": "BKG-1"})
    return serviceBookingDocumentUpload(persistence, documents, get_detail), persistence, documents, get_detail


def _upload(svc, **kwargs):
    args = dict(booking_id="BKG-1", document_type="pan_card", file_bytes=_png(), filename="pan.png",
                client_content_type="image/png", expected_version=3, note=None,
                idempotency_key="k", admin_id="DV0002", request_id="rid")
    args.update(kwargs)
    return svc.upload(**args)


# ---------- validators ----------

@pytest.mark.parametrize("doc_type,data,name,mime,expected", [
    ("pan_card", _png(), "a.png", "image/png", ("image/png", "png")),
    ("pan_card", _jpg(), "a.jpeg", "image/jpeg", ("image/jpeg", "jpg")),
    ("pan_card", _jpg(), "A.JPG", "", ("image/jpeg", "jpg")),
    ("cancelled_cheque", _PDF, "c.pdf", "application/pdf", ("application/pdf", "pdf")),
    ("cancelled_cheque", _PDF, "c.pdf", "application/octet-stream", ("application/pdf", "pdf")),
    ("cancelled_cheque", _png(), "c.png", "image/png; charset=binary", ("image/png", "png")),
])
def test_validate_accepts(doc_type, data, name, mime, expected):
    assert validate_document_file(doc_type, data, name, mime) == expected


@pytest.mark.parametrize("doc_type,data,name,mime,code", [
    ("pan_card", b"", "a.png", "image/png", "validation_error"),
    ("pan_card", _PDF, "a.pdf", "application/pdf", "unsupported_media_type"),       # PDF not for images
    ("aadhaar_front", _PDF, "a.pdf", "application/pdf", "unsupported_media_type"),
    ("pan_card", _png(), "a.jpg", "image/png", "unsupported_media_type"),          # ext vs bytes
    ("pan_card", _png(), "a.png", "image/jpeg", "unsupported_media_type"),         # client MIME vs bytes
    ("pan_card", b"GIF89a....", "a.gif", "image/gif", "unsupported_media_type"),
    ("cancelled_cheque", b"%PDF-1.4 /Encrypt", "c.pdf", "application/pdf", "unsupported_media_type"),
    ("pan_card", b"\xff\xd8\xff" + b"\x00" * 32, "a.jpg", "image/jpeg", "unsupported_media_type"),  # undecodable
])
def test_validate_rejects(doc_type, data, name, mime, code):
    with pytest.raises(BookingDocumentUploadError) as exc:
        validate_document_file(doc_type, data, name, mime)
    assert exc.value.code == code


def test_validate_size_cap_is_exact():
    at_cap = b"%PDF-" + b"0" * (MAX_UPLOAD_BYTES - 5)
    assert validate_document_file("cancelled_cheque", at_cap, "c.pdf", "application/pdf")[0] == "application/pdf"
    with pytest.raises(BookingDocumentUploadError) as exc:
        validate_document_file("cancelled_cheque", at_cap + b"0", "c.pdf", "application/pdf")
    assert exc.value.code == "file_too_large"


def test_clean_note():
    assert clean_note(None) is None
    assert clean_note("   ") is None
    assert clean_note("  ok\nline\t2 ") == "ok\nline\t2"
    assert clean_note("x" * 500) == "x" * 500
    for bad in ("x" * 501, "a\x00b", "a\x1bb"):
        with pytest.raises(BookingDocumentUploadError) as exc:
            clean_note(bad)
        assert exc.value.code == "validation_error"


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd.png", "passwd.png"),
    ("C:\\Users\\x\\pan.pdf", "pan.pdf"),
    ("pa\x00n.png", "pan.png"),
    (None, ""),
    ("a" * 300 + ".png", ("a" * 300 + ".png")[-150:]),
])
def test_sanitize_filename(raw, expected):
    assert _sanitize_filename(raw) == expected


# ---------- upload flow ----------

def test_happy_path_stores_then_commits_with_limits_and_metadata():
    svc, persistence, documents, get_detail = _service()
    detail, replayed = _upload(svc, note=" hi ")
    assert (detail, replayed) == ({"id": "BKG-1"}, False)
    path, data, mime = documents.store_object.call_args.args
    assert path.startswith("C00001/pan_card_") and path.endswith(".png") and mime == "image/png"
    kwargs = persistence.record_admin_document_upload.call_args.kwargs
    assert kwargs["note"] == "hi" and kwargs["request_id"] == "rid"
    assert kwargs["document"]["owner_id"] == "C00001" and kwargs["document"]["storage_path"] == path
    assert kwargs["admin_since"] > kwargs["booking_since"]  # 1 minute window vs 1 hour window
    assert kwargs["admin_limit"] == 10 and kwargs["booking_limit"] == 20
    documents.delete_object.assert_not_called()


def test_replay_skips_storage_and_rate_limits():
    svc, persistence, documents, get_detail = _service()
    fp = svc._fingerprint("BKG-1", "pan_card", 3, None, __import__("hashlib").sha256(_png()).hexdigest())
    persistence.get_document_upload_by_key.return_value = SimpleNamespace(request_fingerprint=fp, booking_id="BKG-1")
    assert _upload(svc) == ({"id": "BKG-1"}, True)
    documents.store_object.assert_not_called()
    persistence.count_recent_document_uploads.assert_not_called()


def test_fingerprint_covers_every_request_field():
    base = serviceBookingDocumentUpload._fingerprint("B", "pan_card", 1, "n", "h")
    for variant in (("X", "pan_card", 1, "n", "h"), ("B", "aadhaar_front", 1, "n", "h"),
                    ("B", "pan_card", 2, "n", "h"), ("B", "pan_card", 1, "m", "h"),
                    ("B", "pan_card", 1, "n", "g"), ("B", "pan_card", 1, None, "h")):
        assert serviceBookingDocumentUpload._fingerprint(*variant) != base


@pytest.mark.parametrize("counts,retry_after", [((10, 0), 60), ((0, 20), 3600)])
def test_precheck_rate_limits_stop_before_storage(counts, retry_after):
    svc, persistence, documents, _ = _service()
    persistence.count_recent_document_uploads.return_value = counts
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "rate_limited" and exc.value.retry_after == retry_after
    documents.store_object.assert_not_called()


@pytest.mark.parametrize("outcome,retry_after", [("admin_rate_limited", 60), ("booking_rate_limited", 3600)])
def test_commit_time_rate_limit_drops_object(outcome, retry_after):
    svc, persistence, documents, _ = _service()
    persistence.record_admin_document_upload.return_value = (outcome, None)
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "rate_limited" and exc.value.retry_after == retry_after
    documents.delete_object.assert_called_once()


@pytest.mark.parametrize("current,code,extra", [
    (SimpleNamespace(status="pending_kyc_review", version=4), "version_conflict", {"current_version": 4}),
    (SimpleNamespace(status="booked", version=4), "booking_not_uploadable", {}),
    (None, "booking_not_found", {}),
    # Guard failed yet the re-read matches - still a conflict, still carries the version.
    (SimpleNamespace(status="pending_kyc_review", version=3), "version_conflict", {"current_version": 3}),
])
def test_stale_commit_reports_current_state(current, code, extra):
    svc, persistence, documents, _ = _service()
    persistence.record_admin_document_upload.return_value = ("stale", current)
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == code and exc.value.extra == extra
    documents.delete_object.assert_called_once()


def test_same_key_race_replays_winner_and_drops_object():
    svc, persistence, documents, _ = _service()
    fp = svc._fingerprint("BKG-1", "pan_card", 3, None, __import__("hashlib").sha256(_png()).hexdigest())
    winner = SimpleNamespace(request_fingerprint=fp, booking_id="BKG-1")
    persistence.get_document_upload_by_key.side_effect = [None, winner]
    persistence.record_admin_document_upload.side_effect = IntegrityError("x", {}, Exception())
    assert _upload(svc) == ({"id": "BKG-1"}, True)
    documents.delete_object.assert_called_once()


def test_same_key_race_with_different_content_conflicts():
    svc, persistence, documents, _ = _service()
    persistence.get_document_upload_by_key.side_effect = [None, SimpleNamespace(request_fingerprint="other",
                                                                                booking_id="BKG-1")]
    persistence.record_admin_document_upload.side_effect = IntegrityError("x", {}, Exception())
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "idempotency_conflict"
    documents.delete_object.assert_called_once()


def test_integrity_error_not_from_the_key_propagates():
    svc, persistence, documents, _ = _service()
    persistence.record_admin_document_upload.side_effect = IntegrityError("x", {}, Exception())
    with pytest.raises(IntegrityError):
        _upload(svc)
    documents.delete_object.assert_called_once()


def test_storage_failure_is_503_and_never_commits():
    svc, persistence, documents, _ = _service()
    documents.store_object.side_effect = RuntimeError("storage_unreachable")
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "document_storage_unavailable"
    persistence.record_admin_document_upload.assert_not_called()


def test_unexpected_commit_error_drops_object_and_propagates():
    svc, persistence, documents, _ = _service()
    persistence.record_admin_document_upload.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        _upload(svc)
    documents.delete_object.assert_called_once()


def test_invalid_document_type_fails_before_any_io():
    svc, persistence, documents, _ = _service()
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc, document_type="passport")
    assert exc.value.code == "validation_error"
    persistence.get_document_upload_by_key.assert_not_called()


# ---------- detail reload after a persisted write ----------

def test_detail_reload_failure_after_commit_is_saved_refresh_failed():
    svc, persistence, documents, get_detail = _service()
    get_detail.side_effect = RuntimeError("get_detail_failed")
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "document_saved_refresh_failed" and exc.value.extra == {"saved": True}
    documents.delete_object.assert_not_called()  # the object belongs to a committed row


def test_replay_detail_failure_is_also_saved_refresh_failed():
    svc, persistence, documents, get_detail = _service()
    fp = svc._fingerprint("BKG-1", "pan_card", 3, None, __import__("hashlib").sha256(_png()).hexdigest())
    persistence.get_document_upload_by_key.return_value = SimpleNamespace(request_fingerprint=fp, booking_id="BKG-1")
    get_detail.side_effect = RuntimeError("boom")
    with pytest.raises(BookingDocumentUploadError) as exc:
        _upload(svc)
    assert exc.value.code == "document_saved_refresh_failed"


def test_detail_not_found_after_commit_still_propagates_value_error():
    svc, persistence, documents, get_detail = _service()
    get_detail.side_effect = ValueError("not_found")
    with pytest.raises(ValueError):
        _upload(svc)


def test_document_role_is_left_to_the_transaction():
    svc, persistence, documents, _ = _service()
    _upload(svc)
    assert "owner_role" not in persistence.record_admin_document_upload.call_args.kwargs["document"]

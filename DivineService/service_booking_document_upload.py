import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.exc import IntegrityError
from DivineService.service_booking_kyc import KYC_DOCUMENT_TYPES
from DivineService.service_document import is_decodable_image
from DivineService.env_config import positive_int_env

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # exact file cap
MAX_NOTE_LENGTH = 500

# Only a booking still under KYC review accepts an admin upload/replacement -
# post-decision corrections would need their own audited workflow.
_UPLOADABLE_STATUS = "pending_kyc_review"

# Per-admin and per-booking limits, counted from divine_booking_document_uploads
# so they hold across app instances. Idempotent replays never reach the count.
ADMIN_UPLOADS_PER_MINUTE = positive_int_env("BOOKING_DOC_UPLOADS_PER_ADMIN_PER_MINUTE", 10)
BOOKING_UPLOADS_PER_HOUR = positive_int_env("BOOKING_DOC_UPLOADS_PER_BOOKING_PER_HOUR", 20)
_ADMIN_WINDOW = timedelta(minutes=1)
_BOOKING_WINDOW = timedelta(hours=1)

# detected MIME -> (storage extension, extensions the filename may carry)
_ALLOWED_TYPES = {
    "application/pdf": ("pdf", {"pdf"}),
    "image/jpeg": ("jpg", {"jpg", "jpeg"}),
    "image/png": ("png", {"png"}),
}
# Aadhaar/PAN/applicant photo are embedded as IMAGES into the generated booking
# application (serviceDocument.generate -> pdf.image), so a PDF there would
# silently render as "could not be embedded". Only the cheque - never embedded -
# may be a PDF; this matches what the customer upload path accepts for the rest.
_PDF_DOCUMENT_TYPES = {"cancelled_cheque"}
_GENERIC_CLIENT_TYPES = {"", "application/octet-stream"}

_RATE_LIMITED = {
    "admin_rate_limited": ("Too many document uploads. Try again shortly.", int(_ADMIN_WINDOW.total_seconds())),
    "booking_rate_limited": ("Too many document uploads for this booking. Try again later.",
                             int(_BOOKING_WINDOW.total_seconds())),
}


class BookingDocumentUploadError(Exception):
    """Expected, client-facing failure. `code` is the stable error code the API
    maps to an HTTP status; `message` is safe to show; `extra` is merged into the
    error body (e.g. current_version); `retry_after` feeds the Retry-After header."""

    def __init__(self, code: str, message: str, extra: dict = None, retry_after: int = None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.retry_after = retry_after


def _detect_mime(file_bytes: bytes):
    if file_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _sanitize_filename(filename: str) -> str:
    """Basename only, no path components or control characters, capped length -
    stored purely as metadata, never used to build a storage path."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[-150:]


def validate_document_file(document_type: str, file_bytes: bytes, filename: str,
                           client_content_type: str) -> tuple:
    """(detected_mime, storage_ext) or raises BookingDocumentUploadError. The bytes
    decide the type: extension and client MIME must both agree with the detected
    signature, and images must actually decode."""
    if not file_bytes:
        raise BookingDocumentUploadError("validation_error", "The uploaded file is empty.")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise BookingDocumentUploadError("file_too_large", "File exceeds the 10 MiB limit.")

    pdf_allowed = document_type in _PDF_DOCUMENT_TYPES
    unsupported = BookingDocumentUploadError(
        "unsupported_media_type",
        "Only PDF, JPG/JPEG, or PNG files are accepted." if pdf_allowed
        else "Only JPG/JPEG or PNG images are accepted for this document.",
    )
    detected = _detect_mime(file_bytes)
    if detected is None or (detected == "application/pdf" and not pdf_allowed):
        raise unsupported
    ext, allowed_exts = _ALLOWED_TYPES[detected]

    name = _sanitize_filename(filename)
    file_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if file_ext not in allowed_exts:
        raise unsupported
    client_type = (client_content_type or "").split(";", 1)[0].strip().lower()
    if client_type not in _GENERIC_CLIENT_TYPES and client_type != detected:
        raise unsupported

    if detected == "application/pdf":
        # No scanner can inspect an encrypted PDF's contents.
        if b"/Encrypt" in file_bytes:
            raise BookingDocumentUploadError("unsupported_media_type", "Encrypted PDFs are not supported.")
    elif not is_decodable_image(file_bytes):
        raise unsupported
    return detected, ext


def clean_note(note: str):
    """Trimmed plain text or None. Control characters other than newline/tab are
    rejected rather than silently stripped."""
    if note is None:
        return None
    note = note.strip()
    if not note:
        return None
    if len(note) > MAX_NOTE_LENGTH:
        raise BookingDocumentUploadError("validation_error", "Note must be at most 500 characters.")
    if any(not ch.isprintable() and ch not in "\n\r\t" for ch in note):
        raise BookingDocumentUploadError("validation_error", "Note contains unsupported characters.")
    return note


def _version_conflict(current_version) -> BookingDocumentUploadError:
    return BookingDocumentUploadError(
        "version_conflict", "Booking changed since it was loaded. Refresh and try again.",
        extra={"current_version": current_version},
    )


class serviceBookingDocumentUpload:
    """Admin upload/replacement of one KYC checklist document on a booking under
    review. The stored file becomes the customer's latest document of that type
    (owner_id = the booking's customer), which is what the Booking Detail
    checklist reads. Never changes kyc_status or status - approval stays a
    separate, explicit admin action.

    `get_detail(booking_id)` renders the response (serviceBookingKyc.get_detail),
    so the upload returns exactly what GET /admin/bookings/{id} would."""

    def __init__(self, booking_persistence, document_service, get_detail):
        self._persistence = booking_persistence
        self._documents = document_service
        self._get_detail = get_detail

    @staticmethod
    def _fingerprint(booking_id, document_type, expected_version, note, content_sha256) -> str:
        parts = [booking_id, document_type, str(expected_version), note or "", content_sha256]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _detail_after_save(self, booking_id: str) -> dict:
        """The refreshed Booking Detail for an upload that IS persisted. If only
        the reload fails, say so precisely - reporting "upload failed" would push
        the client into a duplicate upload or a confusing version_conflict.
        ValueError (booking gone) still propagates as not-found."""
        try:
            return self._get_detail(booking_id)
        except ValueError:
            raise
        except Exception:
            logger.exception("booking_document_upload.detail_reload_failed booking_id=%s", booking_id)
            raise BookingDocumentUploadError(
                "document_saved_refresh_failed",
                "The document was saved, but the booking could not be reloaded. Refresh the page to see it.",
                extra={"saved": True},
            )

    def _replay_or_conflict(self, existing, fingerprint: str) -> dict:
        if existing.request_fingerprint != fingerprint:
            raise BookingDocumentUploadError(
                "idempotency_conflict", "Idempotency key was already used for a different request.",
            )
        return self._detail_after_save(existing.booking_id)

    @staticmethod
    def _raise_rate_limited(outcome: str) -> None:
        message, retry_after = _RATE_LIMITED[outcome]
        raise BookingDocumentUploadError("rate_limited", message, retry_after=retry_after)

    def _precheck_rate_limits(self, admin_id: str, booking_id: str, now: datetime) -> None:
        """Fast fail before any storage write. Advisory only - the authoritative
        check runs again under row locks inside the commit transaction."""
        by_admin, by_booking = self._persistence.count_recent_document_uploads(
            admin_id, booking_id, now - _ADMIN_WINDOW, now - _BOOKING_WINDOW)
        if by_admin >= ADMIN_UPLOADS_PER_MINUTE:
            self._raise_rate_limited("admin_rate_limited")
        if by_booking >= BOOKING_UPLOADS_PER_HOUR:
            self._raise_rate_limited("booking_rate_limited")

    @staticmethod
    def _check_booking(booking, expected_version: int) -> None:
        if not booking:
            raise BookingDocumentUploadError("booking_not_found", "Booking not found.")
        if (getattr(booking, "status", None) or "") != _UPLOADABLE_STATUS:
            raise BookingDocumentUploadError(
                "booking_not_uploadable", "This booking cannot accept document uploads in its current state.",
            )
        if (getattr(booking, "version", None) or 0) != expected_version:
            raise _version_conflict(booking.version)

    def upload(self, booking_id: str, document_type: str, file_bytes: bytes, filename: str,
               client_content_type: str, expected_version: int, note: str, idempotency_key: str,
               admin_id: str, request_id: str = None) -> tuple:
        """Returns (booking_detail, replayed). Raises BookingDocumentUploadError
        for every expected failure; anything else is a genuine internal error."""
        if document_type not in KYC_DOCUMENT_TYPES:
            raise BookingDocumentUploadError("validation_error", "Unsupported document type.")
        note = clean_note(note)
        content_sha256 = hashlib.sha256(file_bytes or b"").hexdigest()
        fingerprint = self._fingerprint(booking_id, document_type, expected_version, note, content_sha256)

        existing = self._persistence.get_document_upload_by_key(admin_id, idempotency_key)
        if existing:
            return self._replay_or_conflict(existing, fingerprint), True

        now = datetime.now(timezone.utc)
        self._precheck_rate_limits(admin_id, booking_id, now)
        booking = self._persistence.get_by_id(booking_id)
        self._check_booking(booking, expected_version)
        detected_mime, ext = validate_document_file(document_type, file_bytes, filename, client_content_type)

        document_id = str(uuid.uuid4())
        # Server-generated object name - nothing from the client's filename.
        object_path = f"{booking.customer_id}/{document_type}_{document_id}.{ext}"
        try:
            self._documents.store_object(object_path, file_bytes, detected_mime)
        except RuntimeError as e:
            logger.warning("booking_document_upload.storage_failed booking_id=%s error=%s", booking_id, e)
            raise BookingDocumentUploadError(
                "document_storage_unavailable", "Document service is temporarily unavailable.",
            )

        document = {
            # owner_role is resolved inside the commit transaction (customer or broker).
            "id": document_id, "owner_id": booking.customer_id,
            "document_type": document_type, "storage_path": object_path, "status": "uploaded",
            "storage_bucket": self._documents.bucket,
            "form_data": {
                "content_type": detected_mime, "source": "admin_upload", "booking_id": booking_id,
                "uploaded_by_admin_id": admin_id, "original_filename": _sanitize_filename(filename),
                "sha256": content_sha256,
            },
        }
        try:
            outcome, row = self._persistence.record_admin_document_upload(
                booking_id=booking_id, expected_version=expected_version, uploadable_status=_UPLOADABLE_STATUS,
                admin_id=admin_id, document=document, idempotency_key=idempotency_key,
                request_fingerprint=fingerprint, request_id=request_id, note=note,
                byte_size=len(file_bytes), content_sha256=content_sha256,
                admin_limit=ADMIN_UPLOADS_PER_MINUTE, admin_since=now - _ADMIN_WINDOW,
                booking_limit=BOOKING_UPLOADS_PER_HOUR, booking_since=now - _BOOKING_WINDOW,
            )
        except IntegrityError:
            # A concurrent request with the same key committed first.
            self._documents.delete_object(object_path)
            existing = self._persistence.get_document_upload_by_key(admin_id, idempotency_key)
            if not existing:
                raise
            return self._replay_or_conflict(existing, fingerprint), True
        except Exception:
            self._documents.delete_object(object_path)
            raise

        if outcome != "ok":
            # Nothing was written - drop the staged object and report why.
            self._documents.delete_object(object_path)
            if outcome in _RATE_LIMITED:
                self._raise_rate_limited(outcome)
            # Lost a race between the pre-check and the commit: `row` is the
            # booking as it is now, so the error carries the real current state.
            self._check_booking(row, expected_version)
            raise _version_conflict(getattr(row, "version", None))

        # get_detail signs fresh preview URLs; a signing failure there already
        # degrades to preview_url=None rather than failing the persisted upload.
        return self._detail_after_save(booking_id), False

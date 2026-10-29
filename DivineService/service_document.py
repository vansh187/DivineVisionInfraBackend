import io
import json
import logging
import os
import re
import uuid
import cv2
import numpy as np
import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from Divinepersistence import persistenceDocument, persistencePayment, persistenceInventory, persistenceBooking
from DivineDTO.models import DocumentGenerateRequestDTO
from DivineService.service_payment_schedule import build_payment_schedule
from DivineService.loan_utils import normalize_indian_amount

logger = logging.getLogger(__name__)

# form_data key spellings for the TOTAL plot amount (aligned with serviceCustomerProfile).
_TOTAL_AMOUNT_KEYS = (
    "total_amount", "totalAmount", "total_consideration", "totalConsideration",
    "total_price", "totalPrice", "consideration", "plot_total_amount", "plotTotalAmount",
    "sale_value", "saleValue",
)
_BOOKING_DATE_KEYS = ("booking_date", "bookingDate", "date")

DEFAULT_SIGNED_URL_EXPIRY_SECONDS = 3600
MAX_PHOTO_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB, matches the KYC upload limit
MAX_BOOKING_APPLICATION_UPLOAD_BYTES = 19 * 1024 * 1024  # 19 MB - room for embedded photos, safely under Zoho CRM's 20 MB per-attachment cap
MAX_FORM_DATA_FIELDS = 200  # guards against a pathological form_data payload bloating the DB row

_UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_ALLOWED_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
_ALLOWED_PHOTO_SIDES = {"front", "back"}
_ALLOWED_BOOKING_APPLICATION_CONTENT_TYPES = {"application/pdf", "application/octet-stream"}
_PDF_MAGIC = b"%PDF-"
_BOOKING_FORMS_BUCKET_ENV = "SUPABASE_BOOKING_FORMS_BUCKET"
_DEFAULT_BOOKING_FORMS_BUCKET = "Booking_Forms"
_PAID_PAYMENT_STATUS = "paid"

# document_type this applies to - other document_type values (e.g. the "kyc"
# placeholder used in older tests) generate without requiring/attaching anything,
# so as not to couple every possible future document type to this specific gate.
_GATED_DOCUMENT_TYPE = "booking_application"
_REQUIRED_PHOTO_DOCUMENT_TYPES = ("aadhaar_front", "aadhaar_back", "pan_card", "applicant_photo", "co_applicant_photo")
_ATTACHMENT_LABELS = {
    "aadhaar_front": "Aadhaar Card - Front",
    "aadhaar_back": "Aadhaar Card - Back",
    "pan_card": "PAN Card",
}
# applicant_photo/co_applicant_photo are NOT in _ATTACHMENT_LABELS above - they don't get
# their own full page like the identity documents. Instead they're placed on the right-hand
# side of the applicant's/co-applicant's own details section (see _render_applicant_section).
_PHOTO_DOCUMENT_TYPES = ("applicant_photo", "co_applicant_photo")

# A form_data key is treated as belonging to the co-applicant section if it mentions
# "co applicant" in any common casing/separator style (coApplicantName, co_applicant_name,
# "Co-Applicant Name", etc.) - everything else (including plain "applicant..." keys) is
# treated as the applicant's own section.
_CO_APPLICANT_KEY_RE = re.compile(r"co[\s_-]?applicant", re.IGNORECASE)
_PHOTO_COLUMN_WIDTH_MM = 45
_PHOTO_COLUMN_HEIGHT_MM = 55
_PHOTO_COLUMN_GAP_MM = 6


def _first_present(form_data: dict, keys) -> str:
    for key in keys:
        value = (form_data or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _split_applicant_fields(form_data: dict) -> tuple:
    applicant_fields, co_applicant_fields = {}, {}
    for key, value in (form_data or {}).items():
        if _CO_APPLICANT_KEY_RE.search(str(key)):
            co_applicant_fields[key] = value
        else:
            applicant_fields[key] = value
    return applicant_fields, co_applicant_fields


def _safe_path_segment(value: str, fallback: str = "document") -> str:
    """Reduce a user-supplied string to a value safe for use as a storage object-path segment."""
    slug = _UNSAFE_PATH_CHARS.sub("_", value).strip("_")[:50]
    return slug or fallback


def is_decodable_image(file_bytes: bytes) -> bool:
    """True when the bytes really decode as an image - Content-Type and file
    signatures are claims; decoding is the proof."""
    try:
        return cv2.imdecode(np.frombuffer(file_bytes, dtype=np.uint8), cv2.IMREAD_COLOR) is not None
    except cv2.error:
        return False


def _pdf_safe_text(value) -> str:
    """FPDF's core Helvetica font only supports Latin-1; replace anything outside it rather than crash."""
    return str(value).encode("latin-1", errors="replace").decode("latin-1")


class serviceDocument:
    def __init__(self, persistence: persistenceDocument = None, payment_persistence: persistencePayment = None,
                 inventory_persistence: persistenceInventory = None, booking_persistence: persistenceBooking = None):
        self._persistence = persistence or persistenceDocument()
        self._payment_persistence = payment_persistence or persistencePayment()
        try:
            self._inventory_persistence = inventory_persistence or persistenceInventory()
        except Exception as e:
            logger.warning("document_inventory_persistence_init_failed: %s", e)
            self._inventory_persistence = None
        # Only used by the booking-application-upload safety net below, to create
        # the divine_bookings row a fresh KYC-review hold needs - see
        # _confirm_inventory_booked. Guarded the same way as inventory_persistence:
        # a missing/broken dependency here must never stop a document from being
        # stored, it only degrades this one fallback path.
        try:
            self._booking_persistence = booking_persistence or persistenceBooking()
        except Exception as e:
            logger.warning("document_booking_persistence_init_failed: %s", e)
            self._booking_persistence = None
        self._supabase_url = os.getenv("SUPABASE_URL")
        self._service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        self._bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "documents")
        self._booking_forms_bucket = os.getenv(_BOOKING_FORMS_BUCKET_ENV, _DEFAULT_BOOKING_FORMS_BUCKET)
        # Reused across every Supabase Storage call this instance makes - keeps the
        # TCP/TLS connection to Supabase warm instead of paying a fresh handshake on
        # every single sign/upload/download/delete call.
        self._http = requests.Session()

    def _render_pdf(self, document_type: str, form_data: dict, attachments: dict = None, photos: dict = None) -> bytes:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _pdf_safe_text(document_type), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        if document_type == _GATED_DOCUMENT_TYPE:
            applicant_fields, co_applicant_fields = _split_applicant_fields(form_data)
            self._render_applicant_section(pdf, "Applicant Details", applicant_fields, (photos or {}).get("applicant_photo"))
            pdf.add_page()
            self._render_applicant_section(pdf, "Co-Applicant Details", co_applicant_fields, (photos or {}).get("co_applicant_photo"))
        else:
            pdf.set_font("Helvetica", "", 12)
            for key, value in (form_data or {}).items():
                line = f"{_pdf_safe_text(key)}: {_pdf_safe_text(value)}"
                pdf.multi_cell(0, 8, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        for label, image_bytes in (attachments or {}).items():
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 14)
            pdf.cell(0, 10, _pdf_safe_text(label), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            try:
                pdf.image(io.BytesIO(image_bytes), x=10, w=pdf.epw)
            except Exception:
                # A corrupt/unreadable attachment shouldn't fail the whole document -
                # note it and move on, since the text page has already succeeded.
                pdf.set_font("Helvetica", "", 11)
                pdf.multi_cell(0, 8, "(This attachment could not be embedded.)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        return bytes(pdf.output())

    def _render_applicant_section(self, pdf: FPDF, heading: str, fields: dict, photo_bytes: bytes) -> None:
        """Renders one applicant's/co-applicant's details on the left with their photo
        placed on the right-hand side of the same section."""
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, _pdf_safe_text(heading), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        section_top_y = pdf.get_y()
        text_column_width = pdf.epw - _PHOTO_COLUMN_WIDTH_MM - _PHOTO_COLUMN_GAP_MM

        pdf.set_font("Helvetica", "", 12)
        if fields:
            for key, value in fields.items():
                line = f"{_pdf_safe_text(key)}: {_pdf_safe_text(value)}"
                pdf.multi_cell(text_column_width, 8, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            pdf.multi_cell(text_column_width, 8, "(no details provided)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        text_bottom_y = pdf.get_y()

        photo_bottom_y = section_top_y
        if photo_bytes:
            photo_x = pdf.l_margin + text_column_width + _PHOTO_COLUMN_GAP_MM
            try:
                pdf.image(io.BytesIO(photo_bytes), x=photo_x, y=section_top_y,
                          w=_PHOTO_COLUMN_WIDTH_MM, h=_PHOTO_COLUMN_HEIGHT_MM)
                photo_bottom_y = section_top_y + _PHOTO_COLUMN_HEIGHT_MM
            except Exception:
                # A corrupt/unreadable photo shouldn't fail the whole document - note it
                # and move on, since the text column has already succeeded.
                pdf.set_xy(photo_x, section_top_y)
                pdf.set_font("Helvetica", "", 9)
                pdf.multi_cell(_PHOTO_COLUMN_WIDTH_MM, 6, "(photo could not be embedded)")
                photo_bottom_y = pdf.get_y()

        pdf.set_xy(pdf.l_margin, max(text_bottom_y, photo_bottom_y) + 4)

    def _upload_to_storage(self, object_path: str, file_bytes: bytes, content_type: str = "application/pdf", bucket: str = None) -> None:
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        upload_url = f"{self._supabase_url}/storage/v1/object/{bucket or self._bucket}/{object_path}"
        try:
            resp = requests.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {self._service_key}",
                    "Content-Type": content_type,
                    "x-upsert": "false",
                },
                data=file_bytes,
                timeout=30,
            )
        except requests.exceptions.RequestException:
            raise RuntimeError("storage_unreachable")
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"storage_upload_failed:{resp.status_code}")

    def _delete_from_storage(self, object_path: str, bucket: str = None) -> None:
        """Best-effort cleanup for an uploaded object that never made it into a DB row. Never raises."""
        if not self._supabase_url or not self._service_key:
            return
        try:
            delete_url = f"{self._supabase_url}/storage/v1/object/{bucket or self._bucket}/{object_path}"
            requests.delete(
                delete_url,
                headers={"Authorization": f"Bearer {self._service_key}"},
                timeout=30,
            )
        except Exception:
            pass

    @property
    def bucket(self) -> str:
        """The bucket identity documents (KYC photos/scans) are stored in."""
        return self._bucket

    def store_object(self, object_path: str, file_bytes: bytes, content_type: str) -> None:
        """Public entry point for writing a KYC object to the default bucket.
        Raises RuntimeError('storage_*') when storage is unconfigured/unreachable."""
        self._upload_to_storage(object_path, file_bytes, content_type)

    def delete_object(self, object_path: str) -> None:
        """Best-effort removal of an object store_object wrote. Never raises."""
        self._delete_from_storage(object_path)

    def _sign_url(self, object_path: str, expires_in: int = DEFAULT_SIGNED_URL_EXPIRY_SECONDS, bucket: str = None) -> str:
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        sign_url = f"{self._supabase_url}/storage/v1/object/sign/{bucket or self._bucket}/{object_path}"
        try:
            resp = self._http.post(
                sign_url,
                headers={
                    "Authorization": f"Bearer {self._service_key}",
                    "Content-Type": "application/json",
                },
                json={"expiresIn": expires_in},
                timeout=30,
            )
        except requests.exceptions.RequestException:
            raise RuntimeError("storage_unreachable")
        if resp.status_code != 200:
            raise RuntimeError(f"storage_sign_failed:{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("storage_sign_failed:invalid_response")
        signed_url = data.get("signedURL") if isinstance(data, dict) else None
        if not signed_url:
            raise RuntimeError("storage_sign_failed:no_url")
        return f"{self._supabase_url}/storage/v1{signed_url}"

    def _sign_urls_batch(self, object_paths: list, expires_in: int = DEFAULT_SIGNED_URL_EXPIRY_SECONDS,
                          bucket: str = None) -> dict:
        """One Supabase Storage call signing MANY paths at once (the plural
        /object/sign/{bucket} endpoint, not the singular /object/sign/{bucket}/{path}
        _sign_url uses) - for admin_get_latest_batch, where signing 5 KYC documents
        one at a time was the entire cost of a slow admin booking-detail page.
        Returns {object_path: signed_url_or_None} - a path Supabase couldn't sign
        (its own per-path "error") maps to None rather than failing every other
        path in the batch, same per-item tolerance _document_checklist already
        relies on from the old one-call-per-doc code. Raises only if the WHOLE
        batch call itself fails (config/network/non-200/unparseable body) - never
        for an individual path's error."""
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        if not object_paths:
            return {}
        sign_url = f"{self._supabase_url}/storage/v1/object/sign/{bucket or self._bucket}"
        try:
            resp = self._http.post(
                sign_url,
                headers={
                    "Authorization": f"Bearer {self._service_key}",
                    "Content-Type": "application/json",
                },
                json={"paths": object_paths, "expiresIn": expires_in},
                timeout=30,
            )
        except requests.exceptions.RequestException:
            raise RuntimeError("storage_unreachable")
        if resp.status_code != 200:
            raise RuntimeError(f"storage_sign_failed:{resp.status_code}")
        try:
            items = resp.json()
        except ValueError:
            raise RuntimeError("storage_sign_failed:invalid_response")
        if not isinstance(items, list):
            raise RuntimeError("storage_sign_failed:invalid_response")

        result = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            signed_url = item.get("signedURL")
            if path and signed_url and not item.get("error"):
                result[path] = f"{self._supabase_url}/storage/v1{signed_url}"
        return result

    def _download_from_storage(self, object_path: str, bucket: str = None) -> bytes:
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        download_url = f"{self._supabase_url}/storage/v1/object/{bucket or self._bucket}/{object_path}"
        try:
            # Reuse the service's pooled HTTP session. The previous top-level
            # requests.get opened a fresh connection for every form download,
            # adding a TCP/TLS handshake to the storage round trip.
            resp = self._http.get(
                download_url,
                headers={"Authorization": f"Bearer {self._service_key}"},
                timeout=30,
            )
        except requests.exceptions.RequestException:
            raise RuntimeError("storage_unreachable")
        if resp.status_code != 200:
            raise RuntimeError(f"storage_download_failed:{resp.status_code}")
        return resp.content

    def generate(self, dto: DocumentGenerateRequestDTO, owner_id: str, owner_role: str):
        attachment_bytes = {}
        photo_bytes = {}
        if dto.document_type == _GATED_DOCUMENT_TYPE:
            missing = []
            records = {}
            for doc_type in _REQUIRED_PHOTO_DOCUMENT_TYPES:
                record = self._persistence.get_latest_by_owner_and_type(owner_id, doc_type)
                if not record:
                    missing.append(doc_type)
                else:
                    records[doc_type] = record
            if missing:
                raise ValueError(f"documents_incomplete:{','.join(missing)}")
            for doc_type, record in records.items():
                if doc_type in _PHOTO_DOCUMENT_TYPES:
                    photo_bytes[doc_type] = self._download_from_storage(record.storage_path)
                else:
                    attachment_bytes[_ATTACHMENT_LABELS[doc_type]] = self._download_from_storage(record.storage_path)

        document_id = str(uuid.uuid4())
        safe_type = _safe_path_segment(dto.document_type)
        object_path = f"{owner_id}/{safe_type}_{document_id}.pdf"

        pdf_bytes = self._render_pdf(dto.document_type, dto.form_data, attachment_bytes, photo_bytes)
        self._upload_to_storage(object_path, pdf_bytes)

        try:
            doc = self._persistence.create_document(
                id=document_id,
                owner_id=owner_id,
                owner_role=owner_role,
                document_type=dto.document_type,
                form_data=dto.form_data,
                storage_path=object_path,
                status="generated",
                storage_bucket=self._bucket,
            )
        except Exception:
            self._delete_from_storage(object_path)
            raise

        signed_url = self._sign_url(object_path)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def _upload_photo(self, file_bytes: bytes, content_type: str, document_type: str, owner_id: str, owner_role: str):
        """Stores a raw photo (Aadhaar front/back, PAN card) straight to Supabase Storage for
        later use in document generation - no parsing or verification, unlike the QR KYC flow."""
        if not file_bytes:
            raise ValueError("empty_file")
        if len(file_bytes) > MAX_PHOTO_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        ext = _ALLOWED_PHOTO_TYPES.get((content_type or "").lower())
        if not ext:
            raise ValueError("unsupported_file_type")
        # Content-Type is a client-supplied header, not a guarantee of what the bytes
        # actually are - decoding confirms this is a real, readable image (matching how
        # the QR KYC flow validates uploads) rather than trusting the header alone, which
        # would let an arbitrary file through mislabeled as image/jpeg or image/png.
        if not is_decodable_image(file_bytes):
            raise ValueError("unsupported_file_type")

        document_id = str(uuid.uuid4())
        object_path = f"{owner_id}/{document_type}_{document_id}.{ext}"

        self._upload_to_storage(object_path, file_bytes, content_type)

        try:
            doc = self._persistence.create_document(
                id=document_id,
                owner_id=owner_id,
                owner_role=owner_role,
                document_type=document_type,
                form_data={"content_type": content_type},
                storage_path=object_path,
                status="uploaded",
                storage_bucket=self._bucket,
            )
        except Exception:
            self._delete_from_storage(object_path)
            raise

        signed_url = self._sign_url(object_path)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def upload_aadhaar_photo(self, file_bytes: bytes, content_type: str, side: str, owner_id: str, owner_role: str):
        if side not in _ALLOWED_PHOTO_SIDES:
            raise ValueError("invalid_side")
        return self._upload_photo(file_bytes, content_type, f"aadhaar_{side}", owner_id, owner_role)

    def upload_pan_photo(self, file_bytes: bytes, content_type: str, owner_id: str, owner_role: str):
        return self._upload_photo(file_bytes, content_type, "pan_card", owner_id, owner_role)

    def upload_applicant_photo(self, file_bytes: bytes, content_type: str, owner_id: str, owner_role: str):
        return self._upload_photo(file_bytes, content_type, "applicant_photo", owner_id, owner_role)

    def upload_co_applicant_photo(self, file_bytes: bytes, content_type: str, owner_id: str, owner_role: str):
        return self._upload_photo(file_bytes, content_type, "co_applicant_photo", owner_id, owner_role)

    def upload_cancelled_cheque(self, file_bytes: bytes, content_type: str, owner_id: str, owner_role: str):
        """Bank-account-verification document for the Booking KYC review checklist
        (needed to know where an approved refund should go, if one is ever needed
        later) - same storage/validation path as the other identity photos."""
        return self._upload_photo(file_bytes, content_type, "cancelled_cheque", owner_id, owner_role)

    def upload_booking_application(
        self,
        file_bytes: bytes,
        content_type: str,
        document_type: str,
        project_id: str,
        payment_id: str,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        form_data_raw: str,
        owner_id: str,
        owner_role: str,
        inventory_id: str = None,
    ):
        """Stores a signed booking-application PDF in the Booking_Forms bucket, linked to the
        payment record that unlocked it. Unlike generate(), the PDF already exists client-side
        (it's a filled/signed form) - this endpoint only validates, stores, and links it.

        `inventory_id` is a safety-net: if the payment settled through an older flow that
        never carried it, this confirms the plot as 'booked' here (idempotent - a no-op
        when servicePayment already flipped it). The result is attached to the returned
        doc as `doc.inventory_id` / `doc.inventory_status` ("pending_kyc_review" | "conflict" | None)."""
        if not file_bytes:
            raise ValueError("empty_file")
        if len(file_bytes) > MAX_BOOKING_APPLICATION_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        if (content_type or "").lower() not in _ALLOWED_BOOKING_APPLICATION_CONTENT_TYPES:
            raise ValueError("unsupported_file_type")
        # Content-Type is a client-supplied header, not a guarantee of what the bytes actually
        # are - confirm the file starts with the PDF magic bytes rather than trusting the header.
        if not file_bytes.startswith(_PDF_MAGIC):
            raise ValueError("unsupported_file_type")

        if not document_type or not document_type.strip():
            raise ValueError("document_type_required")
        if not project_id or not project_id.strip():
            raise ValueError("project_id_required")
        if not payment_id or not payment_id.strip():
            raise ValueError("payment_id_required")

        try:
            form_data = json.loads(form_data_raw) if form_data_raw else {}
        except (TypeError, ValueError):
            raise ValueError("invalid_form_data")
        if not isinstance(form_data, dict):
            raise ValueError("invalid_form_data")
        if len(form_data) > MAX_FORM_DATA_FIELDS:
            raise ValueError("form_data_too_large")

        payment = self._payment_persistence.get_by_id(payment_id)
        if not payment:
            raise ValueError("payment_not_found")
        if payment.owner_id != owner_id:
            raise PermissionError("forbidden")
        if payment.status != _PAID_PAYMENT_STATUS:
            raise ValueError("payment_not_completed")
        # Cross-check the Razorpay identifiers the client sent against what's actually on the
        # payment record - including when the record has none (e.g. a cash payment, which is
        # legitimately status="paid" with razorpay_order_id/razorpay_payment_id both NULL).
        # Requiring an exact match even against None means a client can't attach an unverified,
        # self-reported order/payment id to a cash payment's document by supplying one while the
        # record has none - the client-supplied values are never trusted for storage either;
        # only payment.razorpay_order_id / payment.razorpay_payment_id are persisted below.
        if razorpay_order_id and razorpay_order_id != payment.razorpay_order_id:
            raise ValueError("payment_mismatch")
        if razorpay_payment_id and razorpay_payment_id != payment.razorpay_payment_id:
            raise ValueError("payment_mismatch")

        # The TOTAL plot amount is now required so a full payment schedule can be
        # derived (the booking amount alone -> blank demand/allotment letters).
        total_amount = normalize_indian_amount(_first_present(form_data, _TOTAL_AMOUNT_KEYS))
        if not total_amount or total_amount <= 0:
            raise ValueError("total_amount_required")
        booking_amount = float(payment.amount or 0)   # what was actually paid now
        if booking_amount > total_amount:
            raise ValueError("booking_amount_exceeds_total")
        plan = build_payment_schedule(
            total_amount=total_amount,
            booking_amount=booking_amount,
            booking_date=_first_present(form_data, _BOOKING_DATE_KEYS),
        )
        # Persist the derived plan back into form_data under canonical keys so the
        # stored booking application is self-contained (demand / allotment letters
        # and GET /customer/profile read straight from it).
        form_data["total_consideration"] = plan["total_receivable"]
        form_data["amount_received"] = plan["total_received"]
        form_data["total_outstanding"] = plan["total_outstanding"]
        form_data["total_outstanding_words"] = plan["total_outstanding_words"]
        form_data["booking_date"] = plan["booking_date"]
        form_data["payment_schedule"] = plan["rows"]

        document_id = str(uuid.uuid4())
        safe_type = _safe_path_segment(document_type)
        object_path = f"{owner_id}/{safe_type}_{document_id}.pdf"

        self._upload_to_storage(object_path, file_bytes, "application/pdf", bucket=self._booking_forms_bucket)

        try:
            doc = self._persistence.create_document(
                id=document_id,
                owner_id=owner_id,
                owner_role=owner_role,
                document_type=document_type,
                form_data=form_data,
                storage_path=object_path,
                status="generated",
                storage_bucket=self._booking_forms_bucket,
                project_id=project_id,
                payment_id=payment_id,
                # Always the payment record's own values, never the client-supplied
                # razorpay_order_id/razorpay_payment_id params - those were only used above to
                # verify the caller's claim matches, not as a data source to persist.
                razorpay_order_id=payment.razorpay_order_id,
                razorpay_payment_id=payment.razorpay_payment_id,
            )
        except Exception:
            self._delete_from_storage(object_path, bucket=self._booking_forms_bucket)
            raise

        # No "booking confirmed" email here - the booking still only has a KYC-review
        # HOLD on the unit at this point, not an admin-approved booking. Only a
        # CUSTOMER ever books a plot (brokers have no inventory/booking access), so
        # owner_id here is always the real customer - serviceBookingKyc.approve()
        # emails that same customer (via dispatch_kyc_decision_email) once KYC is
        # actually verified and the unit is booked for real - never earlier, so a
        # premature "congratulations" email can't promise a booking that KYC review
        # might still reject.

        # Safety-net plot lock. This ONLY ever re-confirms the exact unit the payment
        # itself was created for (payment.purpose == 'plot_booking' + payment.inventory_id) -
        # it never binds a plot from the client-supplied form field, so a caller can't
        # attach an arbitrary plot to an unrelated paid payment. book_unit is idempotent
        # for the same payment id, so this is a no-op when servicePayment already flipped
        # the unit at settle time.
        self._confirm_inventory_booked(
            doc,
            payment=payment,
            client_inventory_id=(inventory_id or "").strip() or None,
            owner_id=owner_id,
        )

        # Materialise the per-milestone rows so instalment payments + the payment-due
        # reminder job have a table to work against. Best-effort: never blocks the upload.
        self._create_payment_milestones(doc, plan, owner_id, project_id, payment,
                                        _first_present(form_data, _BOOKING_DATE_KEYS))

        signed_url = self._sign_url(object_path, bucket=self._booking_forms_bucket)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS, plan

    def _create_payment_milestones(self, doc, plan, owner_id, project_id, payment, booking_date_raw):
        try:
            from DivineService.service_milestones import serviceMilestones
            booking_id = getattr(doc, "id", None)
            if not booking_id or not isinstance(plan, dict) or not plan.get("rows"):
                return
            svc = serviceMilestones()
            rows = svc._rows_from_plan(
                plan, booking_id=booking_id, customer_id=owner_id,
                project_id=project_id, inventory_id=getattr(payment, "inventory_id", None),
                booking_payment_id=getattr(payment, "id", None),
                booking_date=booking_date_raw or plan.get("booking_date"),
            )
            if rows:
                svc._persistence.create_milestones(rows)
        except Exception as e:
            logger.warning("document_milestone_create_failed doc_id=%s error=%s",
                           getattr(doc, "id", None), e)

    def _confirm_inventory_booked(self, doc, payment, client_inventory_id: str, owner_id: str) -> None:
        """Attaches doc.inventory_id / doc.inventory_status. Never raises - the document
        is already stored, so a booking-flip miss is logged, not surfaced as a failure.

        Trust rule: the plot is taken from the PAYMENT (purpose 'plot_booking' +
        payment.inventory_id), never from the client form field. The form field is
        only echoed back on the doc and, when the payment carries no inventory_id,
        used purely to log the mismatch.

        Holds the unit for KYC review (does NOT book it outright) - same as the
        payment-settlement path in DivineService/service_payment.py. This is a
        safety net for when that path's own hold didn't fire (a transient error,
        or the client uploaded the booking application before the webhook/verify
        call landed); it must apply the SAME gate, not a shortcut around it -
        otherwise a customer could skip KYC review entirely just by uploading
        this document before the payment's own settlement runs."""
        try:
            payment_purpose = (getattr(payment, "purpose", None) or "other")
            payment_inventory_id = getattr(payment, "inventory_id", None)
            doc.inventory_id = payment_inventory_id or client_inventory_id or None
            doc.inventory_status = None

            if payment_purpose != "plot_booking" or not payment_inventory_id:
                if client_inventory_id and client_inventory_id != payment_inventory_id:
                    logger.info(
                        "document_inventory_id_ignored client=%s payment=%s purpose=%s",
                        client_inventory_id, payment_inventory_id, payment_purpose,
                    )
                return
            if self._inventory_persistence is None:
                return

            unit = self._inventory_persistence.hold_for_kyc_review(
                id=payment_inventory_id, payment_id=getattr(payment, "id", None), customer_id=owner_id,
            )
            doc.inventory_status = "pending_kyc_review" if unit is not None else "conflict"
            if unit is not None:
                self._ensure_booking_record(payment, unit, owner_id)
        except Exception as e:
            logger.warning("document_inventory_confirm_failed error=%s", e)
            try:
                doc.inventory_status = "conflict"
            except Exception:
                pass

    def _ensure_booking_record(self, payment, unit, owner_id: str) -> None:
        """Creates the divine_bookings row this fresh KYC-review hold needs to
        actually be reviewable - without it the unit sits 'pending_kyc_review'
        forever with no way for an admin to Approve/Reject it and no way for the
        customer to see it under GET /bookings/mine. Idempotent for a repeat run
        on the same payment_id (persistenceBooking.create_booking returns the
        existing row instead of erroring) - safe even when
        _apply_booking_to_inventory already created it and this safety net is
        just re-confirming the same hold. Never raises - a failure here is
        logged, not surfaced as a document-upload failure."""
        if self._booking_persistence is None:
            return
        try:
            self._booking_persistence.create_booking(
                payment_id=getattr(payment, "id", None),
                inventory_id=getattr(payment, "inventory_id", None),
                customer_id=owner_id,
                project_name=getattr(unit, "project_name", None),
                unit_number=getattr(unit, "unit_number", None),
                amount=getattr(payment, "amount", None),
            )
        except Exception as e:
            logger.warning("document_booking_record_create_failed payment_id=%s error=%s",
                           getattr(payment, "id", None), e)

    def get(self, document_id: str, requester_id: str, requester_role: str = None):
        """Fetch one document and mint a FRESH signed URL for it, whatever its type -
        identity photos (aadhaar_front / aadhaar_back / pan_card / applicant_photo /
        co_applicant_photo), generated PDFs and booking applications alike. The app
        calls this to refresh an expired signed URL. Authorisation is by ownership
        (owner_id, cross-checked with owner_role); raises ValueError('document_not_found')
        when the id is unknown and PermissionError('forbidden') for someone else's doc."""
        doc = self._persistence.get_by_id(document_id)
        if not doc:
            raise ValueError("document_not_found")
        # owner_id alone currently can't collide across roles (customer/broker IDs use
        # distinct prefixes), so this check only "works" by accident of that ID scheme -
        # comparing owner_role too makes the authorization boundary explicit rather than
        # incidental, in case that ID scheme is ever changed.
        if doc.owner_id != requester_id or (requester_role is not None and doc.owner_role != requester_role):
            raise PermissionError("forbidden")
        # Every row records the bucket it was actually written to at upload time (storage_bucket).
        # Rows created before that column existed have it NULL - those all predate the
        # Booking_Forms bucket, so they fall back to the default bucket, not an inference off
        # some other column that could coincidentally change meaning later.
        bucket = getattr(doc, "storage_bucket", None) or self._bucket
        signed_url = self._sign_url(doc.storage_path, bucket=bucket)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def get_demand_letter(self, document_id: str, requester_id: str, requester_role: str = None):
        """Render the Demand Letter PDF for a booking application from its stored
        form_data (payment schedule + plot/customer details). Returns (pdf_bytes,
        filename). Ownership rules mirror get()."""
        doc = self._persistence.get_by_id(document_id)
        if not doc:
            raise ValueError("not_found")
        if doc.owner_id != requester_id or (requester_role is not None and doc.owner_role != requester_role):
            raise PermissionError("forbidden")
        if "booking" not in (getattr(doc, "document_type", "") or "").lower():
            raise ValueError("not_a_booking_application")

        form_data = getattr(doc, "form_data", None)
        if isinstance(form_data, str):
            try:
                form_data = json.loads(form_data)
            except ValueError:
                form_data = {}
        if not isinstance(form_data, dict):
            form_data = {}

        from DivineService.service_demand_letter import _PLOT_KEYS, _first, generate_demand_letter_pdf
        if not _first(form_data, _PLOT_KEYS):
            # The application form doesn't always carry the plot number - the
            # booking row for the same payment does.
            unit_number = self._booking_unit_number(getattr(doc, "payment_id", None))
            if unit_number:
                form_data = {**form_data, "unit_number": unit_number}
        pdf_bytes = generate_demand_letter_pdf(form_data, doc.owner_id)
        return pdf_bytes, f"demand-letter-{document_id}.pdf"

    def get_latest(self, document_type: str, requester_id: str, requester_role: str = None):
        if not document_type or not document_type.strip():
            raise ValueError("document_type_required")
        doc = self._persistence.get_latest_by_owner_and_type(requester_id, document_type.strip())
        if not doc:
            raise ValueError("not_found")
        if requester_role is not None and doc.owner_role != requester_role:
            raise PermissionError("forbidden")
        bucket = getattr(doc, "storage_bucket", None) or self._bucket
        signed_url = self._sign_url(doc.storage_path, bucket=bucket)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def _booking_unit_number(self, payment_id: str):
        """Plot/unit number of the divine_bookings row for a payment, or None. Never raises."""
        try:
            if not payment_id or self._booking_persistence is None:
                return None
            booking = self._booking_persistence.get_by_payment_id(payment_id)
            return getattr(booking, "unit_number", None) if booking else None
        except Exception as e:
            logger.warning("booking_unit_number_lookup_failed payment_id=%s error=%s", payment_id, e)
            return None

    def get_booking_application_document_id(self, payment_id: str):
        """The booking-application document's own id for a given plot-booking
        payment - this, NOT divine_bookings.id, is what serviceMilestones calls
        "booking_id" everywhere (see service_milestones.py /
        _create_payment_milestones - milestone rows are keyed off the document's
        id so a multi-plot customer's separate booking-application documents each
        get their own isolated milestone plan). Returns None when nothing has
        been uploaded against this payment yet. Never raises."""
        doc = self.get_booking_application_document(payment_id)
        return getattr(doc, "id", None) if doc else None

    def get_booking_application_document(self, payment_id: str):
        """The latest booking-application document row for a given plot-booking
        payment (form_data included), or None when nothing has been uploaded
        against it yet. Never raises."""
        try:
            if not payment_id:
                return None
            return self._persistence.get_latest_by_payment_id(payment_id)
        except Exception as e:
            logger.warning("get_booking_application_document_failed payment_id=%s error=%s", payment_id, e)
            return None

    def get_booking_application_bytes(self, payment_id: str):
        """Fetches the raw bytes of the customer's signed booking-application PDF for a
        given payment (the same file uploaded via upload_booking_application) - used to
        mirror it into Zoho as a Contact attachment once the booking is KYC-approved. No
        document_type filter: payment_id already uniquely scopes this to the one booking
        application uploaded against that payment. Returns (None, None) when there's
        nothing to attach yet rather than raising, since a missing/not-yet-uploaded
        document is an expected state, not an error."""
        try:
            if not payment_id:
                return None, None
            doc = self._persistence.get_latest_by_payment_id(payment_id)
            if not doc:
                return None, None
            bucket = getattr(doc, "storage_bucket", None) or self._bucket
            file_bytes = self._download_from_storage(doc.storage_path, bucket=bucket)
            file_name = f"{getattr(doc, 'document_type', 'booking_application')}-{getattr(doc, 'id', payment_id)}.pdf"
            return file_bytes, file_name
        except Exception as e:
            logger.warning("get_booking_application_bytes_failed payment_id=%s error=%s", payment_id, e)
            return None, None

    def admin_get_latest(self, document_type: str, owner_id: str):
        """Same lookup as get_latest(), but for an admin reviewing a CUSTOMER's
        documents (e.g. the Booking KYC review screen) - no ownership check,
        since the caller isn't the document's owner by design. Authorization is
        the router's get_current_admin dependency, not this method. Returns
        None (not an exception) when the customer hasn't uploaded that document
        type yet, since a missing document is an expected, common state for a
        checklist UI, not an error."""
        try:
            doc = self._persistence.get_latest_by_owner_and_type(owner_id, (document_type or "").strip())
            if not doc:
                return None, None, None
            bucket = getattr(doc, "storage_bucket", None) or self._bucket
            signed_url = self._sign_url(doc.storage_path, bucket=bucket)
            return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS
        except Exception as e:
            logger.warning("admin_get_latest_document_failed document_type=%s owner_id=%s error=%s",
                           document_type, owner_id, e)
            return None, None, None

    def admin_get_latest_batch(self, document_types, owner_id: str) -> dict:
        """Batched sibling of admin_get_latest - one DB query (instead of one per
        document type) plus one Supabase sign call per distinct storage bucket
        involved (normally just one call total, since every KYC document type is
        uploaded to the same bucket) instead of one sign call per document. This
        is what made the admin booking-detail page slow: 5 document types used to
        mean 5 serial DB queries + 5 serial blocking Supabase HTTP calls on a
        single request.

        Returns {document_type: (doc, signed_url, expires_in)} - same 3-tuple
        admin_get_latest returns per type, just for every type at once. A type
        with nothing uploaded, or whose signed-URL failed, is simply absent from
        the dict - callers already do dict.get(doc_type, (None, None, None))."""
        try:
            clean_types = [(t or "").strip() for t in (document_types or []) if (t or "").strip()]
            if not clean_types:
                return {}
            docs = self._persistence.get_latest_by_owner_and_types(owner_id, clean_types)
            if not docs:
                return {}

            by_bucket = {}
            for doc in docs:
                bucket = getattr(doc, "storage_bucket", None) or self._bucket
                by_bucket.setdefault(bucket, []).append(doc)

            signed_by_path = {}
            for bucket, bucket_docs in by_bucket.items():
                try:
                    signed_by_path.update(self._sign_urls_batch(
                        [d.storage_path for d in bucket_docs], bucket=bucket,
                    ))
                except Exception as e:
                    # One bucket's batch sign failing must not lose the other
                    # buckets' results (or crash the whole checklist) - those
                    # documents just come back without a preview_url, same as a
                    # single admin_get_latest call failing for one doc type today.
                    logger.warning("admin_get_latest_batch_sign_failed bucket=%s error=%s", bucket, e)

            result = {}
            for doc in docs:
                signed_url = signed_by_path.get(doc.storage_path)
                result[doc.document_type] = (doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS)
            return result
        except Exception as e:
            logger.warning("admin_get_latest_batch_failed owner_id=%s error=%s", owner_id, e)
            return {}

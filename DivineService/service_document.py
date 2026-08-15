import os
import re
import uuid
import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from Divinepersistence import persistenceDocument
from DivineDTO.models import DocumentGenerateRequestDTO

DEFAULT_SIGNED_URL_EXPIRY_SECONDS = 3600
MAX_PHOTO_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB, matches the KYC upload limit

_UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_ALLOWED_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
_ALLOWED_PHOTO_SIDES = {"front", "back"}


def _safe_path_segment(value: str, fallback: str = "document") -> str:
    """Reduce a user-supplied string to a value safe for use as a storage object-path segment."""
    slug = _UNSAFE_PATH_CHARS.sub("_", value).strip("_")[:50]
    return slug or fallback


def _pdf_safe_text(value) -> str:
    """FPDF's core Helvetica font only supports Latin-1; replace anything outside it rather than crash."""
    return str(value).encode("latin-1", errors="replace").decode("latin-1")


class serviceDocument:
    def __init__(self, persistence: persistenceDocument = None):
        self._persistence = persistence or persistenceDocument()
        self._supabase_url = os.getenv("SUPABASE_URL")
        self._service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        self._bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "documents")

    def _render_pdf(self, document_type: str, form_data: dict) -> bytes:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _pdf_safe_text(document_type), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 12)
        for key, value in form_data.items():
            line = f"{_pdf_safe_text(key)}: {_pdf_safe_text(value)}"
            pdf.multi_cell(0, 8, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return bytes(pdf.output())

    def _upload_to_storage(self, object_path: str, file_bytes: bytes, content_type: str = "application/pdf") -> None:
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        upload_url = f"{self._supabase_url}/storage/v1/object/{self._bucket}/{object_path}"
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

    def _delete_from_storage(self, object_path: str) -> None:
        """Best-effort cleanup for an uploaded object that never made it into a DB row. Never raises."""
        if not self._supabase_url or not self._service_key:
            return
        try:
            delete_url = f"{self._supabase_url}/storage/v1/object/{self._bucket}/{object_path}"
            requests.delete(
                delete_url,
                headers={"Authorization": f"Bearer {self._service_key}"},
                timeout=30,
            )
        except Exception:
            pass

    def _sign_url(self, object_path: str, expires_in: int = DEFAULT_SIGNED_URL_EXPIRY_SECONDS) -> str:
        if not self._supabase_url or not self._service_key:
            raise RuntimeError("storage_not_configured")
        sign_url = f"{self._supabase_url}/storage/v1/object/sign/{self._bucket}/{object_path}"
        try:
            resp = requests.post(
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

    def generate(self, dto: DocumentGenerateRequestDTO, owner_id: str, owner_role: str):
        document_id = str(uuid.uuid4())
        safe_type = _safe_path_segment(dto.document_type)
        object_path = f"{owner_id}/{safe_type}_{document_id}.pdf"

        pdf_bytes = self._render_pdf(dto.document_type, dto.form_data)
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
            )
        except Exception:
            self._delete_from_storage(object_path)
            raise

        signed_url = self._sign_url(object_path)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def upload_aadhaar_photo(self, file_bytes: bytes, content_type: str, side: str, owner_id: str, owner_role: str):
        """Stores a raw Aadhaar front/back photo straight to Supabase Storage for later use in
        document generation - no parsing or verification, unlike the QR KYC flow."""
        if not file_bytes:
            raise ValueError("empty_file")
        if len(file_bytes) > MAX_PHOTO_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        if side not in _ALLOWED_PHOTO_SIDES:
            raise ValueError("invalid_side")
        ext = _ALLOWED_PHOTO_TYPES.get((content_type or "").lower())
        if not ext:
            raise ValueError("unsupported_file_type")

        document_id = str(uuid.uuid4())
        document_type = f"aadhaar_{side}"
        object_path = f"{owner_id}/{document_type}_{document_id}.{ext}"

        self._upload_to_storage(object_path, file_bytes, content_type)

        try:
            doc = self._persistence.create_document(
                id=document_id,
                owner_id=owner_id,
                owner_role=owner_role,
                document_type=document_type,
                form_data={"side": side, "content_type": content_type},
                storage_path=object_path,
                status="uploaded",
            )
        except Exception:
            self._delete_from_storage(object_path)
            raise

        signed_url = self._sign_url(object_path)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

    def get(self, document_id: str, requester_id: str):
        doc = self._persistence.get_by_id(document_id)
        if not doc:
            raise ValueError("not_found")
        if doc.owner_id != requester_id:
            raise PermissionError("forbidden")
        signed_url = self._sign_url(doc.storage_path)
        return doc, signed_url, DEFAULT_SIGNED_URL_EXPIRY_SECONDS

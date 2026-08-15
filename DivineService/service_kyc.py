import io
import os
import cv2
import numpy as np
import zxingcpp
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature
from signxml import XMLVerifier

from Divinepersistence import persistenceKyc
from DivineService.aadhaar_decode import AadhaarSecureQr, AadhaarOfflineXML, AadhaarQrParseError

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB, generous for a photo of a card or the offline-XML zip
_QR_FORMAT = zxingcpp.BarcodeFormat.QRCode


def _load_cert(env_var: str):
    """Loads a UIDAI verification certificate from config. Raises RuntimeError if missing/invalid
    - deliberately NOT a hardcoded/bundled certificate; must be supplied via env var.

    QR (Secure QR) and Offline e-KYC XML are signed with different UIDAI certificates, so each
    flow reads its own env var rather than sharing one."""
    cert_pem = os.getenv(env_var)
    if not cert_pem:
        raise RuntimeError(f"{env_var} environment variable must be set")
    cert_pem_bytes = cert_pem.encode("utf-8")
    try:
        cert = x509.load_pem_x509_certificate(cert_pem_bytes)
    except Exception as e:
        raise RuntimeError(f"{env_var}_invalid: {type(e).__name__}")
    return cert.public_key(), cert_pem_bytes


def _load_qr_cert():
    return _load_cert("UIDAI_QR_CERT_PEM")


def _load_xml_cert():
    return _load_cert("UIDAI_XML_CERT_PEM")


def _mask_aadhaar(reference_id: str) -> str:
    """Aadhaar's Secure QR / Offline XML reference id is last-4-digits + a timestamp, never
    the full Aadhaar number - so there is no full number to mask here, only to format for display."""
    last4 = (reference_id or "")[:4]
    if not (last4.isdigit() and len(last4) == 4):
        last4 = "----"
    return f"XXXXXXXX{last4}"


def _safe_extracted_fields(data: dict) -> dict:
    """Drop internal/reference fields we don't want persisted verbatim, keep the rest as display data."""
    return {k: v for k, v in data.items() if k not in ("referenceid",)}


class serviceKyc:
    def __init__(self, persistence: persistenceKyc = None):
        self._persistence = persistence or persistenceKyc()

    # ---------- QR flow ----------

    def _extract_qr_text(self, image_bytes: bytes) -> str:
        if not image_bytes:
            raise ValueError("empty_file")
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        try:
            arr = np.frombuffer(image_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except cv2.error:
            img = None
        if img is None:
            raise ValueError("unreadable_image")

        # zxing-cpp (the ZXing engine - the same family of decoder most real-world QR
        # scanners, including Android's, are built on) reads Aadhaar-density QR codes
        # far more reliably than cv2.QRCodeDetector, which was measured to have a severe
        # miss rate on dense, real-camera-photo QR codes (previously used here). It
        # already tries rotation/downscale/inversion internally, so only a grayscale
        # fallback is kept as a cheap second attempt for unusual lighting.
        try:
            results = zxingcpp.read_barcodes(img, formats=_QR_FORMAT)
        except Exception:
            results = []
        if not results:
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                results = zxingcpp.read_barcodes(gray, formats=_QR_FORMAT)
            except Exception:
                results = []
        if not results:
            raise ValueError("qr_not_found")
        return results[0].text

    def verify_qr(self, image_bytes: bytes, owner_id: str, owner_role: str):
        text = self._extract_qr_text(image_bytes)
        if not text.isdigit():
            # Not a Secure QR payload (e.g. an old unsigned Aadhaar QR, or an unrelated QR code).
            # Not supported: there is no signature to verify on that format.
            raise ValueError("unsupported_qr_format")
        try:
            qr = AadhaarSecureQr(int(text))
        except AadhaarQrParseError as e:
            raise ValueError(f"qr_parse_failed:{e}")

        public_key, _ = _load_qr_cert()
        verified = False
        failure_reason = None
        try:
            public_key.verify(qr.signature(), qr.signedData(), padding.PKCS1v15(), hashes.SHA256())
            verified = True
        except InvalidSignature:
            failure_reason = "signature_invalid"
        except Exception as e:
            failure_reason = f"signature_verification_error:{type(e).__name__}"

        data = qr.decodeddata()
        return self._persistence.create_verification(
            owner_id=owner_id,
            owner_role=owner_role,
            method="qr",
            verified=verified,
            masked_aadhaar=_mask_aadhaar(data.get("referenceid", "")),
            extracted_data=_safe_extracted_fields(data),
            failure_reason=failure_reason,
        )

    # ---------- Offline e-KYC XML flow ----------

    def verify_offline_xml(self, zip_bytes: bytes, share_code: str, owner_id: str, owner_role: str):
        if not zip_bytes:
            raise ValueError("empty_file")
        if len(zip_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        if not share_code or not share_code.strip():
            raise ValueError("missing_share_code")

        try:
            parsed = AadhaarOfflineXML(io.BytesIO(zip_bytes), share_code)
        except AadhaarQrParseError as e:
            raise ValueError(f"xml_parse_failed:{e}")

        _, cert_pem_bytes = _load_xml_cert()
        verified = False
        failure_reason = None
        try:
            XMLVerifier().verify(parsed.raw_xml(), x509_cert=cert_pem_bytes)
            verified = True
        except Exception as e:
            # signxml raises several distinct exception types (InvalidSignature, InvalidDigest,
            # InvalidCertificate, etree parse errors, ...) - all mean "could not cryptographically
            # confirm this document", so all funnel to the same not-verified outcome.
            failure_reason = f"xml_signature_invalid:{type(e).__name__}"

        data = parsed.decodeddata()
        return self._persistence.create_verification(
            owner_id=owner_id,
            owner_role=owner_role,
            method="offline_xml",
            verified=verified,
            masked_aadhaar=_mask_aadhaar(data.get("referenceid", "")),
            extracted_data=_safe_extracted_fields(data),
            failure_reason=failure_reason,
        )

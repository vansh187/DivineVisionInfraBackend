import hashlib
import io
import logging
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

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB, generous for a photo of a card or the offline-XML zip
_QR_FORMAT = zxingcpp.BarcodeFormat.QRCode


def _cert_summary(cert_pem_bytes: bytes) -> str:
    """Non-PII identifying info for a loaded cert - which cert was actually active for a given
    verification attempt, useful for catching stale Render env vars without logging anything
    about the person being verified."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem_bytes)
        fingerprint = hashlib.sha256(cert_pem_bytes).hexdigest()[:16]
        return f"subject={cert.subject.rfc4514_string()!r} serial={cert.serial_number} sha256={fingerprint}"
    except Exception:
        return "unavailable"


def _split_cert_bundle(cert_pem_text: str) -> list:
    """Splits a possibly-multi-certificate PEM string into individual cert PEM blocks. UIDAI
    doesn't publish a single canonical 'the' signing certificate per flow - it's plausible
    (and, in practice, has been necessary) to hold several candidate certificates and try
    each one, since there's no reliable way to know in advance which one a given real card
    was signed with."""
    blocks = []
    start_marker, end_marker = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    pos = 0
    while True:
        start = cert_pem_text.find(start_marker, pos)
        if start == -1:
            break
        end = cert_pem_text.find(end_marker, start)
        if end == -1:
            break
        end += len(end_marker)
        blocks.append(cert_pem_text[start:end])
        pos = end
    return blocks


def _load_cert_bundle(env_var: str):
    """Loads one or more UIDAI verification certificates from config - deliberately NOT a
    hardcoded/bundled certificate; must be supplied via env var. Raises RuntimeError if
    missing/invalid. Returns a list of (public_key, cert_pem_bytes) tuples, one per
    concatenated PEM block found in the env var.

    QR (Secure QR) and Offline e-KYC XML are documented by UIDAI as using different signing
    certificates, so each flow has its own env var here - but in practice a real card's Secure
    QR has been observed verifying only against the certificate documented for Offline XML, not
    any certificate documented/distributed for QR. See _load_qr_verification_certs, which tries
    both bundles for QR verification because of this."""
    cert_pem = os.getenv(env_var)
    if not cert_pem:
        raise RuntimeError(f"{env_var} environment variable must be set")
    blocks = _split_cert_bundle(cert_pem)
    if not blocks:
        raise RuntimeError(f"{env_var}_invalid: no PEM certificate blocks found")
    certs = []
    for block in blocks:
        block_bytes = block.encode("utf-8")
        try:
            cert = x509.load_pem_x509_certificate(block_bytes)
        except Exception as e:
            raise RuntimeError(f"{env_var}_invalid: {type(e).__name__}")
        certs.append((cert.public_key(), block_bytes))
    return certs


def _load_qr_certs():
    return _load_cert_bundle("UIDAI_QR_CERT_PEM")


def _load_xml_certs():
    return _load_cert_bundle("UIDAI_XML_CERT_PEM")


def _load_qr_verification_certs():
    """UIDAI documents Secure QR and Offline e-KYC XML as using distinct signing
    certificates, but that isn't reliably true in practice - confirmed empirically
    against a real 2026-issued (V5) card whose Secure QR signature only verified
    against the certificate documented for the Offline XML flow, not any of the
    certificates documented/distributed for QR verification. So QR verification
    tries its own configured bundle first, then falls back to the XML bundle too.
    Missing/invalid XML config doesn't break QR verification - it's a fallback,
    not a requirement - so RuntimeError from a missing UIDAI_XML_CERT_PEM is
    swallowed here rather than propagated."""
    certs = list(_load_qr_certs())
    seen_fingerprints = {hashlib.sha256(pem).hexdigest() for _, pem in certs}
    try:
        xml_certs = _load_xml_certs()
    except RuntimeError:
        xml_certs = []
    for public_key, pem in xml_certs:
        fingerprint = hashlib.sha256(pem).hexdigest()
        if fingerprint not in seen_fingerprints:
            certs.append((public_key, pem))
            seen_fingerprints.add(fingerprint)
    return certs


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
            logger.info("kyc.qr.extract result=empty_file")
            raise ValueError("empty_file")
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            logger.info("kyc.qr.extract result=file_too_large bytes=%d", len(image_bytes))
            raise ValueError("file_too_large")
        try:
            arr = np.frombuffer(image_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except cv2.error:
            img = None
        if img is None:
            logger.info("kyc.qr.extract result=unreadable_image bytes=%d", len(image_bytes))
            raise ValueError("unreadable_image")
        logger.info("kyc.qr.extract image_shape=%s", getattr(img, "shape", None))

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
            logger.info("kyc.qr.extract result=qr_not_found")
            raise ValueError("qr_not_found")
        logger.info("kyc.qr.extract result=found payload_digits=%d", len(results[0].text))
        return results[0].text

    def verify_qr(self, image_bytes: bytes, owner_id: str, owner_role: str):
        logger.info("kyc.qr.request owner_id=%s image_bytes=%d", owner_id, len(image_bytes or b""))
        text = self._extract_qr_text(image_bytes)
        logger.info("kyc.qr.detected owner_id=%s payload_digits=%d", owner_id, len(text))
        if not text.isdigit():
            # Not a Secure QR payload (e.g. an old unsigned Aadhaar QR, or an unrelated QR code).
            # Not supported: there is no signature to verify on that format.
            logger.info("kyc.qr.response owner_id=%s result=unsupported_qr_format", owner_id)
            raise ValueError("unsupported_qr_format")
        try:
            qr = AadhaarSecureQr(int(text))
        except AadhaarQrParseError as e:
            logger.info("kyc.qr.response owner_id=%s result=qr_parse_failed reason=%s", owner_id, e)
            raise ValueError(f"qr_parse_failed:{e}")
        logger.info(
            "kyc.qr.parsed owner_id=%s version=%s decompressed_bytes=%d",
            owner_id, qr.decodeddata().get("version", "none"), len(qr.decompressed_array),
        )

        candidates = _load_qr_verification_certs()
        logger.info("kyc.qr.cert.candidates owner_id=%s count=%d", owner_id, len(candidates))
        verified = False
        failure_reason = None
        for public_key, cert_pem_bytes in candidates:
            # Signature length is derived from THIS candidate's own RSA key size rather
            # than assumed to always be 256 bytes (RSA-2048) - a candidate signed with a
            # larger key (e.g. RSA-3072/4096) would otherwise have its signature/signed-data
            # split at the wrong offset, guaranteeing InvalidSignature regardless of whether
            # the key itself is actually correct.
            sig_len = public_key.key_size // 8
            try:
                public_key.verify(
                    qr.signature(sig_len), qr.signedData(sig_len), padding.PKCS1v15(), hashes.SHA256()
                )
                verified = True
                failure_reason = None
                logger.info(
                    "kyc.qr.cert.match owner_id=%s cert=%s sig_len=%d", owner_id, _cert_summary(cert_pem_bytes), sig_len
                )
                break
            except InvalidSignature:
                logger.info(
                    "kyc.qr.cert.no_match owner_id=%s cert=%s sig_len=%d", owner_id, _cert_summary(cert_pem_bytes), sig_len
                )
                failure_reason = "signature_invalid"
            except Exception as e:
                failure_reason = f"signature_verification_error:{type(e).__name__}"
                logger.info(
                    "kyc.qr.cert.error owner_id=%s cert=%s sig_len=%d error=%s",
                    owner_id, _cert_summary(cert_pem_bytes), sig_len, failure_reason,
                )
                # Keep trying remaining candidates - an error verifying against one cert
                # (e.g. a malformed key) doesn't mean the next candidate won't succeed.
        logger.info(
            "kyc.qr.response owner_id=%s verified=%s failure_reason=%s masked_aadhaar=%s",
            owner_id, verified, failure_reason, _mask_aadhaar(qr.decodeddata().get("referenceid", "")),
        )

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
        logger.info("kyc.xml.request owner_id=%s zip_bytes=%d", owner_id, len(zip_bytes or b""))
        if not zip_bytes:
            raise ValueError("empty_file")
        if len(zip_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        if not share_code or not share_code.strip():
            raise ValueError("missing_share_code")

        try:
            parsed = AadhaarOfflineXML(io.BytesIO(zip_bytes), share_code)
        except AadhaarQrParseError as e:
            logger.info("kyc.xml.response owner_id=%s result=xml_parse_failed reason=%s", owner_id, e)
            raise ValueError(f"xml_parse_failed:{e}")
        logger.info("kyc.xml.parsed owner_id=%s raw_xml_bytes=%d", owner_id, len(parsed.raw_xml()))

        candidates = _load_xml_certs()
        logger.info("kyc.xml.cert.candidates owner_id=%s count=%d", owner_id, len(candidates))
        verified = False
        failure_reason = None
        for _, cert_pem_bytes in candidates:
            try:
                XMLVerifier().verify(parsed.raw_xml(), x509_cert=cert_pem_bytes)
                verified = True
                failure_reason = None
                logger.info("kyc.xml.cert.match owner_id=%s cert=%s", owner_id, _cert_summary(cert_pem_bytes))
                break
            except Exception as e:
                # signxml raises several distinct exception types (InvalidSignature, InvalidDigest,
                # InvalidCertificate, etree parse errors, ...) - all mean "could not cryptographically
                # confirm this document against this candidate", so keep trying the rest.
                failure_reason = f"xml_signature_invalid:{type(e).__name__}"
                logger.info(
                    "kyc.xml.cert.no_match owner_id=%s cert=%s error=%s",
                    owner_id, _cert_summary(cert_pem_bytes), failure_reason,
                )
        logger.info(
            "kyc.xml.response owner_id=%s verified=%s failure_reason=%s masked_aadhaar=%s",
            owner_id, verified, failure_reason, _mask_aadhaar(parsed.decodeddata().get("referenceid", "")),
        )

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

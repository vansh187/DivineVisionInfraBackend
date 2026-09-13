import gzip
import io
import os
import zipfile
import datetime

import cv2
import numpy as np
import zxingcpp
from lxml import etree
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
from signxml import XMLSigner

# Ensure test env before importing app/persistence
os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

# Generate a throwaway test keypair/cert to stand in for UIDAI's real certificate.
# This validates the verification *mechanics* (accepts genuinely-signed data, rejects
# tampered/wrongly-signed data) - it cannot validate against real UIDAI production
# data, since we deliberately never fabricate or bundle a real UIDAI certificate.
_TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test UIDAI (not real)")])
_TEST_CERT = (
    x509.CertificateBuilder()
    .subject_name(_subject)
    .issuer_name(_subject)
    .public_key(_TEST_PRIVATE_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
    .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
    .sign(_TEST_PRIVATE_KEY, hashes.SHA256())
)
_TEST_CERT_PEM = _TEST_CERT.public_bytes(serialization.Encoding.PEM)
_TEST_KEY_PEM = _TEST_PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)

os.environ["UIDAI_QR_CERT_PEM"] = _TEST_CERT_PEM.decode("utf-8")
os.environ["UIDAI_XML_CERT_PEM"] = _TEST_CERT_PEM.decode("utf-8")

# A second, wrong-key cert - used to test that UIDAI_*_CERT_PEM accepts a bundle of
# multiple candidate certificates and tries each one, since in practice it's been
# necessary to hold several UIDAI certificates and try them all against a real card.
_DECOY_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_decoy_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Decoy cert (wrong key)")])
_DECOY_CERT_PEM = (
    x509.CertificateBuilder()
    .subject_name(_decoy_subject)
    .issuer_name(_decoy_subject)
    .public_key(_DECOY_PRIVATE_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
    .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
    .sign(_DECOY_PRIVATE_KEY, hashes.SHA256())
).public_bytes(serialization.Encoding.PEM)

from unittest.mock import patch
from fastapi.testclient import TestClient
from Divinepersistence.persistence_db import PersistenceDB
from DivineAPI.main import app

client = TestClient(app)


_TOKEN = None  # set in setup_module - one shared test user, reused by every test below


def setup_module(module):
    global _TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    # A single shared user/token for every test in this file: the global rate limiter
    # (10 requests/60s per client+path) applies across the whole test session, and this
    # file doesn't need distinct identities per test (unlike e.g. an ownership check),
    # so one signup+login keeps well within budget instead of exhausting it.
    client.post("/customer/signup", json={"username": "kyc_test_user", "password": "strongpassword123", "phone": "9876500010"})
    lr = client.post("/customer/login", json={"username": "kyc_test_user", "password": "strongpassword123"})
    assert lr.status_code == 200, lr.text
    _TOKEN = lr.json()["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------- Secure QR fixture builder ----------

def _build_secure_qr_int(tamper_signature: bool = False, private_key=None) -> int:
    fields = [
        "3", "999912345678", "Test Name", "01-01-1990", "M", "",
        "District", "", "House", "Location", "123456",
        "PostOffice", "State", "Street", "SubDistrict", "VTC",
    ]
    signed_data = b"\xff".join(f.encode("ISO-8859-1") for f in fields) + b"\xff"
    signature = (private_key or _TEST_PRIVATE_KEY).sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    if tamper_signature:
        signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
    raw = signed_data + signature
    compressed = gzip.compress(raw, compresslevel=6)
    return int.from_bytes(compressed, "big")


def _qr_image_bytes(payload) -> bytes:
    """Renders `payload` as a QR code PNG, self-verified against zxingcpp (the library
    the backend actually uses for detection) so a rendering that's too small/dense to
    decode fails loudly here rather than surfacing as a confusing qr_not_found deep in
    an unrelated test."""
    encoder = cv2.QRCodeEncoder.create()
    qr_matrix = encoder.encode(str(payload))  # already 0/255 uint8, not a 0/1 matrix
    scale, border = 8, 32
    big = np.repeat(np.repeat(qr_matrix, scale, axis=0), scale, axis=1)
    bordered = cv2.copyMakeBorder(big, border, border, border, border, cv2.BORDER_CONSTANT, value=255)
    ok, png_bytes = cv2.imencode(".png", bordered)
    assert ok
    results = zxingcpp.read_barcodes(bordered, formats=zxingcpp.BarcodeFormat.QRCode)
    assert results and results[0].text == str(payload), "test fixture itself failed to render a decodable QR"
    return png_bytes.tobytes()


# ---------- Offline XML fixture builder ----------

def _build_offline_xml_zip_bytes(share_code: str, tamper_after_signing: bool = False) -> bytes:
    xml_doc = etree.fromstring(
        b'<OfflinePaperlessKyc referenceId="888812345678">'
        b'<UidData><Poi m="" e="" name="Test Person" dob="02-02-1985" gender="F"/>'
        b'<Poa careof="" dist="TestDist" landmark="" house="12" loc="TestLoc" pc="560001" '
        b'po="TestPO" state="Karnataka" street="MG Road" subdist="" vtc="Bengaluru"/></UidData>'
        b"</OfflinePaperlessKyc>"
    )
    signed_root = XMLSigner().sign(xml_doc, key=_TEST_KEY_PEM, cert=_TEST_CERT_PEM)
    signed_bytes = etree.tostring(signed_root)
    if tamper_after_signing:
        signed_bytes = signed_bytes.replace(b"Test Person", b"Evil Person")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("offline.xml", signed_bytes)
    buf.seek(0)
    raw_zip = buf.getvalue()

    # zipfile (stdlib) can't natively write an encrypted zip, but our persistence layer
    # only relies on AadhaarOfflineXML's zipfile.setpassword() call at *read* time, and
    # a plain (unencrypted) zip works fine there since setpassword() is a no-op unless
    # the entry is actually encrypted. This still exercises the full read/parse/verify path.
    return raw_zip


# ================= QR flow =================

def test_qr_verify_requires_auth():
    r = client.post("/kyc/aadhaar/qr/verify", files={"file": ("card.png", b"x", "image/png")})
    assert r.status_code == 401


def test_qr_verify_rejects_empty_file():
    r = client.post(
        "/kyc/aadhaar/qr/verify",
        files={"file": ("card.png", b"", "image/png")},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_file"


def test_qr_verify_rejects_image_with_no_qr_code():
    blank = np.zeros((100, 100, 3), dtype=np.uint8)
    ok, png_bytes = cv2.imencode(".png", blank)
    r = client.post(
        "/kyc/aadhaar/qr/verify",
        files={"file": ("card.png", png_bytes.tobytes(), "image/png")},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "qr_not_found"


def test_qr_verify_rejects_non_aadhaar_qr():
    image_bytes = _qr_image_bytes("https://example.com/not-an-aadhaar-qr")
    r = client.post(
        "/kyc/aadhaar/qr/verify",
        files={"file": ("card.png", image_bytes, "image/png")},
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_qr_format"


def test_qr_verify_valid_signature():
    image_bytes = _qr_image_bytes(_build_secure_qr_int())
    r = client.post(
        "/kyc/aadhaar/qr/verify",
        files={"file": ("card.png", image_bytes, "image/png")},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True
    assert data["status"] == "success"
    assert data["message"] == "Aadhaar verification successful."
    assert data["method"] == "qr"
    assert data["masked_aadhaar"] == "XXXXXXXX9999"
    assert data["extracted_data"]["name"] == "Test Name"
    assert data["failure_reason"] is None


def test_qr_verify_succeeds_when_second_cert_in_bundle_matches():
    # UIDAI_QR_CERT_PEM can hold multiple concatenated candidate certificates - the first
    # (wrong) one should be tried and rejected, then the second (correct) one accepted,
    # rather than giving up after the first candidate fails.
    bundle = _DECOY_CERT_PEM.decode("utf-8") + "\n" + _TEST_CERT_PEM.decode("utf-8")
    image_bytes = _qr_image_bytes(_build_secure_qr_int())
    with patch.dict(os.environ, {"UIDAI_QR_CERT_PEM": bundle}):
        r = client.post(
            "/kyc/aadhaar/qr/verify",
            files={"file": ("card.png", image_bytes, "image/png")},
            headers=_auth_headers(),
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True
    assert data["failure_reason"] is None


def test_qr_verify_fails_when_no_cert_in_bundle_matches():
    # QR verification also falls back to the UIDAI_XML_CERT_PEM bundle (see
    # _load_qr_verification_certs) - both must be mismatched for this to fail.
    bundle = _DECOY_CERT_PEM.decode("utf-8")
    image_bytes = _qr_image_bytes(_build_secure_qr_int())
    with patch.dict(os.environ, {"UIDAI_QR_CERT_PEM": bundle, "UIDAI_XML_CERT_PEM": bundle}):
        r = client.post(
            "/kyc/aadhaar/qr/verify",
            files={"file": ("card.png", image_bytes, "image/png")},
            headers=_auth_headers(),
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is False
    assert data["failure_reason"] == "signature_invalid"


def test_qr_verify_succeeds_via_xml_cert_fallback_when_qr_bundle_mismatches():
    # Real-world case this fallback exists for: a card whose Secure QR signature only
    # verifies against the certificate documented/configured for Offline XML, not any
    # of the certificates configured for QR. UIDAI_QR_CERT_PEM alone is the decoy; only
    # UIDAI_XML_CERT_PEM (the module-level real test cert) can match. Exercises
    # serviceKyc directly rather than through the HTTP endpoint, so this doesn't
    # consume the shared per-path rate-limit budget the other tests in this file share.
    from DivineService.service_kyc import serviceKyc

    image_bytes = _qr_image_bytes(_build_secure_qr_int())
    with patch.dict(os.environ, {"UIDAI_QR_CERT_PEM": _DECOY_CERT_PEM.decode("utf-8")}):
        record = serviceKyc().verify_qr(image_bytes, owner_id="C00001", owner_role="customer")
    assert record.verified
    assert record.failure_reason is None


def test_qr_verify_succeeds_with_larger_key_size_cert():
    # Regression test: signature length used to be hardcoded to 256 bytes (RSA-2048).
    # A card signed with a larger key (e.g. RSA-3072, 384-byte signature) would have its
    # signature/signed-data split at the wrong offset and fail InvalidSignature no matter
    # how many correct-but-2048-bit candidate certs were tried. sig_len is now derived
    # from each candidate's own key size instead of assumed uniform.
    large_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    large_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test UIDAI (3072-bit)")])
    large_cert_pem = (
        x509.CertificateBuilder()
        .subject_name(large_subject)
        .issuer_name(large_subject)
        .public_key(large_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .sign(large_key, hashes.SHA256())
    ).public_bytes(serialization.Encoding.PEM)

    image_bytes = _qr_image_bytes(_build_secure_qr_int(private_key=large_key))
    with patch.dict(os.environ, {"UIDAI_QR_CERT_PEM": large_cert_pem.decode("utf-8")}):
        r = client.post(
            "/kyc/aadhaar/qr/verify",
            files={"file": ("card.png", image_bytes, "image/png")},
            headers=_auth_headers(),
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True, data


def test_qr_verify_tampered_signature_not_verified():
    image_bytes = _qr_image_bytes(_build_secure_qr_int(tamper_signature=True))
    r = client.post(
        "/kyc/aadhaar/qr/verify",
        files={"file": ("card.png", image_bytes, "image/png")},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is False
    assert data["status"] == "error"
    assert data["message"]
    assert data["failure_reason"] == "signature_invalid"


def test_qr_verify_fails_cleanly_without_configured_cert():
    image_bytes = _qr_image_bytes(_build_secure_qr_int())
    with patch.dict(os.environ, {}, clear=False):
        del os.environ["UIDAI_QR_CERT_PEM"]
        try:
            r = client.post(
                "/kyc/aadhaar/qr/verify",
                files={"file": ("card.png", image_bytes, "image/png")},
                headers=_auth_headers(),
            )
        finally:
            os.environ["UIDAI_QR_CERT_PEM"] = _TEST_CERT_PEM.decode("utf-8")
    assert r.status_code == 500
    assert "UIDAI_QR_CERT_PEM" in r.json()["detail"]


# ================= Offline XML flow =================

def test_xml_verify_requires_auth():
    r = client.post(
        "/kyc/aadhaar/xml/verify",
        files={"file": ("offline.zip", b"x", "application/zip")},
        data={"share_code": "1234"},
    )
    assert r.status_code == 401


def test_xml_verify_missing_share_code_is_422():
    r = client.post(
        "/kyc/aadhaar/xml/verify",
        files={"file": ("offline.zip", b"x", "application/zip")},
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_xml_verify_valid_signature():
    zip_bytes = _build_offline_xml_zip_bytes("1234")
    r = client.post(
        "/kyc/aadhaar/xml/verify",
        files={"file": ("offline.zip", zip_bytes, "application/zip")},
        data={"share_code": "1234"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True
    assert data["status"] == "success"
    assert data["method"] == "offline_xml"
    assert data["masked_aadhaar"] == "XXXXXXXX8888"
    assert data["extracted_data"]["name"] == "Test Person"


def test_xml_verify_succeeds_when_second_cert_in_bundle_matches():
    bundle = _DECOY_CERT_PEM.decode("utf-8") + "\n" + _TEST_CERT_PEM.decode("utf-8")
    zip_bytes = _build_offline_xml_zip_bytes("1234")
    with patch.dict(os.environ, {"UIDAI_XML_CERT_PEM": bundle}):
        r = client.post(
            "/kyc/aadhaar/xml/verify",
            files={"file": ("offline.zip", zip_bytes, "application/zip")},
            data={"share_code": "1234"},
            headers=_auth_headers(),
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True


def test_xml_verify_succeeds_via_qr_cert_fallback_when_xml_bundle_mismatches():
    # Mirror of the QR flow's fallback to the XML bundle: verify_offline_xml now also
    # falls back to UIDAI_QR_CERT_PEM if its own bundle doesn't match, since it isn't
    # known in which direction a documented-cert mismatch might recur. Exercises
    # serviceKyc directly to avoid consuming the shared per-path rate-limit budget.
    from DivineService.service_kyc import serviceKyc

    zip_bytes = _build_offline_xml_zip_bytes("1234")
    with patch.dict(os.environ, {"UIDAI_XML_CERT_PEM": _DECOY_CERT_PEM.decode("utf-8")}):
        record = serviceKyc().verify_offline_xml(
            zip_bytes, "1234", owner_id="C00001", owner_role="customer"
        )
    assert record.verified
    assert record.failure_reason is None


def test_xml_verify_tampered_content_not_verified():
    zip_bytes = _build_offline_xml_zip_bytes("1234", tamper_after_signing=True)
    r = client.post(
        "/kyc/aadhaar/xml/verify",
        files={"file": ("offline.zip", zip_bytes, "application/zip")},
        data={"share_code": "1234"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is False
    assert data["status"] == "error"
    assert data["message"]
    assert data["failure_reason"] is not None


def test_xml_verify_rejects_corrupt_zip():
    r = client.post(
        "/kyc/aadhaar/xml/verify",
        files={"file": ("offline.zip", b"this is not a zip file", "application/zip")},
        data={"share_code": "1234"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400

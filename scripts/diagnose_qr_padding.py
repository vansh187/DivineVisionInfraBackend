"""
Run this LOCALLY against a real Aadhaar QR image. Tries every certificate
currently configured in UIDAI_QR_CERT_PEM against the real payload using
both PKCS1v15 (what the app currently uses) and RSA-PSS padding, so we can
tell whether the padding scheme - not the certificate - is the mismatch.

Prints only cert identity (subject/serial/fingerprint, already non-PII) and
match/no-match booleans. Never prints decoded demographic fields.

Usage:
    python scripts/diagnose_qr_padding.py path/to/qr_photo.jpg
"""
import sys

from _bootstrap import setup
setup()

import cv2
import numpy as np
import zxingcpp
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

from DivineService.aadhaar_decode import AadhaarSecureQr, AadhaarQrParseError
from DivineService.service_kyc import _load_qr_certs, _cert_summary


def main(path: str) -> None:
    with open(path, "rb") as f:
        image_bytes = f.read()

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    results = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    if not results:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        results = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
    if not results:
        print("no QR code found")
        return

    text = results[0].text
    if not text.isdigit():
        print("payload not numeric")
        return

    try:
        qr = AadhaarSecureQr(int(text))
    except AadhaarQrParseError as e:
        print(f"parse failed: {e}")
        return

    print(f"decompressed_bytes: {len(qr.decompressed_array)}")
    candidates = _load_qr_certs()
    print(f"cert_candidates: {len(candidates)}")

    for public_key, cert_pem_bytes in candidates:
        sig_len = public_key.key_size // 8
        summary = _cert_summary(cert_pem_bytes)
        sig = qr.signature(sig_len)
        data = qr.signedData(sig_len)

        # 1) current behavior: PKCS1v15 + SHA256
        try:
            public_key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
            print(f"MATCH  pkcs1v15/sha256   {summary}")
            continue
        except InvalidSignature:
            pass

        # 2) RSA-PSS with SHA256, standard MGF1(SHA256), salt_length = digest size
        try:
            public_key.verify(
                sig, data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
                hashes.SHA256(),
            )
            print(f"MATCH  pss/sha256/salt=32   {summary}")
            continue
        except InvalidSignature:
            pass

        # 3) RSA-PSS with SHA256, salt_length = max (common alternate default)
        try:
            public_key.verify(
                sig, data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
            print(f"MATCH  pss/sha256/salt=max   {summary}")
            continue
        except InvalidSignature:
            pass

        # 4) PKCS1v15 + SHA1 (older UIDAI docs/tools sometimes reference SHA1)
        try:
            public_key.verify(sig, data, padding.PKCS1v15(), hashes.SHA1())
            print(f"MATCH  pkcs1v15/sha1   {summary}")
            continue
        except InvalidSignature:
            pass

        print(f"no_match (tried pkcs1v15/sha256, pss/sha256 x2, pkcs1v15/sha1)   {summary}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/diagnose_qr_padding.py path/to/qr_photo.jpg")
        sys.exit(1)
    main(sys.argv[1])

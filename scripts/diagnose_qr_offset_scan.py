"""
Run this LOCALLY against a real Aadhaar QR image. Slides the assumed
256/384/512-byte signature window across the tail of the decompressed
payload (in case there's a small trailer after the real signature that our
fixed "last N bytes" assumption doesn't account for), and tries every held
certificate at every offset.

Prints only cert identity and the offset/length of any match - never any
decoded demographic field content.

Usage:
    python scripts/diagnose_qr_offset_scan.py path/to/qr_photo.jpg
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import cv2
import numpy as np
import zxingcpp
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

from DivineService.aadhaar_decode import AadhaarSecureQr, AadhaarQrParseError
from DivineService.service_kyc import _load_qr_certs, _cert_summary

MAX_TRAILER_SCAN = 64  # bytes of possible trailer-after-signature to try


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

    decompressed = qr.decompressed_array
    total = len(decompressed)
    print(f"decompressed_bytes: {total}")
    candidates = _load_qr_certs()
    print(f"cert_candidates: {len(candidates)}")

    found_any = False
    for public_key, cert_pem_bytes in candidates:
        sig_len = public_key.key_size // 8
        summary = _cert_summary(cert_pem_bytes)
        for trailer in range(0, MAX_TRAILER_SCAN + 1):
            end = total - trailer
            start = end - sig_len
            if start < 0:
                break
            sig = decompressed[start:end]
            data = decompressed[:start]
            try:
                public_key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
                print(f"MATCH  trailer_bytes={trailer} sig_len={sig_len}   {summary}")
                found_any = True
            except InvalidSignature:
                continue
        print(f"scanned trailer 0..{MAX_TRAILER_SCAN} for sig_len={sig_len}   {summary}")

    if not found_any:
        print("no match at any offset for any held certificate")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/diagnose_qr_offset_scan.py path/to/qr_photo.jpg")
        sys.exit(1)
    main(sys.argv[1])

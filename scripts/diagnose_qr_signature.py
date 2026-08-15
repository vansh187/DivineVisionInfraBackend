"""
Run this LOCALLY against your own real Aadhaar QR image. It prints only
structural/non-PII facts about the decoded payload (lengths, hex of the
trailing bytes, delimiter count) - never any demographic field content -
so its output is safe to paste back for debugging.

Usage:
    python scripts/diagnose_qr_signature.py path/to/qr_photo.jpg
"""
import sys
import zlib

import cv2
import numpy as np
import zxingcpp


def main(path: str) -> None:
    with open(path, "rb") as f:
        image_bytes = f.read()

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        print("could not decode image file")
        return

    results = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    if not results:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        results = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
    if not results:
        print("no QR code found in image")
        return

    text = results[0].text
    print(f"payload_digits: {len(text)}")
    if not text.isdigit():
        print("payload is not purely numeric - not a Secure QR payload")
        return

    n = int(text)
    bytes_array = n.to_bytes(5000, "big").lstrip(b"\x00")
    decompressed = zlib.decompress(bytes_array, 16 + zlib.MAX_WBITS)

    print(f"decompressed_total_bytes: {len(decompressed)}")
    print(f"version_prefix: {decompressed[:2]!r}")

    delimiter_count = decompressed.count(255)
    print(f"delimiter_0xFF_count: {delimiter_count}")

    for label, sig_len in [("rsa2048", 256), ("rsa3072", 384), ("rsa4096", 512)]:
        if len(decompressed) <= sig_len:
            continue
        tail = decompressed[len(decompressed) - sig_len:]
        # A raw RSA-PKCS1v15 signature is effectively random-looking bytes the
        # full width of the key. A DER-encoded ECDSA signature instead starts
        # with 0x30 (SEQUENCE tag) near the boundary and is shorter/variable
        # length - if trailing bytes at every one of these candidate offsets
        # start with 0x30 followed by a plausible length byte, that's a sign
        # this isn't a fixed-length RSA blob at all.
        print(
            f"tail_{label} first_byte=0x{tail[0]:02x} "
            f"first_8_hex={tail[:8].hex()} last_8_hex={tail[-8:].hex()}"
        )

    # Scan backwards from the end for the last few 0x30 bytes (potential DER
    # SEQUENCE tags), which would suggest an ECDSA signature living somewhere
    # in the tail region rather than a flat RSA blob filling it entirely.
    tail_512 = decompressed[-512:]
    positions = [i for i, b in enumerate(tail_512) if b == 0x30]
    print(f"0x30_byte_positions_in_last_512 (offset from end, count={len(positions)}): "
          f"{[512 - p for p in positions][:10]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/diagnose_qr_signature.py path/to/qr_photo.jpg")
        sys.exit(1)
    main(sys.argv[1])

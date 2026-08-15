import gzip
import io
import os
import zipfile

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

import pytest
from DivineService.aadhaar_decode import AadhaarSecureQr, AadhaarOfflineXML, AadhaarQrParseError

# Order must match AadhaarSecureQr.details after the non-V2 pop (drops "version" and
# "last_4_digits_mobile_no"): email_mobile_status, referenceid, name, dob, gender, careof,
# district, landmark, house, location, pincode, postoffice, state, street, subdistrict, vtc.
_FIELDS = [
    "3", "999912345678", "Test Name", "01-01-1990", "M", "",
    "District", "", "House", "Location", "123456",
    "PostOffice", "State", "Street", "SubDistrict", "VTC",
]

# Same fields as _FIELDS, but with the full details layout (includes "version"
# and "last_4_digits_mobile_no") - used to exercise version prefixes other
# than the hardcoded "V2" (e.g. "V5", observed on a real card in production).
_VERSIONED_FIELDS = [
    "V5", "3", "999912345678", "Test Name", "01-01-1990", "M", "",
    "District", "", "House", "Location", "123456",
    "PostOffice", "State", "Street", "SubDistrict", "VTC", "XXXXXX1234",
]


def _encode_qr_int(fields) -> int:
    signed_data = b"\xff".join(f.encode("ISO-8859-1") for f in fields) + b"\xff"
    raw = signed_data + b"\x00" * 256  # dummy 256-byte "signature" - parsing doesn't verify it
    compressed = gzip.compress(raw, compresslevel=6)
    return int.from_bytes(compressed, "big")


# ---------- AadhaarSecureQr ----------

def test_parses_well_formed_non_v2_payload():
    qr = AadhaarSecureQr(_encode_qr_int(_FIELDS))
    data = qr.decodeddata()
    assert data["name"] == "Test Name"
    assert data["referenceid"] == "999912345678"
    assert data["aadhaar_last_4_digit"] == "9999"
    assert data["email"] is True   # email_mobile_status "3" -> both
    assert data["mobile"] is True


def test_parses_well_formed_versioned_payload_beyond_v2():
    # Regression test: _check_aadhaar_version() used to only recognize the
    # literal string "V2" as a version marker. Any other version digit (e.g.
    # "V5") was treated as "no version prefix", which drops the "version" and
    # "last_4_digits_mobile_no" fields from the expected layout and shifts
    # every remaining field's slice by one position - referenceid ends up
    # holding what should have been email_mobile_status (a single digit),
    # which then fails the "at least 4 chars" check even though the QR
    # decoded and gzip-decompressed just fine.
    qr = AadhaarSecureQr(_encode_qr_int(_VERSIONED_FIELDS))
    data = qr.decodeddata()
    assert data["version"] == "V5"
    assert data["name"] == "Test Name"
    assert data["referenceid"] == "999912345678"
    assert data["aadhaar_last_4_digit"] == "9999"
    assert data["last_4_digits_mobile_no"] == "XXXXXX1234"


def test_signature_and_signed_data_split_at_last_256_bytes():
    qr = AadhaarSecureQr(_encode_qr_int(_FIELDS))
    assert len(qr.signature()) == 256
    assert qr.signature() == b"\x00" * 256
    assert qr.signedData() + qr.signature() == qr.decompressed_array


def test_garbage_input_raises_parse_error_not_a_raw_exception():
    garbage = int.from_bytes(b"not-a-valid-gzip-stream-at-all", "big")
    with pytest.raises(AadhaarQrParseError):
        AadhaarSecureQr(garbage)


def test_legacy_xml_qr_format_raises_clear_error_not_garbage_field():
    # The pre-Secure-QR "legacy" Aadhaar QR also gzip-compresses to a base10 integer,
    # but wraps data in XML rather than 0xFF-pipe-delimited fields - structurally
    # different and unsupported. This uses synthetic placeholder XML, not real Aadhaar
    # data, purely to exercise the format-detection branch.
    fake_legacy_xml = b'<PrintLetterBarcodeData uid="999912345678" name="Placeholder" gender="M" yob="1990"/>'
    compressed = gzip.compress(fake_legacy_xml, compresslevel=6)
    payload_int = int.from_bytes(compressed, "big")
    with pytest.raises(AadhaarQrParseError, match="legacy_qr_format_unsupported"):
        AadhaarSecureQr(payload_int)


def test_too_few_fields_raises_parse_error():
    short_fields = ["999912345678", "Test Name"]
    with pytest.raises(AadhaarQrParseError):
        AadhaarSecureQr(_encode_qr_int(short_fields))


def test_qr_reference_id_too_short_raises_parse_error():
    fields = list(_FIELDS)
    fields[1] = "12"  # referenceid, under 4 chars
    with pytest.raises(AadhaarQrParseError):
        AadhaarSecureQr(_encode_qr_int(fields))


def test_email_mobile_status_flag_combinations():
    for status, expect_email, expect_mobile in [("0", False, False), ("1", True, False), ("2", False, True), ("3", True, True)]:
        fields = list(_FIELDS)
        fields[0] = status  # email_mobile_status
        data = AadhaarSecureQr(_encode_qr_int(fields)).decodeddata()
        assert data["email"] is expect_email, f"status={status}"
        assert data["mobile"] is expect_mobile, f"status={status}"


def test_non_numeric_email_mobile_status_defaults_to_no_flags():
    fields = list(_FIELDS)
    fields[0] = "not-a-number"
    data = AadhaarSecureQr(_encode_qr_int(fields)).decodeddata()
    assert data["email"] is False
    assert data["mobile"] is False


# ---------- AadhaarOfflineXML ----------

def _build_zip(xml_bytes: bytes, password: str = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("offline.xml", xml_bytes)
    return buf.getvalue()


_VALID_XML = (
    b'<OfflinePaperlessKyc referenceId="888812345678">'
    b'<UidData><Poi m="1" e="" name="Test Person" dob="02-02-1985" gender="F"/>'
    b'<Poa careof="" dist="TestDist" landmark="" house="12" loc="TestLoc" pc="560001" '
    b'po="TestPO" state="Karnataka" street="MG Road" subdist="" vtc="Bengaluru"/></UidData>'
    b"</OfflinePaperlessKyc>"
)


def test_parses_well_formed_xml_inside_unencrypted_zip():
    # zipfile.setpassword() is a no-op on an entry that isn't actually encrypted,
    # so a plain zip still exercises the full read/parse path (see test_kyc.py's
    # fixture builder for the same reasoning).
    parsed = AadhaarOfflineXML(io.BytesIO(_build_zip(_VALID_XML)), "1234")
    data = parsed.decodeddata()
    assert data["name"] == "Test Person"
    assert data["referenceid"] == "888812345678"
    assert data["aadhaar_last_4_digit"] == "8888"
    # Only "m" (mobile hash) is present in _VALID_XML - counterintuitively this maps to
    # email_mobile_status "1", which the email/mobile flags read as email=True, not mobile.
    # This isn't a bug in our code: it's the exact behavior of the upstream library this
    # module was cross-validated against byte-for-byte, so the test asserts what UIDAI's
    # own encoding actually does rather than what the attribute name suggests it should.
    assert data["email"] is True
    assert data["mobile"] is False


def test_email_mobile_hash_presence_combinations():
    for m, e, expect_email, expect_mobile in [
        ("", "", False, False),   # neither -> status "0"
        ("1", "", True, False),   # mobile hash only -> status "1" -> email flag (see note above)
        ("", "1", False, True),   # email hash only -> status "2" -> mobile flag
        ("1", "1", True, True),   # both -> status "3"
    ]:
        xml = _VALID_XML.replace(b'm="1" e=""', f'm="{m}" e="{e}"'.encode())
        data = AadhaarOfflineXML(io.BytesIO(_build_zip(xml)), "1234").decodeddata()
        assert data["email"] is expect_email, f"m={m!r} e={e!r}"
        assert data["mobile"] is expect_mobile, f"m={m!r} e={e!r}"


def test_raw_xml_returns_original_bytes():
    parsed = AadhaarOfflineXML(io.BytesIO(_build_zip(_VALID_XML)), "1234")
    assert parsed.raw_xml() == _VALID_XML


def test_corrupt_zip_raises_parse_error():
    with pytest.raises(AadhaarQrParseError):
        AadhaarOfflineXML(io.BytesIO(b"this is not a zip file at all"), "1234")


def test_malformed_xml_inside_valid_zip_raises_parse_error():
    with pytest.raises(AadhaarQrParseError):
        AadhaarOfflineXML(io.BytesIO(_build_zip(b"<not><valid xml")), "1234")


def test_missing_expected_attributes_raises_parse_error():
    # Valid XML, but missing the "name" attribute AadhaarOfflineXML expects at root[0][0].
    bad_xml = (
        b'<OfflinePaperlessKyc referenceId="888812345678">'
        b'<UidData><Poi m="" e="" dob="02-02-1985" gender="F"/>'
        b'<Poa/></UidData></OfflinePaperlessKyc>'
    )
    with pytest.raises(AadhaarQrParseError):
        AadhaarOfflineXML(io.BytesIO(_build_zip(bad_xml)), "1234")


def test_xml_reference_id_too_short_raises_parse_error():
    bad_xml = _VALID_XML.replace(b'referenceId="888812345678"', b'referenceId="88"')
    with pytest.raises(AadhaarQrParseError):
        AadhaarOfflineXML(io.BytesIO(_build_zip(bad_xml)), "1234")

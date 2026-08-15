"""
Aadhaar Secure QR and Offline e-KYC XML field parsing.

Adapted from pyaadhaar (https://github.com/Tanmoy741127/pyaadhaar), MIT License,
Copyright (c) Tanmoy Sarkar. Vendored (rather than depended on via pip) because
pyaadhaar's package __init__ transitively imports pyzbar, which requires the
system libzbar shared library to even import successfully - unavailable on this
project's deployment target and not something we want as a hard startup crash
risk. Only the parsing logic is used here; QR image reading and signature
verification against UIDAI's certificate are implemented separately in this repo.
"""
import zipfile
import zlib
import xml.etree.ElementTree as ET


class AadhaarQrParseError(Exception):
    """Raised when Aadhaar Secure QR / Offline XML data cannot be parsed into the expected shape."""


class AadhaarSecureQr:
    """Parses an Aadhaar Secure QR payload (both the pre-2022 and V2/2022-with-photo variants)."""

    def __init__(self, base10encodedstring: int) -> None:
        self.base10encodedstring = base10encodedstring
        self.details = [
            "version", "email_mobile_status", "referenceid", "name", "dob", "gender",
            "careof", "district", "landmark", "house", "location", "pincode",
            "postoffice", "state", "street", "subdistrict", "vtc", "last_4_digits_mobile_no",
        ]
        self.delimeter = [-1]
        self.data = {}
        try:
            self._convert_base10encoded_to_decompressed_array()
            self._check_aadhaar_version()
            self._create_delimeter()
            self._extract_info_from_decompressed_array()
        except AadhaarQrParseError:
            raise
        except Exception as e:
            raise AadhaarQrParseError(f"failed_to_parse_secure_qr: {e}") from e

    def _convert_base10encoded_to_decompressed_array(self) -> None:
        bytes_array = self.base10encodedstring.to_bytes(5000, "big").lstrip(b"\x00")
        self.decompressed_array = zlib.decompress(bytes_array, 16 + zlib.MAX_WBITS)
        # The pre-Secure-QR "legacy" Aadhaar QR (issued roughly before 2018) also
        # gzip-compresses to a base10 integer, but wraps its data in signed XML
        # (root element <PrintLetterBarcodeData ...>) rather than this class's
        # 0xFF-pipe-delimited plain-text layout - a structurally different, unsupported
        # format (see module docstring: only Secure QR is handled here). Detect and
        # reject it explicitly; otherwise the delimiter/field-slicing logic below runs
        # against XML bytes and produces a misleading missing_or_invalid_reference_id
        # instead of a clear signal that this card predates the supported format.
        if self.decompressed_array.lstrip()[:1] == b"<":
            raise AadhaarQrParseError("legacy_qr_format_unsupported")

    def _check_aadhaar_version(self) -> None:
        # UIDAI has issued Secure QR revisions beyond "V2" (e.g. "V5" seen on
        # cards generated in 2026) that use the same 2-byte "V<digit>"
        # version-prefix framing - only the version digit differs. Matching
        # the literal string "V2" here mis-detects any newer version as the
        # pre-versioned legacy layout (no prefix), which shifts every
        # subsequent field's slice by one and corrupts the whole payload -
        # e.g. referenceid ends up holding what should have been
        # email_mobile_status (a single digit), tripping
        # missing_or_invalid_reference_id even though the QR decoded fine.
        prefix = self.decompressed_array[:2]
        is_versioned = len(prefix) == 2 and prefix[0:1] == b"V" and prefix[1:2].isdigit()
        if not is_versioned:
            self.details.pop(0)
            self.details.pop()

    def _create_delimeter(self) -> None:
        for i in range(len(self.decompressed_array)):
            if self.decompressed_array[i] == 255:
                self.delimeter.append(i)
        if len(self.delimeter) < len(self.details) + 1:
            raise AadhaarQrParseError("not_enough_fields_in_qr_payload")

    def _extract_info_from_decompressed_array(self) -> None:
        for i in range(len(self.details)):
            self.data[self.details[i]] = self.decompressed_array[
                self.delimeter[i] + 1 : self.delimeter[i + 1]
            ].decode("ISO-8859-1")
        if not self.data.get("referenceid") or len(self.data["referenceid"]) < 4:
            raise AadhaarQrParseError("missing_or_invalid_reference_id")
        self.data["aadhaar_last_4_digit"] = self.data["referenceid"][:4]
        self.data["aadhaar_last_digit"] = self.data["referenceid"][3]
        self.data["email"] = False
        self.data["mobile"] = False
        try:
            status = int(self.data["email_mobile_status"])
        except ValueError:
            status = 0
        if status in (3, 1):
            self.data["email"] = True
        if status in (3, 2):
            self.data["mobile"] = True

    def decodeddata(self) -> dict:
        return self.data

    def signature(self, sig_len: int = 256) -> bytes:
        """`sig_len` is the trailing signature's byte length - 256 for RSA-2048 (the
        documented/observed default), but callers verifying against a candidate
        certificate with a different key size should pass that key's actual byte
        length (key_size // 8) instead of assuming 2048-bit universally."""
        return self.decompressed_array[len(self.decompressed_array) - sig_len :]

    def signedData(self, sig_len: int = 256) -> bytes:
        return self.decompressed_array[: len(self.decompressed_array) - sig_len]

    # Note: deliberately not extracting the embedded photo here. pyaadhaar's upstream
    # logic for locating the photo's byte boundary (relative to the trailing signature
    # and optional email/mobile hash blocks) is intricate, and getting it wrong would
    # silently return a corrupt/wrong image rather than raise - not worth the risk for
    # a field this feature doesn't need (verification only needs signedData/signature
    # and the demographic fields already extracted above).


class AadhaarOfflineXML:
    """Parses UIDAI's password-protected Aadhaar Offline e-KYC ZIP (ZIP containing signed XML)."""

    def __init__(self, file, share_code: str) -> None:
        self.share_code = share_code
        self.data = {}
        try:
            zf = zipfile.ZipFile(file, "r")
            zf.setpassword(str(self.share_code).encode("utf-8"))
            filedata = zf.open(zf.namelist()[0]).read()
        except (zipfile.BadZipFile, RuntimeError, KeyError) as e:
            # RuntimeError covers wrong-password ("Bad password for file") from zipfile
            raise AadhaarQrParseError(f"invalid_zip_or_share_code: {e}") from e

        self.raw_xml_bytes = filedata

        try:
            parsedxml = ET.fromstring(filedata, parser=ET.XMLParser(encoding="utf-8"))
            self.root = parsedxml

            hashofmobile = self.root[0][0].attrib.get("m", "")
            hashofemail = self.root[0][0].attrib.get("e", "")
            if hashofmobile and hashofemail:
                self.data["email_mobile_status"] = "3"
            elif hashofemail:
                self.data["email_mobile_status"] = "2"
            elif hashofmobile:
                self.data["email_mobile_status"] = "1"
            else:
                self.data["email_mobile_status"] = "0"

            self.data["referenceid"] = self.root.attrib["referenceId"]
            self.data["name"] = self.root[0][0].attrib["name"]
            self.data["dob"] = self.root[0][0].attrib["dob"]
            self.data["gender"] = self.root[0][0].attrib["gender"]
            self.data["careof"] = self.root[0][1].attrib.get("careof", "")
            self.data["district"] = self.root[0][1].attrib.get("dist", "")
            self.data["landmark"] = self.root[0][1].attrib.get("landmark", "")
            self.data["house"] = self.root[0][1].attrib.get("house", "")
            self.data["location"] = self.root[0][1].attrib.get("loc", "")
            self.data["pincode"] = self.root[0][1].attrib.get("pc", "")
            self.data["postoffice"] = self.root[0][1].attrib.get("po", "")
            self.data["state"] = self.root[0][1].attrib.get("state", "")
            self.data["street"] = self.root[0][1].attrib.get("street", "")
            self.data["subdistrict"] = self.root[0][1].attrib.get("subdist", "")
            self.data["vtc"] = self.root[0][1].attrib.get("vtc", "")
            if not self.data.get("referenceid") or len(self.data["referenceid"]) < 4:
                raise AadhaarQrParseError("missing_or_invalid_reference_id")
            self.data["aadhaar_last_4_digit"] = self.data["referenceid"][0:4]
            self.data["aadhaar_last_digit"] = self.data["referenceid"][3]
            self.data["email"] = self.data["email_mobile_status"] in ("1", "3")
            self.data["mobile"] = self.data["email_mobile_status"] in ("2", "3")
        except AadhaarQrParseError:
            raise
        except (KeyError, IndexError, ET.ParseError) as e:
            raise AadhaarQrParseError(f"unexpected_offline_xml_structure: {e}") from e

    def decodeddata(self) -> dict:
        return self.data

    def raw_xml(self) -> bytes:
        return self.raw_xml_bytes

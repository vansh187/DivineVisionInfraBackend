import os

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineAPI.kyc_api import _kyc_message


def test_kyc_message_verified_true_ignores_failure_reason():
    assert _kyc_message(True, None) == "Aadhaar verification successful."
    assert _kyc_message(True, "signature_invalid") == "Aadhaar verification successful."


def test_kyc_message_signature_invalid():
    msg = _kyc_message(False, "signature_invalid")
    assert "signature" in msg.lower()
    assert "genuine" in msg.lower()


def test_kyc_message_xml_signature_invalid_prefix_match():
    msg = _kyc_message(False, "xml_signature_invalid:InvalidSignature")
    assert "Offline XML" in msg


def test_kyc_message_signature_verification_error_prefix_match():
    msg = _kyc_message(False, "signature_verification_error:ValueError")
    assert "technical error" in msg.lower()


def test_kyc_message_unknown_failure_reason_falls_back():
    msg = _kyc_message(False, "some_new_failure_reason_not_yet_mapped")
    assert msg == "Aadhaar verification failed."


def test_kyc_message_none_failure_reason_when_not_verified_falls_back():
    # Defensive case - verified is False but failure_reason wasn't set for some reason.
    assert _kyc_message(False, None) == "Aadhaar verification failed."

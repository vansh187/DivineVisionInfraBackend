from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

import jwt


class CapturingOtpEmail:
    def __init__(self):
        self.enabled = True
        self.otps = {}
        self.send_otp_email = MagicMock(side_effect=self._send_otp_email)

    def _send_otp_email(self, email, otp, **kwargs):
        self.otps[(email or "").strip().lower()] = otp
        return True


def signup_and_verify_admin(client, payload, approve=True):
    import DivineAPI.admin_api as admin_api

    original_email = admin_api._admin_service._email
    fake_email = CapturingOtpEmail()
    admin_api._admin_service._email = fake_email
    try:
        signup = client.post("/admin/signup", json=payload)
    finally:
        admin_api._admin_service._email = original_email

    assert signup.status_code == 200, signup.text
    email = payload["email"].strip().lower()
    otp = fake_email.otps[email]
    verify = client.post("/admin/verify-signup", json={"email": email, "otp": otp})
    assert verify.status_code == 200, verify.text
    if approve:
        # Stand-in for a super admin clicking approve - the approval flow itself
        # is covered in test_admin_approval_api.py.
        admin_api._admin_service._persistence.set_approval_status(
            signup.json()["id"], "approved", decided_by="test",
        )
    return signup


def mint_admin_access_token(admin_id, email):
    payload = {
        "sub": admin_id,
        "username": email,
        "role": "admin",
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, "admintestsecret", algorithm="HS256")

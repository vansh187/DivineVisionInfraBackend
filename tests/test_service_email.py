import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

from DivineService.service_email import serviceEmail


def _svc(**kw):
    kw.setdefault("api_key", "re_test")
    kw.setdefault("from_email", "noreply@divinevisioninfra.com")
    kw.setdefault("logo_path", "/does/not/exist.png")
    return serviceEmail(**kw)


def _sent_payload(fn, *args, **kwargs):
    with patch("DivineService.service_email.requests.post") as post:
        post.return_value = MagicMock(status_code=200, text="{}")
        result = fn(*args, **kwargs)
        return result, (post.call_args.kwargs["json"] if post.call_args else None)


def test_channel_partner_welcome_payload():
    ok, payload = _sent_payload(_svc().send_broker_welcome, "b@example.com", first_name="Ravi")
    assert ok is True
    assert payload["from"] == "Divine Vision Infra <noreply@divinevisioninfra.com>"
    assert payload["to"] == ["b@example.com"]
    assert "reply_to" not in payload  # no-reply welcome email
    assert "Ravi" in payload["html"]
    assert "Welcome to the Network" in payload["html"]
    assert "Log in as a Channel Partner" in payload["html"]
    assert "Visits &amp; Commissions" in payload["html"]
    assert "Guardian of Trust" not in payload["html"]
    assert payload["text"]


def test_customer_welcome_payload_differs_from_partner():
    ok, payload = _sent_payload(_svc().send_customer_welcome, "c@example.com", first_name="Meera")
    assert ok is True
    assert "Welcome Home" in payload["html"]
    assert "Log in as a Customer" in payload["html"]
    assert "Site Visits &amp; Bookings" in payload["html"]
    assert "commission" not in payload["html"].lower()
    assert "Guardian of Trust" not in payload["html"]


def test_login_url_defaults_to_marketing_site():
    _, payload = _sent_payload(_svc().send_customer_welcome, "c@example.com", first_name="Meera")
    assert "https://www.divinevisioninfra.com" in payload["html"]


def test_login_url_env_override():
    with patch.dict(os.environ, {"DIVINE_PARTNER_LOGIN_URL": "https://portal.example.com/partner"}):
        _, payload = _sent_payload(_svc().send_broker_welcome, "b@example.com", first_name="Ravi")
    assert "https://portal.example.com/partner" in payload["html"]


def test_welcome_escapes_name():
    _, payload = _sent_payload(_svc().send_customer_welcome, "c@example.com", first_name="<script>x</script>")
    assert "<script>x</script>" not in payload["html"]
    assert "&lt;script&gt;" in payload["html"]


def test_logo_attached_when_file_present(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    svc = _svc(logo_path=str(logo))
    _, payload = _sent_payload(svc.send_customer_welcome, "c@example.com", first_name="Meera")
    assert payload["attachments"][0]["content_id"] == "divinelogo"
    assert 'src="cid:divinelogo"' in payload["html"]


def test_no_attachment_and_wordmark_when_logo_missing():
    _, payload = _sent_payload(_svc().send_customer_welcome, "c@example.com", first_name="Meera")
    assert "attachments" not in payload
    assert "DIVINE VISION INFRA" in payload["html"].upper()


def test_welcome_skipped_when_no_api_key():
    with patch.dict(os.environ, {"RESEND_API_KEY": ""}, clear=False):
        svc = serviceEmail(api_key=None, from_email="noreply@divinevisioninfra.com", logo_path="/none")
    with patch("DivineService.service_email.requests.post") as post:
        assert svc.send_customer_welcome("c@example.com", first_name="Meera") is False
        post.assert_not_called()


def test_welcome_skipped_for_invalid_email():
    with patch("DivineService.service_email.requests.post") as post:
        assert _svc().send_customer_welcome("not-an-email") is False
        post.assert_not_called()


def test_welcome_returns_false_on_resend_error():
    with patch("DivineService.service_email.requests.post") as post:
        post.return_value = MagicMock(status_code=422, text="bad")
        assert _svc().send_broker_welcome("b@example.com", first_name="Ravi") is False


def test_api_key_quotes_are_stripped():
    svc = serviceEmail(api_key='"re_abc"', from_email="noreply@divinevisioninfra.com", logo_path="/none")
    assert svc._api_key == "re_abc"


def test_body_does_not_invite_replies():
    for fn in (_svc().send_customer_welcome, _svc().send_broker_welcome):
        _, payload = _sent_payload(fn, "x@example.com", first_name="Sam")
        assert "reply to this email" not in payload["html"].lower()
        assert "not monitored" in payload["html"].lower()


def test_enabled_flag_reflects_config():
    assert _svc().enabled is True
    with patch.dict(os.environ, {"RESEND_API_KEY": "", "DIVINE_RESEND_EMAIL": ""}, clear=False):
        assert serviceEmail(api_key=None, from_email="noreply@divinevisioninfra.com", logo_path="/none").enabled is False
        assert serviceEmail(api_key="re_x", from_email=None, logo_path="/none").enabled is False


def test_dispatch_welcome_email_skips_when_disabled():
    from DivineService.service_email import dispatch_welcome_email, CUSTOMER
    svc = MagicMock()
    svc.enabled = False
    dispatch_welcome_email(svc, CUSTOMER, "c@example.com", first_name="Meera")
    svc.send_welcome_async.assert_not_called()


def test_dispatch_welcome_email_skips_without_address():
    from DivineService.service_email import dispatch_welcome_email, CUSTOMER
    svc = MagicMock()
    svc.enabled = True
    dispatch_welcome_email(svc, CUSTOMER, None, first_name="Meera")
    svc.send_welcome_async.assert_not_called()


def test_dispatch_welcome_email_dispatches_when_enabled():
    from DivineService.service_email import dispatch_welcome_email, CHANNEL_PARTNER
    svc = MagicMock()
    svc.enabled = True
    dispatch_welcome_email(svc, CHANNEL_PARTNER, "b@example.com", first_name="Ravi", username="ravi")
    svc.send_welcome_async.assert_called_once_with(CHANNEL_PARTNER, "b@example.com", first_name="Ravi", username="ravi")


def test_dispatch_welcome_email_never_raises():
    from DivineService.service_email import dispatch_welcome_email, CUSTOMER
    svc = MagicMock()
    svc.enabled = True
    svc.send_welcome_async.side_effect = RuntimeError("boom")
    dispatch_welcome_email(svc, CUSTOMER, "c@example.com")  # must not raise

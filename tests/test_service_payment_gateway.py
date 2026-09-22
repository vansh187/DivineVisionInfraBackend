"""Unit tests for the Zoho Payments gateway client, mocking requests.post/request
the same way test_service_zoho.py's own HTTP boundary would be mocked - no live
Zoho account needed. Covers: OAuth token fetch/cache/401-triggers-refresh,
create_payment_session's request shape (with an explicit assertion that the
amount sent is a decimal string, never Razorpay's paise convention), redirect
signature verification, webhook signature verification (including a stale-
timestamp replay rejection), and refund create/retrieve."""
import hashlib
import hmac
import time
from unittest.mock import patch, MagicMock

from DivineService.service_payment_gateway import servicePaymentGateway


def _gateway(**env):
    gw = servicePaymentGateway()
    gw._client_id = env.get("client_id", "client_id_fake")
    gw._client_secret = env.get("client_secret", "client_secret_fake")
    gw._refresh_token = env.get("refresh_token", "refresh_token_fake")
    gw._account_id = env.get("account_id", "account_fake")
    gw._accounts_domain = "accounts.zoho.in"
    gw._api_domain = "payments.zoho.in"
    gw._signing_key = env.get("signing_key", "signing_key_fake")
    gw._webhook_secret = env.get("webhook_secret", "webhook_secret_fake")
    return gw


def _reset_token_cache():
    servicePaymentGateway._cached_token = None
    servicePaymentGateway._cached_token_expiry = 0.0


def _token_response(status_code=200, access_token="token_abc", expires_in=3600, api_domain=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"access_token": access_token, "expires_in": expires_in, "api_domain": api_domain}
    resp.text = "{}"
    return resp


# ---------- amount formatting ----------

def test_to_gateway_amount_formats_decimal_never_paise():
    """The single highest-risk assertion in the whole Zoho migration: Zoho
    Payments takes a decimal amount string, never Razorpay's amount*100 paise
    convention. Getting this wrong overcharges/undercharges every customer by
    100x."""
    assert servicePaymentGateway.to_gateway_amount(1500.5) == "1500.50"
    assert servicePaymentGateway.to_gateway_amount(50000) == "50000.00"
    assert servicePaymentGateway.to_gateway_amount("2450000") == "2450000.00"
    assert servicePaymentGateway.to_gateway_amount(99.995) == "100.00"  # rounds half up to the paisa


# ---------- OAuth token ----------

def test_get_access_token_raises_when_not_configured():
    _reset_token_cache()
    gw = _gateway(client_id=None)
    try:
        gw._get_access_token()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_not_configured"


@patch("DivineService.service_payment_gateway.requests.post")
def test_get_access_token_fetches_and_caches(mock_post):
    _reset_token_cache()
    mock_post.return_value = _token_response(access_token="token_first")
    gw = _gateway()

    token1 = gw._get_access_token()
    token2 = gw._get_access_token()

    assert token1 == "token_first"
    assert token2 == "token_first"
    mock_post.assert_called_once()  # second call served from cache, no new HTTP request


@patch("DivineService.service_payment_gateway.requests.post")
def test_get_access_token_raises_when_gateway_rejects_refresh_token(mock_post):
    _reset_token_cache()
    mock_post.return_value = _token_response(status_code=401)
    gw = _gateway()
    try:
        gw._get_access_token()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_gateway_auth_failed"


@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_request_refreshes_token_once_on_401_then_retries(mock_post, mock_request):
    """The cache thought the token was still valid but the server disagreed
    (revoked/invalidated server-side) - one fresh token fetch, one retry, never
    an infinite loop."""
    _reset_token_cache()
    mock_post.side_effect = [_token_response(access_token="token_stale"), _token_response(access_token="token_fresh")]

    unauthorized = MagicMock(status_code=401, text="unauthorized")
    unauthorized.json.return_value = {"message": "invalid token"}
    ok = MagicMock(status_code=200, text="{}")
    ok.json.return_value = {"payments_session": {"payments_session_id": "s1", "access_key": "k1"}}
    mock_request.side_effect = [unauthorized, ok]

    gw = _gateway()
    result = gw.create_payment_session(
        amount=100, currency="INR", description="test",
        success_url="https://fe/success", failure_url="https://fe/failure",
    )

    assert result["payments_session_id"] == "s1"
    assert mock_post.call_count == 2
    assert mock_request.call_count == 2


# ---------- create_payment_session ----------

@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_create_payment_session_sends_decimal_amount_and_redirect_urls(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=201, text="{}")
    resp.json.return_value = {
        "payments_session": {
            "payments_session_id": "session_1", "access_key": "key_1",
            "amount": "1500.50", "currency": "INR",
        },
    }
    mock_request.return_value = resp
    gw = _gateway()

    result = gw.create_payment_session(
        amount=1500.50, currency="INR", description="Divine Vision Infra - plot_booking",
        success_url="https://fe.example.com/success", failure_url="https://fe.example.com/failure",
        email="cust@example.com",
    )

    assert result["payments_session_id"] == "session_1"
    assert result["checkout_url"] == "https://payments.zoho.in/hostedcheckout/key_1"
    method, url = mock_request.call_args.args
    assert method == "POST"
    assert "paymentsessions" in url
    body = mock_request.call_args.kwargs["json"]
    # The critical, highest-risk assertion: decimal amount, never paise.
    assert body["amount"] == "1500.50"
    assert body["configurations"]["hosted_checkout_parameters"]["success_url"] == "https://fe.example.com/success"
    assert body["configurations"]["hosted_checkout_parameters"]["failure_url"] == "https://fe.example.com/failure"
    assert body["configurations"]["hosted_checkout_parameters"]["email"] == "cust@example.com"


@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_create_payment_session_raises_on_gateway_error(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=400, text="bad request")
    resp.json.return_value = {"message": "Invalid amount"}
    mock_request.return_value = resp
    gw = _gateway()

    try:
        gw.create_payment_session(
            amount=100, currency="INR", description="test",
            success_url="https://fe/success", failure_url="https://fe/failure",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e).startswith("payment_gateway_error:status_400")


# ---------- verify_redirect_signature ----------

def test_verify_redirect_signature_accepts_matching_hmac():
    gw = _gateway(signing_key="sk_test")
    parts = ["session_1", "succeeded", "pay_1", "succeeded", "5000.00", "", "", "", "", ""]
    signature = hmac.new(b"sk_test", ".".join(parts).encode(), hashlib.sha256).hexdigest()

    assert gw.verify_redirect_signature(
        payments_session_id="session_1", payment_session_status="succeeded",
        payment_id="pay_1", payment_status="succeeded", amount="5000.00", signature=signature,
    ) is True


def test_verify_redirect_signature_rejects_forged_signature():
    gw = _gateway(signing_key="sk_test")
    assert gw.verify_redirect_signature(
        payments_session_id="session_1", payment_session_status="succeeded",
        payment_id="pay_1", payment_status="succeeded", amount="5000.00", signature="forged",
    ) is False


def test_verify_redirect_signature_raises_when_signing_key_not_configured():
    gw = _gateway(signing_key=None)
    try:
        gw.verify_redirect_signature(
            payments_session_id="s", payment_session_status="succeeded",
            payment_id="p", payment_status="succeeded", amount="1.00", signature="sig",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_signing_key_not_configured"


# ---------- verify_webhook_signature ----------

def _webhook_header(secret: str, body: str, timestamp_ms: int = None):
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v={sig}"


def test_verify_webhook_signature_accepts_matching_hmac_within_window():
    gw = _gateway(webhook_secret="whsec_test")
    body = '{"event":"payment.success"}'
    header = _webhook_header("whsec_test", body)
    assert gw.verify_webhook_signature(body.encode(), header) is True


def test_verify_webhook_signature_rejects_forged_signature():
    gw = _gateway(webhook_secret="whsec_test")
    body = '{"event":"payment.success"}'
    header = _webhook_header("wrong_secret", body)
    assert gw.verify_webhook_signature(body.encode(), header) is False


def test_verify_webhook_signature_rejects_malformed_header():
    gw = _gateway(webhook_secret="whsec_test")
    assert gw.verify_webhook_signature(b"{}", "not-a-valid-header") is False
    assert gw.verify_webhook_signature(b"{}", "") is False


def test_verify_webhook_signature_rejects_stale_timestamp_replay():
    """A captured/replayed webhook delivery must be rejected even with a
    correct HMAC, if its timestamp is well outside the freshness window."""
    gw = _gateway(webhook_secret="whsec_test")
    body = '{"event":"payment.success"}'
    stale_ms = int((time.time() - 3600) * 1000)  # one hour old
    header = _webhook_header("whsec_test", body, timestamp_ms=stale_ms)
    assert gw.verify_webhook_signature(body.encode(), header) is False


def test_verify_webhook_signature_raises_when_secret_not_configured():
    gw = _gateway(webhook_secret=None)
    try:
        gw.verify_webhook_signature(b"{}", "t=1,v=sig")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e) == "payment_webhook_not_configured"


# ---------- refunds ----------

@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_create_refund_sends_decimal_amount(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=201, text="{}")
    resp.json.return_value = {"refund": {"refund_id": "rfnd_1", "status": "succeeded", "amount": "2450000.00"}}
    mock_request.return_value = resp
    gw = _gateway()

    result = gw.create_refund(payment_id="pay_1", amount=2450000, reason="requested_by_customer")

    assert result["refund_id"] == "rfnd_1"
    body = mock_request.call_args.kwargs["json"]
    assert body["amount"] == "2450000.00"
    assert body["reason"] == "requested_by_customer"


@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_create_refund_falls_back_to_others_for_unrecognized_reason(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=201, text="{}")
    resp.json.return_value = {"refund": {"refund_id": "rfnd_2", "status": "succeeded"}}
    mock_request.return_value = resp
    gw = _gateway()

    gw.create_refund(payment_id="pay_1", amount=100, reason="not_a_real_enum_value")

    body = mock_request.call_args.kwargs["json"]
    assert body["reason"] == "others"


@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_create_refund_raises_on_gateway_error(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=500, text="internal error")
    resp.json.return_value = {}
    mock_request.return_value = resp
    gw = _gateway()

    try:
        gw.create_refund(payment_id="pay_1", amount=100)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e).startswith("payment_gateway_error:status_500")


@patch("DivineService.service_payment_gateway.requests.request")
@patch("DivineService.service_payment_gateway.requests.post")
def test_retrieve_refund_returns_status(mock_post, mock_request):
    _reset_token_cache()
    mock_post.return_value = _token_response()
    resp = MagicMock(status_code=200, text="{}")
    resp.json.return_value = {"refund": {"refund_id": "rfnd_1", "status": "succeeded", "amount": "100.00"}}
    mock_request.return_value = resp
    gw = _gateway()

    result = gw.retrieve_refund("rfnd_1")

    assert result["status"] == "succeeded"
